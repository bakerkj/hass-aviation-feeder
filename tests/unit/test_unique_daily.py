# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for unique_daily.py — the distinct-aircraft-per-day counter and
its local-midnight rollover."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt.unique_daily import UniqueDailyTracker

DAY1 = (2026, 7, 18)
DAY2 = (2026, 7, 19)


class UniqueDaily(unittest.TestCase):
    def test_counts_distinct_hexes(self):
        t = UniqueDailyTracker()
        self.assertEqual(t.update({"aircraft": [{"hex": "a"}, {"hex": "b"}]}, DAY1), 2)

    def test_unions_across_cycles_and_dedupes(self):
        t = UniqueDailyTracker()
        t.update({"aircraft": [{"hex": "a"}, {"hex": "b"}]}, DAY1)
        # 'a' repeats, 'c' is new -> 3 distinct on the day
        self.assertEqual(t.update({"aircraft": [{"hex": "a"}, {"hex": "c"}]}, DAY1), 3)

    def test_case_insensitive_and_whitespace(self):
        t = UniqueDailyTracker()
        n = t.update(
            {"aircraft": [{"hex": "AbC "}, {"hex": "abc"}, {"hex": " abc"}]}, DAY1
        )
        self.assertEqual(n, 1)  # all the same aircraft

    def test_resets_at_local_midnight(self):
        t = UniqueDailyTracker()
        t.update({"aircraft": [{"hex": "a"}, {"hex": "b"}]}, DAY1)
        # new day -> set resets, only the new cycle's aircraft count
        self.assertEqual(t.update({"aircraft": [{"hex": "z"}]}, DAY2), 1)

    def test_empty_and_malformed(self):
        t = UniqueDailyTracker()
        for payload in ({"aircraft": []}, {}, {"aircraft": "nonsense"}):
            self.assertEqual(t.update(payload, DAY1), 0)

    def test_missing_or_blank_hex_ignored(self):
        t = UniqueDailyTracker()
        n = t.update(
            {
                "aircraft": [
                    {"hex": "a"},
                    {"flight": "NOHEX"},
                    {"hex": ""},
                    {"hex": "  "},
                ]
            },
            DAY1,
        )
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
