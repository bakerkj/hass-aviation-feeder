#!/usr/bin/env python3
# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
#
# Build-time patch for the ultrafeeder base image's vendored mlat-client
# (community mlat-client, ~v0.4.2). It teaches mlat-client to ALSO write its
# client-side stats -- positions/minute and aircraft-used -- into the same
# --stats-json file it already uses for server-pushed peer/sync stats.
#
# Why: mlat-client only writes --stats-json inside `elif 'stats' in request:`
# (jsonclient.py), i.e. only when the mlat *server* pushes a periodic `stats`
# message. Most aggregators do; RadarBox's server never does. But the client
# ALWAYS computes positions/minute + aircraft-used on its own timer (stats.py
# Stats.log_and_reset, called from coordinator.periodic_stats) and only logs
# them to stdout. This patch writes those numbers to the file too, on that
# client timer -- so EVERY mlat feeder (incl. RadarBox) reports them uniformly,
# and our HA publisher reads one JSON per feeder (see mlat_stats.py).
#
# The patch fails the BUILD loudly if the upstream anchors have moved (e.g. a
# base-image bump changed stats.py/jsonclient.py), rather than silently losing
# stats at runtime. It does NOT hardcode the python3.x path (Renovate/base bumps
# move it) -- it discovers the mlat/client package instead.

import glob
import sys


class PatchError(Exception):
    """Raised when an anchor no longer matches (mlat-client changed upstream)."""


def apply_patch(src, anchor, replacement, already):
    """Return patched source. Idempotent (returns src unchanged if `already` is
    present). Raises PatchError unless the anchor matches exactly once."""
    if already in src:
        return src
    n = src.count(anchor)
    if n != 1:
        raise PatchError(
            f"expected exactly 1 anchor match, found {n} -- "
            f"mlat-client changed upstream; re-verify this patch."
        )
    return src.replace(anchor, replacement)


def patch_file(path, anchor, replacement, already):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    try:
        out = apply_patch(src, anchor, replacement, already)
    except PatchError as exc:
        sys.stderr.write(f"patch-mlat-client: FAILED: {path}: {exc}\n")
        sys.exit(1)
    if out == src:
        sys.stderr.write(f"patch-mlat-client: {path} already patched, skipping\n")
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    sys.stderr.write(f"patch-mlat-client: patched {path}\n")


# --- stats.py: write client-side stats to the --stats-json file on the timer ---
STATS_ANCHOR = """        log('Results:  {0:3.1f} positions/minute',
            self.mlat_positions / elapsed * 60.0)
        self.reset(now)"""

STATS_REPLACEMENT = """        positions_per_minute = self.mlat_positions / elapsed * 60.0
        log('Results:  {0:3.1f} positions/minute', positions_per_minute)
        _haf_write_client_stats(coordinator, positions_per_minute,
                                getattr(self, 'receiver_rx_messages', 0), elapsed)
        self.reset(now)"""

STATS_HELPER = '''

def _haf_write_client_stats(coordinator, positions_per_minute, receiver_rx_messages, elapsed):
    """hass-aviation-feeder: also emit client-side MLAT stats (positions/minute,
    aircraft-used, receiver/server state) into the --stats-json file, on the
    client's own stats timer. This populates even when the server never pushes a
    'stats' message (e.g. RadarBox). Fields are merged with any server-pushed
    fields already in the file; a private temp name avoids racing jsonclient's
    own '.tmp' write. Best-effort: never let stats-writing break the client (all
    field access + arithmetic is inside the try)."""
    import json
    import os
    import time
    server = getattr(coordinator, "server", None)
    path = getattr(server, "stats_path", None)
    if not path:
        return
    try:
        msg_rate = receiver_rx_messages / elapsed if elapsed else 0.0
        adsb_used = adsb_total = 0
        for ac in coordinator.aircraft.values():
            if ac.messages < 2:
                continue
            if ac.adsb_good:
                adsb_total += 1
                if ac.requested:
                    adsb_used += 1
        # mlat_stats.read_mlat_stats only consumes positions_per_minute +
        # aircraft_adsb_used; the rest (msg_rate, aircraft_adsb_total, *_state,
        # client_now) are debug breadcrumbs written to the file but not published.
        fields = {
            "positions_per_minute": round(positions_per_minute, 1),
            "msg_rate": round(msg_rate, 1),
            "aircraft_adsb_used": adsb_used,
            "aircraft_adsb_total": adsb_total,
            "receiver_state": coordinator.receiver.state,
            "server_state": coordinator.server.state,
            "client_now": round(time.time()),
        }
        data = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data.update(fields)
        tmp = path + ".hafclient.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp, path)
    except Exception:
        pass
'''

STATS_FULL_REPLACEMENT = STATS_REPLACEMENT + STATS_HELPER
STATS_SENTINEL = "_haf_write_client_stats"

# --- jsonclient.py: server-push write must MERGE, not overwrite, so it doesn't
# clobber the client-side fields written by stats.py (and vice-versa). ---
JSON_ANCHOR = """                if self.stats_path:
                    tmp = self.stats_path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(stats, f, indent=2)
                        os.rename(tmp, self.stats_path)"""

JSON_REPLACEMENT = """                if self.stats_path:
                    _haf_merged = {}
                    try:
                        with open(self.stats_path) as _haf_rf:
                            _haf_merged = json.load(_haf_rf)
                        if not isinstance(_haf_merged, dict):
                            _haf_merged = {}
                    except Exception:
                        _haf_merged = {}
                    _haf_merged.update(stats)
                    tmp = self.stats_path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(_haf_merged, f, indent=2)
                    os.rename(tmp, self.stats_path)"""

JSON_SENTINEL = "_haf_merged"


def main():
    # fa-mlat-client (piaware) is a different, older client under /opt/fa-mlat and
    # is intentionally NOT matched here; only the community mlat/client package.
    candidates = glob.glob(
        "/usr/local/lib/python3*/dist-packages/mlat/client"
    ) + glob.glob("/usr/lib/python3*/dist-packages/mlat/client")
    if len(candidates) != 1:
        sys.stderr.write(
            f"patch-mlat-client: FAILED: expected exactly one mlat/client "
            f"package, found {candidates!r}\n"
        )
        sys.exit(1)
    pkg = candidates[0]
    patch_file(f"{pkg}/stats.py", STATS_ANCHOR, STATS_FULL_REPLACEMENT, STATS_SENTINEL)
    patch_file(f"{pkg}/jsonclient.py", JSON_ANCHOR, JSON_REPLACEMENT, JSON_SENTINEL)
    sys.stderr.write("patch-mlat-client: OK\n")


if __name__ == "__main__":
    main()
