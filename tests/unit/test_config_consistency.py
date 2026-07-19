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

import json
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
    os.path.dirname(__file__),
    "..",
    "..",
    "aviation_feeder",
    "rootfs",
    "etc",
    "cont-init.d",
    "00-haos-options",
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
            self.assertIn(
                toggle, self.rows, f"{key}: no add_aggregator line for {toggle}"
            )

    def test_adsb_host_port_match(self):
        for toggle, (ah, ap, _mh, _mp) in self.rows.items():
            self.assertIn(toggle, self.by_toggle, f"{toggle}: not in COMMUNITY_FEEDERS")
            key, _n, _t, host, port = self.by_toggle[toggle]
            self.assertEqual(
                (host, port),
                (ah, ap),
                f"{key}: COMMUNITY_FEEDERS adsb {host}:{port} != "
                f"00-haos-options {ah}:{ap}",
            )

    def test_mlat_basename_matches_or_absent(self):
        for toggle, (_ah, _ap, mh, mp) in self.rows.items():
            key = self.by_toggle[toggle][0]
            if mh:  # aggregator has MLAT -> basename must be "<host>:<port>"
                self.assertEqual(
                    MLAT_STATS_BASENAMES.get(key),
                    f"{mh}:{mp}",
                    f"{key}: MLAT_STATS_BASENAMES != 00-haos-options {mh}:{mp}",
                )
            else:  # no MLAT (e.g. avdelphi) -> must not be in the basenames
                self.assertNotIn(
                    key,
                    MLAT_STATS_BASENAMES,
                    f"{key}: has an MLAT basename but no MLAT connector",
                )


class SensorGroupToggles(unittest.TestCase):
    """Every ha_* sensor-group toggle must be wired into app.py's per-cycle
    publish gate. That gate is a hand-listed set of booleans: omitting a toggle
    publishes the group's discovery configs but never its state topics, so the
    entities register in Home Assistant and sit permanently unavailable. This is
    exactly how ha_message_types shipped broken, and it is silent -- no error,
    no failing assertion, just dead sensors."""

    _CONFIG = os.path.join(
        os.path.dirname(__file__), "..", "..", "aviation_feeder", "config.json"
    )
    _APP = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "aviation_feeder",
        "aviation_feeder_mqtt",
        "app.py",
    )
    # ha_sensors is the master switch (guards the publisher as a whole) and
    # ha_near_me_radius is a value, not a group toggle.
    _NOT_GROUP_TOGGLES = {"ha_sensors", "ha_near_me_radius"}

    def _group_toggles(self):
        with open(self._CONFIG, encoding="utf-8") as f:
            opts = json.load(f)["options"]
        return {
            k
            for k, v in opts.items()
            if k.startswith("ha_")
            and isinstance(v, bool)
            and k not in self._NOT_GROUP_TOGGLES
        }

    def _app_source(self) -> str:
        with open(self._APP, encoding="utf-8") as f:
            return f.read()

    def test_every_toggle_is_read_by_the_publisher(self):
        src = self._app_source()
        for opt in sorted(self._group_toggles()):
            self.assertIn(
                f'"{opt}"',
                src,
                f"{opt} is defined in config.json but app.py never reads it",
            )

    def test_every_toggle_reaches_the_state_publish_gate(self):
        """The gate is the `any((...))` guarding the per-cycle state publish.
        Each toggle's variable must appear inside it."""
        src = self._app_source()
        m = re.search(r"if health\.connected and any\(\s*\((.*?)\)\s*\)", src, re.S)
        self.assertIsNotNone(m, "could not locate the state-publish gate in app.py")
        gate = m.group(1)
        # map each option to the local variable app.py assigns it to
        var_of = {}
        for opt in self._group_toggles():
            am = re.search(rf'(\w+)\s*=\s*bool\(opts\.get\(\s*"{opt}"', src)
            self.assertIsNotNone(am, f"{opt} is not assigned to a local in app.py")
            var_of[opt] = am.group(1)
        for opt, var in sorted(var_of.items()):
            self.assertRegex(
                gate,
                rf"\b{var}\b",
                f"{opt} (local `{var}`) is missing from the state-publish gate -- "
                f"its sensors would register in HA and never receive a state",
            )


if __name__ == "__main__":
    unittest.main()
