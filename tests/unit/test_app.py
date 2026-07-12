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
from aviation_feeder_mqtt.metadata import FEEDERS_DEVICE_ID  # noqa: E402


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
        s.connected(100)                       # first cycle (optimistic)
        self.assertTrue(s.connected(150))      # +50 -> feeding
        self.assertFalse(s.connected(150))     # flat -> not feeding
        self.assertFalse(s.connected(120))     # fell -> not feeding
        self.assertTrue(s.connected(200))      # rose again -> feeding


class AssembleFeederDiscovery(unittest.TestCase):
    def _topic(self, key, suffix):
        return f"homeassistant/sensor/{FEEDERS_DEVICE_ID}/{key}_{suffix}/config"

    def setUp(self):
        fstat = [("radarbox", "RadarBox", True),
                 ("fr24", "FlightRadar24", True),
                 ("adsblol", "adsb.lol", True)]
        self.cfg = app.assemble_feeder_discovery(
            "homeassistant", "hafeed/feeders", "hafeed/status", 90, fstat, via_parent=True)

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
            "homeassistant", "hafeed/feeders", "hafeed/status", 90, fstat, via_parent=False)
        p = c[self._topic("radarbox", "uptime")]
        self.assertNotIn("via_device", p["device"])


if __name__ == "__main__":
    unittest.main()
