# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Per-feeder connection status.

Two classes of feeder, two signals — both read locally, no network calls:

* Community aggregators (adsb.lol, adsb.fi, ADSB Exchange, …) are readsb
  net-connectors, so readsb itself reports their connection state in its
  Prometheus export (/run/readsb/stats.prom): `readsb_net_connector_status`
  with host/port labels (positive = seconds connected; 0 or negative =
  disconnected).

* Client-binary feeders (piaware, FR24, PlaneFinder, OpenSky, RadarBox,
  ADSBHub, plane.watch, RadarVirtuel, sdrmap) run as separate processes. The
  naive signal is "is the binary running", but a running binary can still be
  feeding *nothing* — e.g. plane.watch's pw-feeder stayed up the whole time it
  was failing TLS (x509) and shipping zero bytes, so a process check showed
  green. So for feeders that hold a **persistent outbound socket** to their
  aggregator we upgrade the signal to "does the feeder process own an
  ESTABLISHED connection to a non-loopback remote" (read from /proc/net/tcp*
  + /proc/<pid>/fd) — the same feeding vs. merely-running distinction readsb
  already gives us for the community connectors.

  Two feeders (RadarVirtuel, sdrmap) POST over short-lived HTTPS rather than a
  held-open socket, so a point-in-time socket check would false-negative every
  time it lands between POSTs. Those fall back to the process-running signal
  (mode "proc"); it's the honest best-effort for a periodic-POST feeder.

Only feeders the user has enabled in options are reported.
"""

import glob
import os
import re
from collections.abc import Callable, Iterable
from typing import Any

STATS_PROM = "/run/readsb/stats.prom"
NET_TCP_PATHS = ("/proc/net/tcp", "/proc/net/tcp6")
_TCP_ESTABLISHED = "01"  # st column value for an ESTABLISHED socket

# Match the label block + value, then pull host/port out of the block
# independently so we don't depend on readsb's label ordering.
_CONNECTOR_RE = re.compile(r"readsb_net_connector_status\{([^}]*)\}\s+(-?\d+)")
_LABEL_HOST = re.compile(r'host="([^"]+)"')
_LABEL_PORT = re.compile(r'port="([^"]+)"')


# key, friendly name, option flag, adsb connector host+port (matches the
# add_aggregator table in 00-haos-options).
COMMUNITY_FEEDERS: list[tuple[str, str, str, str, int]] = [
    ("adsblol", "adsb.lol", "feed_adsblol", "in.adsb.lol", 30004),
    ("adsbfi", "adsb.fi", "feed_adsbfi", "feed.adsb.fi", 30004),
    ("airplaneslive", "airplanes.live", "feed_airplaneslive", "feed.airplanes.live", 30004),
    ("planespotters", "Planespotters", "feed_planespotters", "feed.planespotters.net", 30004),
    ("theairtraffic", "TheAirTraffic", "feed_theairtraffic", "feed.theairtraffic.com", 30004),
    ("avdelphi", "AVDelphi", "feed_avdelphi", "data.avdelphi.com", 24999),
    ("flyitaly", "Fly Italy ADSB", "feed_flyitaly", "dati.flyitalyadsb.com", 4905),
    ("adsbitalia", "ADSBItalia", "feed_adsbitalia", "feed.adsbitalia.it", 31108),
    ("adsbexchange", "ADS-B Exchange", "feed_adsbexchange", "feed1.adsbexchange.com", 30004),
    ("adsbone", "adsb.one", "feed_adsbone", "feed.adsb.one", 64004),
    ("hpradar", "HpRadar", "feed_hpradar", "skyfeed.hpradar.com", 30004),
]

# key, friendly name, option flag, /proc cmdline token identifying the running
# feeder binary, and the feeding-detection mode:
#   "conn"   — an ESTABLISHED non-loopback TCP socket owned by the process
#              (persistent-TCP feeders; also the source of kernel throughput;
#              catches running-but-not-feeding, e.g. pw-feeder x509);
#   "report" — the feeder's own status endpoint (fr24 feeds UDP, pfclient feeds
#              via its own path — both TCP-invisible, so app_reports is
#              authoritative for connected/throughput);
#   "proc"   — the binary is running (periodic-POST feeders with no held socket
#              and no status endpoint).
PROPRIETARY_FEEDERS: list[tuple[str, str, str, str, str]] = [
    ("piaware", "FlightAware", "enable_piaware", "piaware", "conn"),
    ("fr24", "FlightRadar24", "enable_fr24", "fr24feed", "report"),
    ("planefinder", "PlaneFinder", "enable_planefinder", "pfclient", "report"),
    ("opensky", "OpenSky", "enable_opensky", "openskyd", "conn"),
    ("radarbox", "AirNav RadarBox", "enable_radarbox", "rbfeeder", "conn"),
    ("adsbhub", "ADSBHub", "enable_adsbhub", "adsbhub", "conn"),
    ("planewatch", "plane.watch", "enable_planewatch", "pw-feeder", "conn"),
    ("radarvirtuel", "RadarVirtuel", "enable_radarvirtuel", "docker-entrypoint.py", "proc"),
    ("sdrmap", "sdrmap", "enable_sdrmap", "sdrmapfeeder", "proc"),
    # radar1090: persistent Beast client to 1090MHz UK; token "sbin/radar" is
    # specific (won't collide with radarvirtuel/radarbox cmdlines). ADS-B only.
    ("uk1090", "1090MHz UK", "enable_uk1090", "sbin/radar", "conn"),
]

# Client feeders whose byte throughput comes from the kernel's per-socket
# counters (persistent TCP, inode-attributed — the "conn"-mode feeders). fr24
# and pfclient report their own throughput (app_reports); radarvirtuel/sdrmap
# POST over short-lived connections and community aggregators aren't split
# per-connector by readsb, so neither gets a byte sensor.
THROUGHPUT_KERNEL = frozenset({"piaware", "planewatch", "opensky", "adsbhub", "radarbox", "uk1090"})


def read_connector_status(path: str = STATS_PROM) -> dict[str, int]:
    """Parse readsb_net_connector_status lines into {"host:port": value}."""
    out: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return out
    for labels, val in _CONNECTOR_RE.findall(text):
        h = _LABEL_HOST.search(labels)
        p = _LABEL_PORT.search(labels)
        if not (h and p):
            continue
        try:
            out[f"{h.group(1)}:{p.group(1)}"] = int(val)
        except ValueError:
            continue
    return out


def _hex_addr_is_local(addr_hex: str) -> bool:
    """True if a /proc/net/tcp{,6} hex address is loopback or unspecified.

    Addresses are stored little-endian: IPv4 is 8 hex chars (4 bytes),
    IPv6 is 32 hex chars (four little-endian 32-bit words)."""
    try:
        raw = bytes.fromhex(addr_hex)
    except ValueError:
        return False
    if len(raw) == 4:
        ip = raw[::-1]  # little-endian -> network order
        return ip[0] == 127 or ip == b"\x00\x00\x00\x00"  # 127/8 or 0.0.0.0
    if len(raw) == 16:
        ip = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        if ip == b"\x00" * 16 or ip == b"\x00" * 15 + b"\x01":  # :: or ::1
            return True
        if ip[:12] == b"\x00" * 10 + b"\xff\xff":  # IPv4-mapped ::ffff:a.b.c.d
            return ip[12] == 127 or ip[12:] == b"\x00\x00\x00\x00"
        return False
    return False


def parse_established(text: str) -> set[int]:
    """Socket inodes of ESTABLISHED connections to a non-loopback remote.

    `text` is the contents of a /proc/net/tcp{,6} file. Columns (after the
    header): sl, local_address, rem_address, st, …, uid, timeout, inode."""
    inodes: set[int] = set()
    for line in text.splitlines()[1:]:  # skip header
        fields = line.split()
        if len(fields) < 10:
            continue
        if fields[3] != _TCP_ESTABLISHED:
            continue
        rem_hex = fields[2].split(":", 1)[0]
        if _hex_addr_is_local(rem_hex):
            continue
        try:
            inodes.add(int(fields[9]))
        except ValueError:
            continue
    return inodes


def read_established_inodes(paths: Iterable[str] = NET_TCP_PATHS) -> set[int]:
    """Union of ESTABLISHED-to-remote socket inodes across the tcp/tcp6 tables."""
    inodes: set[int] = set()
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        inodes |= parse_established(text)
    return inodes


def running_cmdlines_by_pid() -> dict[int, str]:
    """{pid: cmdline} for every process, NUL->space, for substring matching.

    Skips s6 supervisors: an idled feeder still has an "s6-supervise <service>"
    process whose cmdline embeds the service name, which would false-match a
    feeder token even though the real binary was replaced by `sleep infinity`.
    Only actual running feeder binaries should count."""
    out: dict[int, str] = {}
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(p, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if cmd.startswith("s6-supervise "):
            continue
        try:
            pid = int(p.rsplit("/", 2)[1])
        except (IndexError, ValueError):
            continue
        out[pid] = cmd
    return out


def socket_inodes_for_pids(pids: Iterable[int]) -> set[int]:
    """Socket inodes owned by the given pids (from /proc/<pid>/fd symlinks)."""
    inodes: set[int] = set()
    for pid in pids:
        for fd in glob.glob(f"/proc/{pid}/fd/*"):
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                try:
                    inodes.add(int(target[8:-1]))
                except ValueError:
                    continue
    return inodes


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def compute_feeder_status(
    options: dict[str, Any],
    connectors: dict[str, int] | None = None,
    cmd_by_pid: dict[int, str] | None = None,
    established: set[int] | None = None,
    inode_provider: Callable[[Iterable[int]], set[int]] = socket_inodes_for_pids,
    reports: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, str, bool]]:
    """Return [(key, friendly_name, connected)] for every ENABLED feeder.

    `connected` means "actually feeding", best-effort per class:
      * community feeders — readsb reports the net-connector up (stats.prom);
      * "conn" client feeders — the feeder process owns an ESTABLISHED
        connection to a non-loopback remote (running *and* feeding);
      * "proc" client feeders — the binary is running (periodic-POST feeders
        hold no persistent socket to probe)."""
    if connectors is None:
        connectors = read_connector_status()
    if cmd_by_pid is None:
        cmd_by_pid = running_cmdlines_by_pid()
    if established is None:
        established = read_established_inodes()

    out: list[tuple[str, str, bool]] = []

    for key, name, flag, host, port in COMMUNITY_FEEDERS:
        if not _truthy(options.get(flag)):
            continue
        status = connectors.get(f"{host}:{port}")
        # readsb reports positive seconds-connected when up and 0/negative when
        # down (matches compute_feeder_uptime's `> 0`). Unknown (metric absent,
        # e.g. stats.prom not written yet) -> not-yet-connected, updates next cycle.
        out.append((key, name, status is not None and status > 0))

    for key, name, flag, token, mode in PROPRIETARY_FEEDERS:
        if not _truthy(options.get(flag)):
            continue
        pids = [pid for pid, cmd in cmd_by_pid.items() if token in cmd]
        if mode == "report":
            # The feeder's own status endpoint is authoritative (TCP-invisible
            # feed). Fall back to process-running only if the endpoint is
            # unreachable, so an endpoint hiccup doesn't false-report "down".
            rep = (reports or {}).get(key)
            if rep is not None and "connected" in rep:
                out.append((key, name, bool(rep["connected"])))
            else:
                out.append((key, name, bool(pids)))
            continue
        if not pids:
            # binary not running (gate idled it, or it died) -> not feeding.
            out.append((key, name, False))
            continue
        if mode == "proc":
            out.append((key, name, True))
        else:  # "conn": running is necessary but not sufficient — need a socket
            feeding = bool(inode_provider(pids) & established)
            out.append((key, name, feeding))

    return out


def _process_uptime_s(pid: int) -> float | None:
    """Seconds since a process started, from /proc/uptime and /proc/<pid>/stat
    (field 22, starttime in clock ticks). None on any read/parse error."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            up = float(f.read().split()[0])
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        # comm (field 2) is parenthesised and may contain spaces -> split after
        # the last ')'. Remaining fields start at field 3; starttime is field 22.
        fields = data[data.rfind(b")") + 2 :].split()
        start_ticks = int(fields[19])
        hz = os.sysconf("SC_CLK_TCK") or 100
        return up - start_ticks / hz
    except (OSError, ValueError, IndexError):
        return None


def compute_feeder_uptime(
    options: dict[str, Any],
    connectors: dict[str, int] | None = None,
    cmd_by_pid: dict[int, str] | None = None,
    uptime_provider=_process_uptime_s,
) -> dict[str, int]:
    """{feeder_key: uptime_seconds} for every ENABLED feeder that has one.

    Community aggregators: readsb reports connection-seconds as the
    net_connector_status value. Client feeders: the feeder process's own uptime
    (longest-running pid if several). Resets on reconnect/restart, so it's a
    measurement, not a monotonic total."""
    if connectors is None:
        connectors = read_connector_status()
    if cmd_by_pid is None:
        cmd_by_pid = running_cmdlines_by_pid()

    out: dict[str, int] = {}
    for key, _name, flag, host, port in COMMUNITY_FEEDERS:
        if not _truthy(options.get(flag)):
            continue
        v = connectors.get(f"{host}:{port}")
        if isinstance(v, int) and v > 0:
            out[key] = v
    for key, _name, flag, token, _mode in PROPRIETARY_FEEDERS:
        if not _truthy(options.get(flag)):
            continue
        pids = [pid for pid, cmd in cmd_by_pid.items() if token in cmd]
        ups = [u for u in (uptime_provider(pid) for pid in pids) if u is not None and u >= 0]
        if ups:
            out[key] = int(max(ups))
    return out
