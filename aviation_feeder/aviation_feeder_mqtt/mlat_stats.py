# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Per-feeder MLAT sync from mlat-client `--stats-json` files.

Every mlat-client run with `--stats-json PATH` writes a small JSON status file
(peer_count, good/bad sync %, outlier %) refreshed on `--stats-interval`. The
ultrafeeder community mlat-clients already do this, writing
`/run/mlat-client/<mlat_host>:<mlat_port>.json`; our three commercial-feeder
mlat run scripts (planewatch/sdrmap/radarvirtuel) pass `--stats-json` with an
explicit basename. This module reads those files and maps each back to its
feeder key. (piaware uses fa-mlat-client, not this client, so its MLAT status
comes from piaware's own status.json.)

Two classes of stat live in these files:
  - peer_count / good_sync %  -- pushed by the mlat *server* (`stats` message);
    every aggregator sends it EXCEPT RadarBox, whose server never does.
  - positions/minute + aircraft-used -- computed client-side and written on the
    client's own timer by our build-time mlat-client patch (see
    patch-mlat-client.py). These populate for EVERY mlat feeder, incl. RadarBox.
So RadarBox reports positions/aircraft but not peers/sync (see MLAT_SYNC_CAPABLE)."""

import json
import os
import time

MLAT_STATS_DIR = "/run/mlat-client"
# A live mlat-client rewrites its file every --stats-interval (we use 30s;
# ultrafeeder's community clients ~60s). If a file hasn't been touched in well
# over that, the mlat-client is dead/gone -> skip it so its last peers/sync don't
# republish forever (the HA sensor then expires to unavailable, which is honest).
_STALE_AFTER_S = 180.0

# feeder_key -> the mlat-client --stats-json basename (no .json). Community
# names are "<mlat_host>:<mlat_port>" exactly as ultrafeeder passes them (must
# match the add_aggregator mlat host/port in 00-haos-options); the three
# commercial feeders use our explicit names (set in their run scripts).
MLAT_STATS_BASENAMES: dict[str, str] = {
    "adsblol": "in.adsb.lol:31090",
    "adsbfi": "feed.adsb.fi:31090",
    "airplaneslive": "feed.airplanes.live:31090",
    "planespotters": "mlat.planespotters.net:31090",
    "theairtraffic": "feed.theairtraffic.com:31090",
    "flyitaly": "dati.flyitalyadsb.com:30100",
    "adsbitalia": "mlat.adsbitalia.it:41113",
    "adsbexchange": "feed.adsbexchange.com:31090",
    "adsbone": "feed.adsb.one:64006",
    "hpradar": "skyfeed.hpradar.com:31090",
    "planewatch": "planewatch",
    "sdrmap": "sdrmap",
    "radarvirtuel": "radarvirtuel",
    "radarbox": "radarbox",  # written by the rbfeeder-mlat shim's --stats-json
}

# feeder keys that have MLAT at all (avdelphi and adsbhub have none).
MLAT_CAPABLE = frozenset(MLAT_STATS_BASENAMES)

# feeder keys whose mlat-server pushes peer_count / good_sync (so the peers/sync
# sensors get values). Every MLAT feeder EXCEPT RadarBox, whose server never
# sends the `stats` message -- RadarBox still reports positions/aircraft (written
# client-side by our mlat-client patch), just not peers/sync.
MLAT_SYNC_CAPABLE = MLAT_CAPABLE - {"radarbox"}


def read_mlat_stats(
    directory: str = MLAT_STATS_DIR,
    basenames: dict[str, str] = MLAT_STATS_BASENAMES,
    now: float | None = None,
) -> dict[str, dict[str, float | int]]:
    """{feeder_key: {"mlat_peers": N, "mlat_sync": pct}} for each feeder whose
    stats file exists, is fresh, and parses. Missing/half-written/stale files
    are skipped."""
    if now is None:
        now = time.time()
    out: dict[str, dict[str, float | int]] = {}
    for key, base in basenames.items():
        path = os.path.join(directory, base + ".json")
        try:
            if now - os.path.getmtime(path) > _STALE_AFTER_S:
                continue  # mlat-client dead/gone -> let the sensor expire
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        vals: dict[str, float | int] = {}
        peers = data.get("peer_count")
        if isinstance(peers, (int, float)):
            vals["mlat_peers"] = peers
        sync = data.get("good_sync_percentage_last_hour")
        if isinstance(sync, (int, float)):
            vals["mlat_sync"] = sync
        # Client-side stats (written by our mlat-client patch) -- present for
        # every mlat feeder incl. RadarBox.
        pos = data.get("positions_per_minute")
        if isinstance(pos, (int, float)):
            vals["mlat_positions_rate"] = pos
        used = data.get("aircraft_adsb_used")
        if isinstance(used, (int, float)):
            vals["mlat_aircraft"] = used
        if vals:
            out[key] = vals
    return out
