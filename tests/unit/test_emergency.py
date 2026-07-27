# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for emergency.py — emergency-squawk detection in aircraft.json."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import emergency


class ComputeEmergency(unittest.TestCase):
    def test_empty_and_malformed(self):
        for payload in ({"aircraft": []}, {}, {"aircraft": "nonsense"}):
            out = emergency.compute_emergency(payload)
            self.assertFalse(out["active"])
            self.assertEqual(out["count"], 0)
            self.assertEqual(out["aircraft"], [])

    def test_normal_squawks_are_not_emergencies(self):
        acs = {
            "aircraft": [
                {"hex": "a1", "squawk": "1200"},
                {"hex": "a2", "squawk": "2000"},
                {"hex": "a3"},  # no squawk
                {"hex": "a4", "squawk": 7700},  # numeric, not a string -> ignored
            ]
        }
        out = emergency.compute_emergency(acs)
        self.assertFalse(out["active"])
        self.assertEqual(out["count"], 0)

    def test_each_emergency_code_detected(self):
        for code, label in (
            ("7500", "hijack"),
            ("7600", "radio failure"),
            ("7700", "general emergency"),
        ):
            out = emergency.compute_emergency(
                {"aircraft": [{"hex": "x", "squawk": code}]}
            )
            self.assertTrue(out["active"], code)
            self.assertEqual(out["count"], 1)
            self.assertEqual(out["aircraft"][0]["type"], label)
            self.assertEqual(out["aircraft"][0]["squawk"], code)

    def test_offender_details_and_flight_fallback(self):
        acs = {
            "aircraft": [
                {
                    "hex": "abc123",
                    "squawk": "7700",
                    "flight": "AAL42 ",
                    "alt_baro": 31000,
                },
                {"hex": "def456", "squawk": "7600"},  # no flight -> falls back to hex
                {"hex": "ghi789", "squawk": "1200"},  # normal -> excluded
            ]
        }
        out = emergency.compute_emergency(acs)
        self.assertTrue(out["active"])
        self.assertEqual(out["count"], 2)
        first = out["aircraft"][0]
        self.assertEqual(first["flight"], "AAL42")  # trimmed
        self.assertEqual(first["altitude_ft"], 31000)
        second = out["aircraft"][1]
        self.assertEqual(second["flight"], "def456")  # hex fallback

    def test_alt_ground_string_is_none(self):
        out = emergency.compute_emergency(
            {"aircraft": [{"hex": "g", "squawk": "7700", "alt_baro": "ground"}]}
        )
        self.assertIsNone(out["aircraft"][0]["altitude_ft"])


if __name__ == "__main__":
    unittest.main()
