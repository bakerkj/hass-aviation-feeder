# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for the pieces lifted out of app.main()'s closure: RateTracker,
PlanefinderFeedState, and assemble_feeder_discovery (metric applicability)."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import app  # noqa: E402
from aviation_feeder_mqtt.feeders import ALL_FEEDER_KEYS  # noqa: E402
from aviation_feeder_mqtt.mlat_stats import MLAT_CAPABLE  # noqa: E402
from aviation_feeder_mqtt.metadata import (  # noqa: E402
    FEEDERS_DEVICE_ID,
    REPORT_BINARY_SENSORS,
)


class RateTracker(unittest.TestCase):
    def test_none_until_baseline_then_per_second(self):
        r = app.RateTracker()
        self.assertIsNone(r.rate("planewatch", "bytes_sent", 100, 10.0))  # baseline
        self.assertEqual(r.rate("planewatch", "bytes_sent", 400, 20.0), 30.0)  # 300/10s

    def test_counter_reset_clamped_to_zero(self):
        r = app.RateTracker()
        r.rate("k", "s", 1000, 10.0)
        self.assertEqual(r.rate("k", "s", 50, 20.0), 0.0)  # reset -> not negative

    def test_non_advancing_timestamp_returns_none(self):
        r = app.RateTracker()
        r.rate("k", "s", 100, 10.0)
        self.assertIsNone(r.rate("k", "s", 200, 10.0))  # ts not advanced

    def test_keys_are_independent(self):
        r = app.RateTracker()
        r.rate("a", "s", 0, 0.0)
        r.rate("b", "s", 0, 0.0)
        self.assertEqual(r.rate("a", "s", 10, 10.0), 1.0)
        self.assertEqual(r.rate("b", "s", 20, 10.0), 2.0)


class PlanefinderFeedState(unittest.TestCase):
    def test_first_cycle_optimistic(self):
        self.assertTrue(app.PlanefinderFeedState().connected(5))
        self.assertFalse(app.PlanefinderFeedState().connected(0))
        self.assertFalse(app.PlanefinderFeedState().connected(None))

    def test_rising_flat_falling(self):
        s = app.PlanefinderFeedState()
        s.connected(100)  # first cycle (optimistic)
        self.assertTrue(s.connected(150))  # +50 -> feeding
        self.assertFalse(s.connected(150))  # flat -> not feeding
        self.assertFalse(s.connected(120))  # fell -> not feeding
        self.assertTrue(s.connected(200))  # rose again -> feeding


class AssembleFeederDiscovery(unittest.TestCase):
    def _topic(self, key, suffix):
        return f"homeassistant/sensor/{FEEDERS_DEVICE_ID}/{key}_{suffix}/config"

    def setUp(self):
        fstat = [
            ("radarbox", "RadarBox", True),
            ("fr24", "FlightRadar24", True),
            ("adsblol", "adsb.lol", True),
        ]
        self.cfg = app.assemble_feeder_discovery(
            "homeassistant",
            "hafeed/feeders",
            "hafeed/status",
            90,
            fstat,
            via_parent=True,
        )

    def test_applicability(self):
        c = self.cfg
        # uptime for all
        for k in ("radarbox", "fr24", "adsblol"):
            self.assertIn(self._topic(k, "uptime"), c)
        # radarbox: kernel bytes + MLAT positions/aircraft, but NOT peers/sync
        self.assertIn(self._topic("radarbox", "bytes_sent_rate"), c)
        self.assertIn(self._topic("radarbox", "mlat_positions_rate"), c)
        self.assertNotIn(self._topic("radarbox", "mlat_peers"), c)
        # fr24: message rate, no byte sensor
        self.assertIn(self._topic("fr24", "messages_rate"), c)
        self.assertNotIn(self._topic("fr24", "bytes_sent"), c)
        # adsblol (community): MLAT peers/sync, no byte/message sensor
        self.assertIn(self._topic("adsblol", "mlat_peers"), c)
        self.assertNotIn(self._topic("adsblol", "bytes_sent"), c)

    def test_via_parent_false_drops_via_device(self):
        fstat = [("radarbox", "RadarBox", True)]
        c = app.assemble_feeder_discovery(
            "homeassistant",
            "hafeed/feeders",
            "hafeed/status",
            90,
            fstat,
            via_parent=False,
        )
        p = c[self._topic("radarbox", "uptime")]
        self.assertNotIn("via_device", p["device"])


class StaleFeederTopics(unittest.TestCase):
    """Retraction of per-feeder discovery.

    The bug this guards: the retraction loop used to iterate the ENABLED feeders
    (compute_feeder_status's output), so a feeder the user switched off was never
    visited and its retained configs stayed in the broker -- its entities sat
    permanently "unavailable" in Home Assistant. Observed live with adsb.one
    (feed_adsbone: false) leaving 6 orphans."""

    PREFIX = "homeassistant"

    def _conn(self, key):
        return f"{self.PREFIX}/binary_sensor/{FEEDERS_DEVICE_ID}/{key}/config"

    def _metric(self, key, suffix):
        return f"{self.PREFIX}/sensor/{FEEDERS_DEVICE_ID}/{key}_{suffix}/config"

    def test_disabled_feeder_is_fully_retracted(self):
        # Publish a complete set for one feeder only; every OTHER feeder is
        # "disabled" from the loop's point of view.
        published = {self._conn("fr24"), self._metric("fr24", "uptime")}
        stale = app.stale_feeder_topics(self.PREFIX, published)
        # a disabled feeder's connection binary_sensor must be retracted --
        # this shape was never covered before
        self.assertIn(self._conn("adsbone"), stale)
        self.assertIn(self._metric("adsbone", "uptime"), stale)
        self.assertIn(self._metric("adsbone", "mlat_sync"), stale)

    def test_published_topics_are_never_retracted(self):
        published = {self._conn("fr24"), self._metric("fr24", "uptime")}
        stale = app.stale_feeder_topics(self.PREFIX, published)
        for t in published:
            self.assertNotIn(t, stale, "retracted a topic that was just published")

    def test_enabled_feeder_missing_one_metric_retracts_only_that(self):
        # An enabled feeder whose applicability dropped a metric: that metric is
        # retracted, its connection is not.
        published = {self._conn("adsblol"), self._metric("adsblol", "uptime")}
        stale = app.stale_feeder_topics(self.PREFIX, published)
        self.assertNotIn(self._conn("adsblol"), stale)
        self.assertIn(self._metric("adsblol", "bytes_sent"), stale)

    def test_covers_every_known_feeder_not_just_enabled_ones(self):
        # The regression in one assertion: with nothing published, every known
        # feeder must appear. A loop over the enabled set would yield nothing.
        stale = app.stale_feeder_topics(self.PREFIX, set())
        for key in ALL_FEEDER_KEYS:
            self.assertIn(self._conn(key), stale, f"{key} would never be cleaned up")

    def test_every_shape_discovery_can_emit_is_retractable(self):
        """Drift guard: if assemble_feeder_discovery grows a fourth topic shape,
        the retraction must learn it too, or that entity becomes unremovable.
        Every topic the builder can produce must appear in a full retraction."""
        fstat = [
            ("piaware", "FlightAware", True),  # has report binary_sensors
            ("fr24", "FlightRadar24", True),  # messages + portal metrics
            ("planefinder", "PlaneFinder", True),  # bytes + portal rates
            ("adsblol", "adsb.lol", True),  # community + MLAT sync
        ]
        disc = app.assemble_feeder_discovery(
            self.PREFIX, "t/feeders", "t/status", 90, fstat, via_parent=True
        )
        retractable = set(app.stale_feeder_topics(self.PREFIX, set()))
        missing = sorted(t for t in disc if t not in retractable)
        self.assertEqual(
            missing, [], "discovery emits topics the retraction cannot remove"
        )

    def test_no_report_binaries_for_feeders_that_never_have_them(self):
        """The inverse of the drift guard above.

        REPORT_BINARY_SENSORS pairs a suffix with the feeder that owns it --
        only piaware has mlat_ok/radio_ok. Flattening that to a bare suffix list
        makes the retraction emit combinations that can never exist
        (adsblol_mlat_ok, fr24_radio_ok, ...): harmless in MQTT but needless
        traffic on every reconnect, and it silently discards the pairing the
        'cannot drift' comment relies on."""
        stale = app.stale_feeder_topics(self.PREFIX, set())
        report = [
            t for t in stale if t.endswith(("_mlat_ok/config", "_radio_ok/config"))
        ]
        owners = {k for k, *_rest in REPORT_BINARY_SENSORS}
        for t in report:
            entity = t.rsplit("/", 2)[-2]  # e.g. piaware_mlat_ok
            key = entity.rsplit("_", 2)[0]  # -> piaware
            self.assertIn(
                key,
                owners,
                f"{t} is a report-binary topic for a feeder that never has one",
            )
        # and the real ones ARE still there
        self.assertEqual(len(report), len(REPORT_BINARY_SENSORS))

    def test_covers_report_binary_sensors(self):
        # piaware's mlat_ok / radio_ok live under binary_sensor/<key>_<suffix>,
        # a shape the old loop never touched.
        stale = app.stale_feeder_topics(self.PREFIX, set())
        self.assertIn(
            f"{self.PREFIX}/binary_sensor/{FEEDERS_DEVICE_ID}/piaware_mlat_ok/config",
            stale,
        )


class MlatStates(unittest.TestCase):
    """Zero-fill for MLAT sensors that are not syncing.

    mlat-client only writes its --stats-json after establishing sync, so a
    missing file beside a running client means "not syncing" -- a known state
    that used to be hidden behind an expired, unavailable sensor."""

    ENABLED = {"adsbfi", "hpradar", "sdrmap", "radarbox", "piaware", "fr24"}

    def test_syncing_feeder_reports_real_values(self):
        stats = {
            "adsbfi": {
                "mlat_peers": 69,
                "mlat_sync": 99,
                "mlat_positions_rate": 23.9,
                "mlat_aircraft": 9,
            }
        }
        out = app.mlat_states(stats, self.ENABLED)
        self.assertEqual(out[("adsbfi", "mlat_peers")], 69)
        self.assertEqual(out[("adsbfi", "mlat_sync")], 99)
        self.assertAlmostEqual(out[("adsbfi", "mlat_positions_rate")], 23.9)

    def test_feeder_with_no_stats_file_reports_zero_not_nothing(self):
        # THE regression: hpradar's client is running but has never written a
        # stats file, so it is not syncing. That is knowledge, not ignorance.
        out = app.mlat_states({}, self.ENABLED)
        for suf in ("mlat_peers", "mlat_sync", "mlat_positions_rate", "mlat_aircraft"):
            self.assertEqual(out[("hpradar", suf)], 0, f"{suf} should be 0")

    def test_sync_incapable_feeders_get_no_peers_or_sync(self):
        # Applicability must mirror assemble_feeder_discovery: radarbox and
        # sdrmap have no peers/sync discovery, so publishing a state for them
        # would target a sensor that was never advertised.
        out = app.mlat_states({}, self.ENABLED)
        for key in ("radarbox", "sdrmap"):
            self.assertNotIn((key, "mlat_peers"), out)
            self.assertNotIn((key, "mlat_sync"), out)
            self.assertEqual(out[(key, "mlat_aircraft")], 0)
            self.assertEqual(out[(key, "mlat_positions_rate")], 0)

    def test_non_mlat_and_disabled_feeders_are_absent(self):
        out = app.mlat_states({}, self.ENABLED)
        # piaware uses fa-mlat-client (not MLAT_CAPABLE); fr24 has no MLAT
        for key in ("piaware", "fr24"):
            self.assertFalse(
                [k for k in out if k[0] == key], f"{key} is not MLAT-capable"
            )
        # a MLAT-capable feeder the user disabled must not get states either
        self.assertFalse([k for k in app.mlat_states({}, set()) if k])

    def test_partial_stats_zero_fill_the_missing_field(self):
        # A file carrying peers but no sync yields sync=0 rather than a gap.
        out = app.mlat_states({"adsbfi": {"mlat_peers": 5}}, self.ENABLED)
        self.assertEqual(out[("adsbfi", "mlat_peers")], 5)
        self.assertEqual(out[("adsbfi", "mlat_sync")], 0)

    def test_every_state_has_matching_discovery(self):
        """Drift guard: a state published for a sensor with no discovery is an
        orphan. Every (key, suffix) produced here must exist as a discovery
        topic for the same feeder set."""
        enabled = sorted(MLAT_CAPABLE)
        fstat = [(k, k, True) for k in enabled]
        disc = app.assemble_feeder_discovery(
            "homeassistant", "t/f", "t/s", 90, fstat, via_parent=True
        )
        for key, suf in app.mlat_states({}, set(enabled)):
            topic = f"homeassistant/sensor/{FEEDERS_DEVICE_ID}/{key}_{suf}/config"
            self.assertIn(
                topic, disc, f"{key}/{suf} would be published with no discovery"
            )


if __name__ == "__main__":
    unittest.main()
