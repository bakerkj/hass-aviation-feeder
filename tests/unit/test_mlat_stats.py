# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for mlat_stats.read_mlat_stats — mapping mlat-client --stats-json
files back to feeder keys, tolerating missing/half-written files."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import mlat_stats  # noqa: E402


class ReadMlatStats(unittest.TestCase):
    def _write(self, d, name, obj):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_community_and_commercial_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            # community: named by mlat host:port
            self._write(d, "in.adsb.lol:31090.json",
                        {"peer_count": 66, "good_sync_percentage_last_hour": 100})
            # commercial: our explicit basename
            self._write(d, "planewatch.json",
                        {"peer_count": 12, "good_sync_percentage_last_hour": 95})
            out = mlat_stats.read_mlat_stats(directory=d)
            self.assertEqual(out["adsblol"], {"mlat_peers": 66, "mlat_sync": 100})
            self.assertEqual(out["planewatch"], {"mlat_peers": 12, "mlat_sync": 95})

    def test_missing_dir_is_empty(self):
        self.assertEqual(mlat_stats.read_mlat_stats(directory="/nonexistent"), {})

    def test_stale_file_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "planewatch.json",
                        {"peer_count": 12, "good_sync_percentage_last_hour": 95})
            mtime = os.path.getmtime(os.path.join(d, "planewatch.json"))
            # far in the "future" relative to the file -> stale -> skipped
            self.assertNotIn(
                "planewatch", mlat_stats.read_mlat_stats(directory=d, now=mtime + 10000))
            # fresh -> included
            self.assertIn(
                "planewatch", mlat_stats.read_mlat_stats(directory=d, now=mtime + 5))

    def test_half_written_file_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "sdrmap.json", "{ partial trunc")  # invalid JSON
            self._write(d, "radarvirtuel.json", {"peer_count": 3})  # no sync field
            out = mlat_stats.read_mlat_stats(directory=d)
            self.assertNotIn("sdrmap", out)  # unparsable -> skipped
            self.assertEqual(out["radarvirtuel"], {"mlat_peers": 3})  # partial ok

    def test_client_side_positions_and_aircraft(self):
        # positions/minute + aircraft-used (written client-side by our mlat-client
        # patch) surface as mlat_positions_rate / mlat_aircraft.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "in.adsb.lol:31090.json", {
                "peer_count": 66, "good_sync_percentage_last_hour": 100,
                "positions_per_minute": 4.5, "aircraft_adsb_used": 8,
            })
            out = mlat_stats.read_mlat_stats(directory=d)
            self.assertEqual(out["adsblol"], {
                "mlat_peers": 66, "mlat_sync": 100,
                "mlat_positions_rate": 4.5, "mlat_aircraft": 8,
            })

    def test_radarbox_positions_without_sync(self):
        # RadarBox's server never pushes peer/sync, but the client-side patch
        # still writes positions/aircraft -> those surface, peers/sync don't.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "radarbox.json",
                        {"positions_per_minute": 0.0, "aircraft_adsb_used": 17,
                         "server_state": "ready"})
            out = mlat_stats.read_mlat_stats(directory=d)
            self.assertEqual(out["radarbox"],
                             {"mlat_positions_rate": 0.0, "mlat_aircraft": 17})
            self.assertNotIn("mlat_peers", out["radarbox"])

    def test_radarbox_capable_but_not_sync_capable(self):
        self.assertIn("radarbox", mlat_stats.MLAT_CAPABLE)
        self.assertNotIn("radarbox", mlat_stats.MLAT_SYNC_CAPABLE)
        # every other MLAT feeder is sync-capable
        self.assertEqual(mlat_stats.MLAT_SYNC_CAPABLE,
                         mlat_stats.MLAT_CAPABLE - {"radarbox"})

    def test_all_basenames_map_to_known_feeder_keys(self):
        # every configured basename key is a real feeder key (community or client)
        from aviation_feeder_mqtt.feeders import COMMUNITY_FEEDERS, PROPRIETARY_FEEDERS
        known = {f[0] for f in COMMUNITY_FEEDERS} | {f[0] for f in PROPRIETARY_FEEDERS}
        for key in mlat_stats.MLAT_STATS_BASENAMES:
            self.assertIn(key, known, f"{key} is not a known feeder")


if __name__ == "__main__":
    unittest.main()
