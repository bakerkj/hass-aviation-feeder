# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for metadata.py extract helpers — the readsb stats.json field
math that the docstring calls the single source of truth."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import metadata  # noqa: E402


class Extractors(unittest.TestCase):
    def test_messages_per_sec(self):
        s = {"last1min": {"start": 100.0, "end": 160.0, "messages": 600}}
        self.assertAlmostEqual(metadata._messages_per_sec(s), 10.0)

    def test_messages_per_sec_guards(self):
        # zero/negative duration -> None (no div-by-zero)
        self.assertIsNone(
            metadata._messages_per_sec(
                {"last1min": {"start": 100.0, "end": 100.0, "messages": 5}}
            )
        )
        # missing fields -> None
        self.assertIsNone(metadata._messages_per_sec({"last1min": {}}))
        self.assertIsNone(metadata._messages_per_sec({}))

    def test_max_range_nm(self):
        self.assertAlmostEqual(
            metadata._max_range_nm({"total": {"max_distance": 185200}}), 100.0
        )
        self.assertIsNone(metadata._max_range_nm({"total": {}}))

    def test_aircraft_total_partial_none(self):
        self.assertEqual(
            metadata._aircraft_total(
                {"aircraft_with_pos": 5, "aircraft_without_pos": 3}
            ),
            8,
        )
        # only one present -> counts as the other being 0
        self.assertEqual(metadata._aircraft_total({"aircraft_with_pos": 5}), 5)
        # neither present -> None (entity goes unavailable)
        self.assertIsNone(metadata._aircraft_total({}))

    def test_compute_metrics_keys_and_values(self):
        stats = {
            "aircraft_with_pos": 10,
            "aircraft_without_pos": 2,
            "aircraft_count_by_type": {"adsb_icao": 7, "mode_s": 3, "mlat": 1},
            "last1min": {"start": 0, "end": 60, "messages": 1200},
            "total": {"max_distance": 92600, "tracks": {"all": 50}},
        }
        out = metadata.compute_metrics(stats)
        self.assertEqual(out["aircraft_total"], 12)
        self.assertEqual(out["aircraft_adsb"], 7)
        self.assertAlmostEqual(out["messages_per_sec"], 20.0)
        self.assertAlmostEqual(out["max_range_nm"], 50.0)
        self.assertEqual(out["tracks_total"], 50)
        # every METRICS key is present
        self.assertEqual(set(out), {m.key for m in metadata.METRICS})

    def test_compute_sdr_metrics(self):
        stats = {
            "gain_db": 49.6,
            "estimated_ppm": -1.2,
            "last1min": {"local": {"signal": -3.1, "noise": -30.0}},
            "total": {"local": {"samples_dropped": 4}},
        }
        out = metadata.compute_sdr_metrics(stats)
        self.assertAlmostEqual(out["sdr_gain_db"], 49.6)
        self.assertAlmostEqual(out["sdr_signal_dbfs"], -3.1)
        self.assertEqual(out["sdr_samples_dropped"], 4)
        # absent local block -> None, not a crash
        self.assertIsNone(metadata.compute_sdr_metrics({})["sdr_signal_dbfs"])


if __name__ == "__main__":
    unittest.main()
