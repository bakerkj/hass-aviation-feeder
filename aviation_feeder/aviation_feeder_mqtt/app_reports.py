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
process/kernel signal, or shows unavailable — never a fabricated value).

SECURITY — read before adding a field. Whatever a report returns is published
verbatim to MQTT as that feeder's `attributes` (see app.py's reports loop), so
these functions are the ONLY barrier between a vendor payload and the broker.
The source payloads carry station identity: piaware's status.json embeds the
FlightAware username and site id in `site_url`; rbfeeder's status.json carries
the serial number, MAC and coordinates; fr24's monitor.json carries `feed_alias`;
pfclient's /ajax/aircraft carries user_lat/user_lon. So every reader copies an
explicit allowlist of scalar fields into a fresh dict — never `return d`, never
`out.update(d)`, never pass through a nested sub-dict. Adding a field here is a
publishing decision, not a parsing one."""

import json
import urllib.request
from typing import Any

from .util import read_json_dict

PIAWARE_STATUS = "/run/piaware/status.json"
FR24_MONITOR_URL = "http://localhost:8754/monitor.json"
PFCLIENT_STATS_URL = "http://localhost:30053/ajax/stats.php"
# Written by the base image's adsbx-stats service (ADSB Exchange's own view).
ADSBX_STATS = "/run/adsbexchange-stats/new.json"

# Plausibility ceilings for pfclient's per-second counters. Not tuning knobs --
# they exist only to reject the ~2^64 underflow that client is known to emit.
# Both sit far above any real station (a busy site runs ~1k msg/s and well under
# 1 MB/s of Beast), so a legitimate reading can never trip them.
_MAX_MSG_RATE = 1_000_000  # msg/s
_MAX_BYTE_RATE = 100_000_000  # B/s

# The COMPLETE set of fields each reader is permitted to publish. gather_reports
# filters every report through this, so a reader that accidentally passes a
# vendor payload through cannot leak: undeclared keys are dropped before they
# reach MQTT. Adding a field here is the explicit, reviewable act of deciding to
# publish it — do that only after checking the vendor payload for identity data
# (see the SECURITY note above). A reader with no entry publishes nothing, so a
# new reader must register here to work at all.
REPORT_FIELDS: dict[str, frozenset[str]] = {
    "piaware": frozenset({"flightaware", "mlat", "radio", "cpu_temp_c"}),
    "fr24": frozenset(
        {
            "feed_status",
            "feed_mode",
            "messages",
            "connected",
            "portal_aircraft",
            "portal_aircraft_adsb",
            "portal_aircraft_other",
        }
    ),
    # "connected" is attached by app.py AFTER gather_reports (pfclient's feeding
    # state is a delta between cycles, which a single reader call can't see), so
    # it must be declared here or the publish-time filter would drop it.
    "planefinder": frozenset(
        {
            "bytes_sent",
            "bytes_received",
            "connected",
            "portal_message_rate",
            "portal_modeac_rate",
            "portal_receive_rate",
        }
    ),
    "adsbexchange": frozenset(
        {"portal_aircraft", "portal_aircraft_adsb", "portal_aircraft_other"}
    ),
}


def filter_report(key: str, report: dict[str, Any]) -> dict[str, Any]:
    """Drop every field not declared in REPORT_FIELDS[key].

    Applied twice on purpose: once in gather_reports, and again in app.py right
    before the reports are published as MQTT attributes. The second application
    is what makes the allowlist an actual barrier rather than a convention —
    app.py enriches reports after gather_reports returns (pfclient's derived
    `connected`), so a field added that way would otherwise reach the broker
    without ever being declared. An unregistered feeder yields nothing."""
    allowed = REPORT_FIELDS.get(key, frozenset())
    return {k: v for k, v in report.items() if k in allowed}


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
    # FR24's own view of the station: how many aircraft *it* considers tracked,
    # split ADS-B vs not. fr24feed reads the same in-container readsb we do, so
    # its total tracks ours closely -- what differs is the classification. A
    # simultaneous sample: fr24 59 = 41 adsb + 18 non_adsb, readsb 58 = 52
    # adsb_icao + 6 mode_s. Do NOT equate non_adsb with readsb's mlat count;
    # they are different measures and FR24 does not document its rule.
    # monitor.json reports every value as a string, hence _as_int.
    for field, name in (
        ("feed_num_ac_tracked", "portal_aircraft"),
        ("feed_num_ac_adsb_tracked", "portal_aircraft_adsb"),
        ("feed_num_ac_non_adsb_tracked", "portal_aircraft_other"),
    ):
        v = _as_int(d.get(field))
        if v is not None:
            out[name] = v
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
    # pfclient's own decode counters, already per-second (no rate maths needed).
    # These are the equivalents of the retired Multi-Portal add-on's
    # planefinder_mode_s_rate / _mode_ac_rate / _bandwidth sensors, which read the
    # same three fields. Every one of them is clamped: upstream pfclient has been
    # seen to underflow a per-second counter to ~2^64 (nine months of recorded
    # history show total_modeac_packets_ps averaging 1.6e14), and all three come
    # from the same client and the same payload, so none is assumed immune.
    for field, name, ceiling in (
        ("total_modes_packets_ps", "portal_message_rate", _MAX_MSG_RATE),
        ("total_modeac_packets_ps", "portal_modeac_rate", _MAX_MSG_RATE),
        ("receiver_bytes_in_ps", "portal_receive_rate", _MAX_BYTE_RATE),
    ):
        v = _as_int(d.get(field))
        if v is not None and 0 <= v < ceiling:
            out[name] = v
    # NB: "connected" is NOT set here. master_server_bytes_out is a CUMULATIVE
    # counter, so >0 stays true forever after the first byte even if the feed
    # later dies. The caller (app.py) derives feeding from a positive delta
    # between cycles, mirroring PlaneFinder's own healthcheck.
    return out


def adsbx_report(path: str = ADSBX_STATS) -> dict[str, Any] | None:
    """ADSB Exchange's own aircraft view, from the adsbx-stats service's json.

    This is the source the retired Multi-Portal add-on's adsbx_* sensors read
    (it used /run/adsbexchange-feed/status.json from its own per-portal feed
    client; ours is written by the base image's adsbx-stats service). Counts by
    `type` so the ADS-B / Mode-S / MLAT split matches how readsb classifies."""
    d = read_json_dict(path)
    if not d:
        return None
    acs = d.get("aircraft")
    if not isinstance(acs, list):
        return None
    by_type: dict[str, int] = {}
    for a in acs:
        if isinstance(a, dict):
            t = a.get("type")
            if isinstance(t, str):
                by_type[t] = by_type.get(t, 0) + 1
    out: dict[str, Any] = {"portal_aircraft": len(acs)}
    # adsb_icao(+_nt) are ADS-B; mlat is multilaterated; mode_s is Mode-S only.
    adsb = by_type.get("adsb_icao", 0) + by_type.get("adsb_icao_nt", 0)
    out["portal_aircraft_adsb"] = adsb
    out["portal_aircraft_other"] = len(acs) - adsb
    # Deliberately NOT publishing new.json's `messages`: adsbx-stats writes that
    # file from our own readsb, so the count duplicates the main device's
    # message figures rather than adding an ADSBX-specific view.
    return out


def gather_reports(
    options: dict[str, Any],
    truthy,
    piaware=piaware_report,
    fr24=fr24_report,
    pfclient=pfclient_report,
    adsbx=adsbx_report,
) -> dict[str, dict[str, Any]]:
    """{feeder_key: report} for the enabled feeders that expose a self-report."""
    out: dict[str, dict[str, Any]] = {}
    for flag, key, fn in (
        ("enable_piaware", "piaware", piaware),
        ("enable_fr24", "fr24", fr24),
        ("enable_planefinder", "planefinder", pfclient),
        ("feed_adsbexchange", "adsbexchange", adsbx),
    ):
        if truthy(options.get(flag)):
            r = fn()
            if r:
                # Enforce the publish allowlist here rather than trusting each
                # reader. app.py applies it a second time just before publishing,
                # to also cover fields attached after this point.
                filtered = filter_report(key, r)
                if filtered:
                    out[key] = filtered
    return out
