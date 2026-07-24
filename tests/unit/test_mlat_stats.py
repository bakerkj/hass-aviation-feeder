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

from aviation_feeder_mqtt import mlat_stats


class ReadMlatStats(unittest.TestCase):
    def _write(self, d, name, obj):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_community_and_commercial_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            # community: named by mlat host:port
            self._write(
                d,
                "in.adsb.lol:31090.json",
                {"peer_count": 66, "good_sync_percentage_last_hour": 100},
            )
            # commercial: our explicit basename
            self._write(
                d,
                "planewatch.json",
                {"peer_count": 12, "good_sync_percentage_last_hour": 95},
            )
            out = mlat_stats.read_mlat_stats(directory=d)
            self.assertEqual(out["adsblol"], {"mlat_peers": 66, "mlat_sync": 100})
            self.assertEqual(out["planewatch"], {"mlat_peers": 12, "mlat_sync": 95})

    def test_missing_dir_is_empty(self):
        self.assertEqual(mlat_stats.read_mlat_stats(directory="/nonexistent"), {})

    def test_stale_file_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(
                d,
                "planewatch.json",
                {"peer_count": 12, "good_sync_percentage_last_hour": 95},
            )
            mtime = os.path.getmtime(os.path.join(d, "planewatch.json"))
            # far in the "future" relative to the file -> stale -> skipped
            self.assertNotIn(
                "planewatch", mlat_stats.read_mlat_stats(directory=d, now=mtime + 10000)
            )
            # fresh -> included
            self.assertIn(
                "planewatch", mlat_stats.read_mlat_stats(directory=d, now=mtime + 5)
            )

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
            self._write(
                d,
                "in.adsb.lol:31090.json",
                {
                    "peer_count": 66,
                    "good_sync_percentage_last_hour": 100,
                    "positions_per_minute": 4.5,
                    "aircraft_adsb_used": 8,
                },
            )
            out = mlat_stats.read_mlat_stats(directory=d)
            self.assertEqual(
                out["adsblol"],
                {
                    "mlat_peers": 66,
                    "mlat_sync": 100,
                    "mlat_positions_rate": 4.5,
                    "mlat_aircraft": 8,
                },
            )

    def test_radarbox_positions_without_sync(self):
        # RadarBox's server never pushes peer/sync, but the client-side patch
        # still writes positions/aircraft -> those surface, peers/sync don't.
        with tempfile.TemporaryDirectory() as d:
            self._write(
                d,
                "radarbox.json",
                {
                    "positions_per_minute": 0.0,
                    "aircraft_adsb_used": 17,
                    "server_state": "ready",
                },
            )
            out = mlat_stats.read_mlat_stats(directory=d)
            self.assertEqual(
                out["radarbox"], {"mlat_positions_rate": 0.0, "mlat_aircraft": 17}
            )
            self.assertNotIn("mlat_peers", out["radarbox"])

    def test_sync_incapable_feeders_are_capable_but_not_sync_capable(self):
        # RadarBox and sdrmap run MLAT, but their servers never push the `stats`
        # message, so peers/sync would sit unavailable forever if advertised.
        for key in ("radarbox", "sdrmap"):
            self.assertIn(key, mlat_stats.MLAT_CAPABLE, f"{key} should do MLAT")
            self.assertNotIn(key, mlat_stats.MLAT_SYNC_CAPABLE, f"{key} has no sync")
        self.assertEqual(
            mlat_stats.MLAT_SYNC_CAPABLE,
            mlat_stats.MLAT_CAPABLE - mlat_stats.MLAT_SYNC_INCAPABLE,
        )

    def test_sync_incapable_derived_from_a_stats_file_without_peer_count(self):
        """The exclusion is a claim about the file each server produces, so drive
        it from a realistic file rather than restating the constant.

        A sync-incapable server yields only the fields our own mlat-client patch
        writes client-side; a capable one adds peer_count and the sync
        percentages. Anything parsed out of the short form must not include
        mlat_peers/mlat_sync, which is exactly why those feeders are excluded."""
        short_form = {
            "positions_per_minute": 0.0,
            "msg_rate": 274.5,
            "aircraft_adsb_used": 31,
            "aircraft_adsb_total": 38,
            "receiver_state": "connected",
            "server_state": "ready",
            "client_now": 1784491744,
        }
        with tempfile.TemporaryDirectory() as d:
            for key in mlat_stats.MLAT_SYNC_INCAPABLE:
                base = mlat_stats.MLAT_STATS_BASENAMES[key]
                with open(os.path.join(d, base + ".json"), "w", encoding="utf-8") as f:
                    json.dump(short_form, f)
            out = mlat_stats.read_mlat_stats(directory=d)
            for key in mlat_stats.MLAT_SYNC_INCAPABLE:
                self.assertIn(key, out, f"{key} should still report client-side stats")
                self.assertNotIn("mlat_peers", out[key])
                self.assertNotIn("mlat_sync", out[key])
                # ...but the client-side metrics DO survive, which is why these
                # feeders stay in MLAT_CAPABLE.
                self.assertIn("mlat_aircraft", out[key])

    def test_all_basenames_map_to_known_feeder_keys(self):
        # every configured basename key is a real feeder key (community or client)
        from aviation_feeder_mqtt.feeders import COMMUNITY_FEEDERS, PROPRIETARY_FEEDERS

        known = {f[0] for f in COMMUNITY_FEEDERS} | {f[0] for f in PROPRIETARY_FEEDERS}
        for key in mlat_stats.MLAT_STATS_BASENAMES:
            self.assertIn(key, known, f"{key} is not a known feeder")


if __name__ == "__main__":
    unittest.main()
