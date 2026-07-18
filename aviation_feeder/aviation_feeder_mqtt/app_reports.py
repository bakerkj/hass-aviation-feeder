# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Per-feeder application self-reports.

Some feeders don't feed over a persistent TCP connection, so the kernel socket
counters (netdiag/throughput) can't see them and would falsely report them
"disconnected" with no throughput. Those feeders report their own status, which
we use as the authoritative source for BOTH their feeding-state and throughput:

* fr24feed feeds over **UDP** (blender.prod.fr24.io:8099/UDP) — invisible to
  TCP INET_DIAG. Its http://localhost:8754/monitor.json gives feed_status +
  num_messages (a message count, not bytes — UDP has no cumulative byte
  counter).
* pfclient (PlaneFinder) feeds over its own path — its
  http://localhost:30053/ajax/stats.php gives master_server_bytes_out/in (real
  cumulative bytes to/from PlaneFinder).
* piaware feeds over TCP (kernel-visible), but its /run/piaware/status.json is
  the only source of its MLAT + radio health (it uses fa-mlat-client).

Each report carries a "connected" bool (authoritative feeding-state for that
feeder) plus metric/attribute fields. Everything is best-effort: any
read/parse/HTTP error yields no report (the feeder then falls back to its
process/kernel signal, or shows unavailable — never a fabricated value)."""

import json
import urllib.request
from typing import Any

from .util import read_json_dict

PIAWARE_STATUS = "/run/piaware/status.json"
FR24_MONITOR_URL = "http://localhost:8754/monitor.json"
PFCLIENT_STATS_URL = "http://localhost:30053/ajax/stats.php"


def _http_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (localhost)
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def piaware_report(path: str = PIAWARE_STATUS) -> dict[str, Any] | None:
    """piaware's MLAT/radio/FlightAware-connection health (attributes only;
    piaware's feeding-state stays kernel-based since it feeds over TCP)."""
    d = read_json_dict(path)
    if not d:
        return None

    def _status(section: str) -> str | None:
        s = d.get(section)
        return s.get("status") if isinstance(s, dict) else None

    out: dict[str, Any] = {}
    for section, key in (
        ("adept", "flightaware"),
        ("mlat", "mlat"),
        ("radio", "radio"),
    ):
        v = _status(section)
        if v is not None:
            out[key] = v  # "green" | "yellow" | "red"
    if isinstance(d.get("cpu_temp_celcius"), (int, float)):
        out["cpu_temp_c"] = d["cpu_temp_celcius"]
    return out or None


def fr24_report(fetch=_http_json) -> dict[str, Any] | None:
    """fr24feed feed status + message count (its UDP feed is TCP-invisible)."""
    d = fetch(FR24_MONITOR_URL)
    if not d:
        return None
    out: dict[str, Any] = {}
    if d.get("feed_status"):
        out["feed_status"] = d["feed_status"]  # "connected" when feeding
    if d.get("feed_current_mode"):
        out["feed_mode"] = d["feed_current_mode"]
    msgs = _as_int(d.get("num_messages"))
    if msgs is not None:
        out["messages"] = msgs
    # Authoritative feeding-state for fr24: its own feed_status.
    out["connected"] = d.get("feed_status") == "connected"
    return out


def pfclient_report(fetch=_http_json) -> dict[str, Any] | None:
    """PlaneFinder bytes to/from its master server (real cumulative counters)."""
    d = fetch(PFCLIENT_STATS_URL)
    if not d:
        return None
    out: dict[str, Any] = {}
    bo = _as_int(d.get("master_server_bytes_out"))
    bi = _as_int(d.get("master_server_bytes_in"))
    if bo is not None:
        out["bytes_sent"] = bo
    if bi is not None:
        out["bytes_received"] = bi
    # NB: "connected" is NOT set here. master_server_bytes_out is a CUMULATIVE
    # counter, so >0 stays true forever after the first byte even if the feed
    # later dies. The caller (app.py) derives feeding from a positive delta
    # between cycles, mirroring PlaneFinder's own healthcheck.
    return out


def gather_reports(
    options: dict[str, Any],
    truthy,
    piaware=piaware_report,
    fr24=fr24_report,
    pfclient=pfclient_report,
) -> dict[str, dict[str, Any]]:
    """{feeder_key: report} for the enabled feeders that expose a self-report."""
    out: dict[str, dict[str, Any]] = {}
    for flag, key, fn in (
        ("enable_piaware", "piaware", piaware),
        ("enable_fr24", "fr24", fr24),
        ("enable_planefinder", "planefinder", pfclient),
    ):
        if truthy(options.get(flag)):
            r = fn()
            if r:
                out[key] = r
    return out
