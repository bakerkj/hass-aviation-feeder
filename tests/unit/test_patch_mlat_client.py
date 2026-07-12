# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for the build-time mlat-client patch (patch-mlat-client.py).

The patch is the riskiest part of the client-side-stats feature: it edits the
base image's vendored Python by anchor-matching. These tests pin the anchors
against faithful copies of the upstream (mlat-client 0.4.2) source blocks, so if
the anchor strings drift out of sync with reality the unit suite fails here
rather than the container build failing later. They also verify idempotency and
that the patched output still compiles."""

import importlib.util
import os
import sys
import unittest

_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", "aviation_feeder", "patch-mlat-client.py"
)
_spec = importlib.util.spec_from_file_location("patch_mlat_client", _SCRIPT)
pmc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pmc)


# Faithful copies of the upstream blocks the patch anchors on (indentation is
# load-bearing -- it must match the vendored source exactly).
STATS_SRC = '''\
from mlat.client.util import monotonic_time, log


class Stats:
    def log_and_reset(self, coordinator):
        now = monotonic_time()
        elapsed = now - self.start
        log('Results:  {0:3.1f} positions/minute',
            self.mlat_positions / elapsed * 60.0)
        self.reset(now)


global_stats = Stats()
'''

JSONCLIENT_SRC = '''\
import json
import os


class JsonServerConnection:
    def handle_request(self, request):
        if 'result' in request:
            pass
        elif 'stats' in request:
            try:
                stats = request['stats']
                if self.stats_path:
                    tmp = self.stats_path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(stats, f, indent=2)
                        os.rename(tmp, self.stats_path)
            except Exception as exc:
                raise
'''


class PatchStatsPy(unittest.TestCase):
    def test_applies_once_and_compiles(self):
        out = pmc.apply_patch(
            STATS_SRC, pmc.STATS_ANCHOR, pmc.STATS_FULL_REPLACEMENT, pmc.STATS_SENTINEL
        )
        self.assertNotEqual(out, STATS_SRC)
        self.assertIn("_haf_write_client_stats", out)
        self.assertIn("positions_per_minute = self.mlat_positions", out)
        compile(out, "stats.py", "exec")  # patched source is valid Python

    def test_idempotent(self):
        once = pmc.apply_patch(
            STATS_SRC, pmc.STATS_ANCHOR, pmc.STATS_FULL_REPLACEMENT, pmc.STATS_SENTINEL
        )
        twice = pmc.apply_patch(
            once, pmc.STATS_ANCHOR, pmc.STATS_FULL_REPLACEMENT, pmc.STATS_SENTINEL
        )
        self.assertEqual(once, twice)

    def test_missing_anchor_raises(self):
        with self.assertRaises(pmc.PatchError):
            pmc.apply_patch(
                "def unrelated():\n    pass\n",
                pmc.STATS_ANCHOR, pmc.STATS_FULL_REPLACEMENT, pmc.STATS_SENTINEL,
            )


class PatchJsonClientPy(unittest.TestCase):
    def test_applies_once_and_compiles(self):
        out = pmc.apply_patch(
            JSONCLIENT_SRC, pmc.JSON_ANCHOR, pmc.JSON_REPLACEMENT, pmc.JSON_SENTINEL
        )
        self.assertNotEqual(out, JSONCLIENT_SRC)
        self.assertIn("_haf_merged.update(stats)", out)
        # merge reads the existing file before writing
        self.assertIn("json.load(_haf_rf)", out)
        compile(out, "jsonclient.py", "exec")

    def test_idempotent(self):
        once = pmc.apply_patch(
            JSONCLIENT_SRC, pmc.JSON_ANCHOR, pmc.JSON_REPLACEMENT, pmc.JSON_SENTINEL
        )
        twice = pmc.apply_patch(
            once, pmc.JSON_ANCHOR, pmc.JSON_REPLACEMENT, pmc.JSON_SENTINEL
        )
        self.assertEqual(once, twice)

    def test_missing_anchor_raises(self):
        with self.assertRaises(pmc.PatchError):
            pmc.apply_patch(
                "x = 1\n", pmc.JSON_ANCHOR, pmc.JSON_REPLACEMENT, pmc.JSON_SENTINEL
            )


class WriteClientStats(unittest.TestCase):
    """Execute the injected _haf_write_client_stats helper against a fake
    coordinator (the counting + merge-write the feature actually relies on)."""

    def _fn(self):
        ns = {}
        exec(pmc.STATS_HELPER, ns)  # noqa: S102 - the code we ship into stats.py
        return ns["_haf_write_client_stats"]

    def _coord(self, path):
        import types

        class AC:
            def __init__(self, messages, adsb_good, requested):
                self.messages, self.adsb_good, self.requested = messages, adsb_good, requested

        return types.SimpleNamespace(
            server=types.SimpleNamespace(stats_path=path, state="ready"),
            receiver=types.SimpleNamespace(state="connected"),
            aircraft={
                "a": AC(5, True, True),    # counted + used
                "b": AC(5, True, False),   # counted, not used
                "c": AC(1, True, True),    # messages < 2 -> skipped
                "d": AC(9, False, True),   # not adsb_good -> not counted as ADS-B
            },
        )

    def test_writes_and_counts(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "radarbox.json")
            self._fn()(self._coord(path), 4.5, 1000, 10.0)  # 1000 msgs / 10s = 100/s
            data = json.load(open(path, encoding="utf-8"))
            self.assertEqual(data["positions_per_minute"], 4.5)
            self.assertEqual(data["msg_rate"], 100.0)
            self.assertEqual(data["aircraft_adsb_used"], 1)   # only 'a'
            self.assertEqual(data["aircraft_adsb_total"], 2)  # 'a' + 'b'
            self.assertEqual(data["server_state"], "ready")
            self.assertEqual(data["receiver_state"], "connected")

    def test_merges_with_server_pushed_fields(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"peer_count": 42, "good_sync_percentage_last_hour": 99}, f)
            self._fn()(self._coord(path), 1.0, 60, 60.0)
            data = json.load(open(path, encoding="utf-8"))
            self.assertEqual(data["peer_count"], 42)               # server field kept
            self.assertEqual(data["good_sync_percentage_last_hour"], 99)
            self.assertEqual(data["positions_per_minute"], 1.0)    # client field added

    def test_no_stats_path_is_noop(self):
        import types
        coord = types.SimpleNamespace(server=types.SimpleNamespace(stats_path=None))
        self._fn()(coord, 1.0, 1, 1.0)  # must not raise


if __name__ == "__main__":
    unittest.main()
