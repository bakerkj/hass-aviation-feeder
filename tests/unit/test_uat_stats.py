# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for the UAT sensor path: compute_uat_metrics (dump978 stats.json
-> HA metric values) and read_uat_stats' missing-file handling."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt.metadata import compute_uat_metrics  # noqa: E402
from aviation_feeder_mqtt.uat_stats import read_uat_stats  # noqa: E402

# A representative /run/stats/stats.json shape (dump978 stats.py output).
STATS = {
    "total": {
        "total_accepted_messages": 60000,
        "total_tracks": 200,
        "max_distance_m": 92600.0,  # 50 nm
        "avg_accepted_rssi": -13.0,
    },
    "last_1min": {
        "total_accepted_messages": 120,  # -> 2.0 msg/s
        "total_tracks": 8,
        "avg_accepted_rssi": -11.5,
        "max_distance_m": 40000.0,
    },
}


class ComputeUatMetrics(unittest.TestCase):
    def test_full_stats(self):
        m = compute_uat_metrics(STATS)
        self.assertEqual(m["uat_aircraft"], 8)  # last_1min.total_tracks
        self.assertAlmostEqual(m["uat_message_rate"], 2.0)  # 120 / 60
        self.assertAlmostEqual(m["uat_max_range_nm"], 50.0, places=1)  # total, m->nm
        self.assertAlmostEqual(m["uat_signal_dbfs"], -11.5)  # last_1min.avg_rssi

    def test_missing_range_when_location_unset(self):
        # No max_distance_m (stats.py skips it when LAT/LON unset) -> None, others ok.
        stats = {"last_1min": {"total_tracks": 3, "total_accepted_messages": 60}}
        m = compute_uat_metrics(stats)
        self.assertEqual(m["uat_aircraft"], 3)
        self.assertAlmostEqual(m["uat_message_rate"], 1.0)
        self.assertIsNone(m["uat_max_range_nm"])
        self.assertIsNone(m["uat_signal_dbfs"])

    def test_empty_stats_all_none(self):
        for payload in ({}, {"last_1min": {}}, {"total": {}}):
            m = compute_uat_metrics(payload)
            self.assertTrue(all(v is None for v in m.values()), payload)

    def test_all_keys_present(self):
        m = compute_uat_metrics(STATS)
        self.assertEqual(
            set(m),
            {"uat_aircraft", "uat_message_rate", "uat_max_range_nm", "uat_signal_dbfs"},
        )


class ReadUatStats(unittest.TestCase):
    def test_missing_file_is_none(self):
        self.assertIsNone(read_uat_stats("/nonexistent/stats.json"))


if __name__ == "__main__":
    unittest.main()
