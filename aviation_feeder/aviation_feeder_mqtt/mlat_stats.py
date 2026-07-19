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
  - peer_count / good_sync %  -- pushed by the mlat *server* (`stats` message).
    Most aggregators send it; RadarBox and sdrmap never do.
  - positions/minute + aircraft-used -- computed client-side and written on the
    client's own timer by our build-time mlat-client patch (see
    patch-mlat-client.py). These populate for EVERY mlat feeder.
So the servers in MLAT_SYNC_INCAPABLE report positions/aircraft but not
peers/sync -- see MLAT_SYNC_CAPABLE."""

import json
import os
import time

MLAT_STATS_DIR = "/run/mlat-client"
# A live mlat-client rewrites its file every --stats-interval (we use 30s;
# ultrafeeder's community clients ~60s). If a file hasn't been touched in well
# over that, the mlat-client is dead/gone -> skip it so its last peers/sync don't
# republish forever. Skipping here is not the end of the story: the caller
# (app.mlat_states) treats absent stats for an ENABLED feeder as 0. This module
# has no view of whether the client process is alive; it reports only what the
# files say. The 0 is justified by mlat-client writing this file only once it
# has synced, so for an enabled feeder no stats means not syncing.
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

# Feeders whose mlat-server never sends the `stats` message, so peer_count and
# good_sync_percentage_last_hour never appear in their --stats-json. Advertising
# MLAT Peers / MLAT Sync for these publishes sensors that can never carry a
# meaningful value -- they would sit at a permanent 0 implying a sync problem
# that does not exist.
#
# The tell is the file itself: a server that pushes stats yields ~392 bytes with
# peer_count and good_sync_percentage_last_hour; one that does not yields ~199
# bytes carrying only the fields our own mlat-client patch writes client-side
# (positions_per_minute, msg_rate, aircraft_adsb_used/total, *_state). Both
# RadarBox and sdrmap produce the short form.
MLAT_SYNC_INCAPABLE = frozenset({"radarbox", "sdrmap"})

# feeder keys whose mlat-server pushes peer_count / good_sync (so the peers/sync
# sensors get values). The excluded ones still report positions/aircraft, which
# are written client-side by our mlat-client patch -- just not peers/sync.
MLAT_SYNC_CAPABLE = MLAT_CAPABLE - MLAT_SYNC_INCAPABLE


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
                continue  # dead/gone -> caller reports 0, not a stale value
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
        # every mlat feeder, including the sync-incapable ones.
        pos = data.get("positions_per_minute")
        if isinstance(pos, (int, float)):
            vals["mlat_positions_rate"] = pos
        used = data.get("aircraft_adsb_used")
        if isinstance(used, (int, float)):
            vals["mlat_aircraft"] = used
        if vals:
            out[key] = vals
    return out
