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
