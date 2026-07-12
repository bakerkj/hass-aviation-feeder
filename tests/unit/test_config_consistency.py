# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The community-aggregator host/port table is duplicated across three files:
the `add_aggregator` calls in 00-haos-options (the actual readsb connectors),
COMMUNITY_FEEDERS in feeders.py (adsb host:port, matched against readsb's
connector status), and MLAT_STATS_BASENAMES in mlat_stats.py (mlat host:port,
the --stats-json filename). Drift is silent at runtime -- a status sensor stuck
"disconnected", or an MLAT sensor that never appears. This test parses
00-haos-options and asserts the two Python tables agree with it, so a drift
fails here instead of in the field."""

import os
import re
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt.feeders import COMMUNITY_FEEDERS  # noqa: E402
from aviation_feeder_mqtt.mlat_stats import MLAT_STATS_BASENAMES  # noqa: E402

_HAOS = os.path.join(
    os.path.dirname(__file__), "..", "..", "aviation_feeder",
    "rootfs", "etc", "cont-init.d", "00-haos-options",
)
_LINE = re.compile(r"^add_aggregator\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)")


def _unquote(s):
    return "" if s == '""' else s


def _parse_add_aggregator():
    rows = {}
    with open(_HAOS, encoding="utf-8") as f:
        for line in f:
            m = _LINE.match(line.strip())
            if m:
                toggle, ah, ap, mh, mp, _uuid = m.groups()
                rows[toggle] = (ah, int(ap), _unquote(mh), _unquote(mp))
    return rows


class AggregatorTablesInSync(unittest.TestCase):
    def setUp(self):
        self.rows = _parse_add_aggregator()
        self.by_toggle = {f[2]: f for f in COMMUNITY_FEEDERS}  # option flag -> tuple

    def test_parsed_something(self):
        self.assertGreaterEqual(len(self.rows), 10)

    def test_every_community_feeder_has_a_connector(self):
        for key, _name, toggle, _h, _p in COMMUNITY_FEEDERS:
            self.assertIn(toggle, self.rows, f"{key}: no add_aggregator line for {toggle}")

    def test_adsb_host_port_match(self):
        for toggle, (ah, ap, _mh, _mp) in self.rows.items():
            self.assertIn(toggle, self.by_toggle, f"{toggle}: not in COMMUNITY_FEEDERS")
            key, _n, _t, host, port = self.by_toggle[toggle]
            self.assertEqual((host, port), (ah, ap),
                             f"{key}: COMMUNITY_FEEDERS adsb {host}:{port} != "
                             f"00-haos-options {ah}:{ap}")

    def test_mlat_basename_matches_or_absent(self):
        for toggle, (_ah, _ap, mh, mp) in self.rows.items():
            key = self.by_toggle[toggle][0]
            if mh:  # aggregator has MLAT -> basename must be "<host>:<port>"
                self.assertEqual(MLAT_STATS_BASENAMES.get(key), f"{mh}:{mp}",
                                 f"{key}: MLAT_STATS_BASENAMES != 00-haos-options {mh}:{mp}")
            else:   # no MLAT (e.g. avdelphi) -> must not be in the basenames
                self.assertNotIn(key, MLAT_STATS_BASENAMES,
                                 f"{key}: has an MLAT basename but no MLAT connector")


if __name__ == "__main__":
    unittest.main()
