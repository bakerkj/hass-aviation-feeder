# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for nearby.py — haversine/bearing math and compute_nearby's
in-range selection, nearest-details, and edge cases."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import nearby  # noqa: E402


class Geometry(unittest.TestCase):
    def test_haversine_same_point_is_zero(self):
        self.assertAlmostEqual(nearby.haversine_nm(42.0, -71.0, 42.0, -71.0), 0.0, places=6)

    def test_haversine_one_degree_lat_is_about_60nm(self):
        # 1 degree of latitude is ~60 nm.
        self.assertAlmostEqual(nearby.haversine_nm(0.0, 0.0, 1.0, 0.0), 60.03, places=1)

    def test_bearing_north_and_east(self):
        self.assertAlmostEqual(nearby.bearing_deg(0.0, 0.0, 1.0, 0.0), 0.0, places=1)
        self.assertAlmostEqual(nearby.bearing_deg(0.0, 0.0, 0.0, 1.0), 90.0, places=1)


class ComputeNearby(unittest.TestCase):
    def test_empty_and_missing_aircraft(self):
        for payload in ({"aircraft": []}, {}, {"aircraft": "nonsense"}):
            out = nearby.compute_nearby(payload, 42.0, -71.0, 50.0)
            self.assertEqual(out["aircraft_in_range"], 0)
            self.assertIsNone(out["nearest"])
            self.assertIsNone(out["nearest_distance_nm"])
            self.assertIsNone(out["nearest_altitude_ft"])

    def test_radius_boundary_and_sorting(self):
        acs = {"aircraft": [
            {"hex": "far", "lat": 43.0, "lon": -71.0, "alt_baro": 30000},   # ~60nm N
            {"hex": "near", "lat": 42.1, "lon": -71.0, "alt_baro": 5000, "flight": "AAL1 "},
        ]}
        out = nearby.compute_nearby(acs, 42.0, -71.0, 50.0)
        self.assertEqual(out["aircraft_in_range"], 1)  # far one is outside 50nm
        self.assertEqual(out["nearest"]["hex"], "near")
        self.assertEqual(out["nearest"]["flight"], "AAL1")  # trimmed
        self.assertEqual(out["nearest_altitude_ft"], 5000)

    def test_ground_altitude_becomes_none(self):
        acs = {"aircraft": [{"hex": "abc", "lat": 42.01, "lon": -71.0, "alt_baro": "ground"}]}
        out = nearby.compute_nearby(acs, 42.0, -71.0, 50.0)
        self.assertEqual(out["aircraft_in_range"], 1)
        self.assertIsNone(out["nearest_altitude_ft"])
        self.assertIsNone(out["nearest"]["altitude_ft"])

    def test_flight_falls_back_to_hex_and_skips_bad_entries(self):
        acs = {"aircraft": [
            "not-a-dict",
            {"hex": "nolatlon"},                                  # skipped: no lat/lon
            {"hex": "dead", "lat": 42.0, "lon": -71.0},           # no flight -> hex
        ]}
        out = nearby.compute_nearby(acs, 42.0, -71.0, 50.0)
        self.assertEqual(out["aircraft_in_range"], 1)
        self.assertEqual(out["nearest"]["flight"], "dead")


if __name__ == "__main__":
    unittest.main()
