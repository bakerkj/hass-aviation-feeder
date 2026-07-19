# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for app_reports — piaware status.json + fr24 monitor.json parsing
and the enabled-feeder gating in collect_app_reports."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import app_reports  # noqa: E402


def _truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


class PiawareReport(unittest.TestCase):
    def test_extracts_health_sections(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "status.json")
            with open(p, "w") as f:
                json.dump(
                    {
                        "adept": {"status": "green", "message": "Connected"},
                        "mlat": {"status": "green", "message": "Synchronized"},
                        "radio": {"status": "yellow"},
                        "cpu_temp_celcius": 51.2,
                    },
                    f,
                )
            r = app_reports.piaware_report(p)
            self.assertEqual(r["flightaware"], "green")
            self.assertEqual(r["mlat"], "green")
            self.assertEqual(r["radio"], "yellow")
            self.assertEqual(r["cpu_temp_c"], 51.2)

    def test_missing_file(self):
        self.assertIsNone(app_reports.piaware_report("/nonexistent/status.json"))


class Fr24Report(unittest.TestCase):
    def test_connected_with_messages(self):
        r = app_reports.fr24_report(
            fetch=lambda url: {
                "feed_status": "connected",
                "feed_current_mode": "UDP",
                "num_messages": "2240478",
            }
        )
        self.assertEqual(r["feed_status"], "connected")
        self.assertEqual(r["feed_mode"], "UDP")
        self.assertEqual(r["messages"], 2240478)
        self.assertIs(r["connected"], True)

    def test_disconnected(self):
        r = app_reports.fr24_report(fetch=lambda url: {"feed_status": "disconnected"})
        self.assertIs(r["connected"], False)

    def test_unreachable(self):
        self.assertIsNone(app_reports.fr24_report(fetch=lambda url: None))

    def test_portal_aircraft_counts(self):
        # monitor.json reports every value as a string.
        r = app_reports.fr24_report(
            fetch=lambda url: {
                "feed_status": "connected",
                "feed_num_ac_tracked": "78",
                "feed_num_ac_adsb_tracked": "56",
                "feed_num_ac_non_adsb_tracked": "22",
            }
        )
        self.assertEqual(r["portal_aircraft"], 78)
        self.assertEqual(r["portal_aircraft_adsb"], 56)
        self.assertEqual(r["portal_aircraft_other"], 22)

    def test_portal_aircraft_absent_when_not_reported(self):
        # An older/leaner fr24feed that omits the counts must not fabricate them.
        r = app_reports.fr24_report(fetch=lambda url: {"feed_status": "connected"})
        for k in ("portal_aircraft", "portal_aircraft_adsb", "portal_aircraft_other"):
            self.assertNotIn(k, r)

    def test_unparsable_counts_are_skipped(self):
        r = app_reports.fr24_report(
            fetch=lambda url: {
                "feed_status": "connected",
                "feed_num_ac_tracked": "n/a",
                "feed_num_ac_adsb_tracked": None,
            }
        )
        self.assertNotIn("portal_aircraft", r)
        self.assertNotIn("portal_aircraft_adsb", r)


class PublishAllowlist(unittest.TestCase):
    """The allowlist is enforced in gather_reports, so these hold for every
    reader — including ones added later that nobody wrote a leak test for."""

    # Canary values standing in for the real identity data in these payloads:
    # piaware's site_url (username + site id), rbfeeder's serial/MAC/coords,
    # fr24's feed_alias, pfclient's user_lat/user_lon.
    CANARY = "CANARY-LEAKED-IDENTITY"

    def _gather(self, report):
        """Run one canned report through the real gather_reports filter."""
        return app_reports.gather_reports(
            {"enable_piaware": True, "enable_fr24": True, "enable_planefinder": True},
            _truthy,
            piaware=lambda: report,
            fr24=lambda: report,
            pfclient=lambda: report,
        )

    def test_every_reader_is_registered(self):
        # A reader with no REPORT_FIELDS entry publishes nothing, which would be
        # a silent feature outage. Keep the registry in step with gather_reports.
        got = self._gather({"connected": True, "mlat": "green", "bytes_sent": 1})
        self.assertEqual(
            set(got),
            {"piaware", "fr24", "planefinder"},
            "a feeder in gather_reports is missing from REPORT_FIELDS",
        )

    def test_undeclared_keys_are_dropped_for_every_feeder(self):
        got = self._gather(
            {
                "connected": True,
                "mlat": "green",
                "bytes_sent": 1,
                "site_url": self.CANARY,
                "feed_alias": self.CANARY,
                "sn": self.CANARY,
                "mac": self.CANARY,
                "user_lat": self.CANARY,
                "some_future_upstream_field": self.CANARY,
            }
        )
        self.assertTrue(got, "guard would pass vacuously on an empty result")
        self.assertNotIn(self.CANARY, json.dumps(got))
        for key, fields in got.items():
            self.assertLessEqual(
                set(fields),
                set(app_reports.REPORT_FIELDS[key]),
                f"{key} published a field outside its allowlist",
            )

    def test_filter_applies_to_fields_added_after_gather(self):
        # app.py enriches reports after gather_reports returns (pfclient's
        # derived `connected`) and then publishes them as MQTT attributes. That
        # path bypassed the allowlist until filter_report was applied there too,
        # so exercise the post-gather mutation shape directly.
        enriched = dict(self._gather({"bytes_sent": 5})["planefinder"])
        enriched["connected"] = True  # declared -> must survive
        enriched["user_lat"] = self.CANARY  # undeclared -> must be dropped
        out = app_reports.filter_report("planefinder", enriched)
        self.assertEqual(out["bytes_sent"], 5)
        self.assertIs(out["connected"], True)
        self.assertNotIn("user_lat", out)
        self.assertNotIn(self.CANARY, json.dumps(out))

    def test_filter_report_drops_everything_for_unregistered_feeder(self):
        self.assertEqual(
            app_reports.filter_report("not_a_feeder", {"sn": self.CANARY}), {}
        )

    def test_allowlist_matches_what_readers_actually_emit(self):
        # A declared-but-never-emitted field is dead config; catching it keeps
        # the allowlist honest rather than an ever-growing wishlist. fr24 is the
        # exhaustive case: a payload with every field it knows how to read must
        # produce exactly its declared set.
        fr = app_reports.fr24_report(
            fetch=lambda url: {
                "feed_status": "connected",
                "feed_current_mode": "UDP",
                "num_messages": "5",
                "feed_num_ac_tracked": "78",
                "feed_num_ac_adsb_tracked": "56",
                "feed_num_ac_non_adsb_tracked": "22",
            }
        )
        self.assertEqual(set(fr), set(app_reports.REPORT_FIELDS["fr24"]))


class ReportsDoNotLeakIdentity(unittest.TestCase):
    """Reports are published verbatim to MQTT as feeder attributes (see app.py's
    reports loop), so a reader must copy an explicit allowlist and drop anything
    else. These payloads carry station identity; the repo is public."""

    def test_fr24_drops_feed_alias_and_unknown_keys(self):
        r = app_reports.fr24_report(
            fetch=lambda url: {
                "feed_status": "connected",
                "feed_num_ac_tracked": "78",
                "feed_alias": "T-STATION123",  # station id -- must not publish
                "feed_current_server": "blender.example.invalid",
                "some_future_upstream_field": "surprise",
            }
        )
        blob = json.dumps(r)
        self.assertNotIn("T-STATION123", blob)
        self.assertNotIn("blender.example.invalid", blob)
        self.assertNotIn("surprise", blob)
        self.assertEqual(r["portal_aircraft"], 78)  # allowlisted field survives

    def test_piaware_drops_site_url(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "status.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "piaware": {"status": "green"},
                        "mlat": {"status": "green"},
                        # username + site id live here -- must never be published
                        "site_url": "https://flightaware.example/user/someuser#stats-99",
                        "cpu_temp_celcius": 51.2,
                    },
                    f,
                )
            r = app_reports.piaware_report(p)
            # Assert the allowlisted fields survived FIRST -- otherwise a reader
            # that returned None would make every assertNotIn below pass
            # vacuously and the leak guard would be worthless.
            self.assertEqual(r["mlat"], "green")
            self.assertEqual(r["cpu_temp_c"], 51.2)
            blob = json.dumps(r)
            self.assertNotIn("someuser", blob)
            self.assertNotIn("site_url", blob)


class PfclientReport(unittest.TestCase):
    def test_bytes_no_connected(self):
        r = app_reports.pfclient_report(
            fetch=lambda url: {
                "master_server_bytes_out": 295563,
                "master_server_bytes_in": 29291,
            }
        )
        self.assertEqual(r["bytes_sent"], 295563)
        self.assertEqual(r["bytes_received"], 29291)
        # connected is derived by the caller from a byte DELTA, not here (the
        # counter is cumulative, so >0 stays true forever).
        self.assertNotIn("connected", r)

    def test_unreachable(self):
        self.assertIsNone(app_reports.pfclient_report(fetch=lambda url: None))


class GatherReports(unittest.TestCase):
    def test_only_enabled_feeders(self):
        out = app_reports.gather_reports(
            {"enable_piaware": True, "enable_fr24": False, "enable_planefinder": True},
            _truthy,
            piaware=lambda: {"mlat": "green"},
            fr24=lambda: {"connected": True},
            pfclient=lambda: {"bytes_sent": 5, "connected": True},
        )
        self.assertIn("piaware", out)
        self.assertNotIn("fr24", out)
        self.assertIn("planefinder", out)

    def test_none_report_omitted(self):
        out = app_reports.gather_reports(
            {"enable_piaware": True}, _truthy, piaware=lambda: None
        )
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
