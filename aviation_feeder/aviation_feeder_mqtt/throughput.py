# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Per-feeder throughput from the kernel's per-socket byte counters (netdiag.py),
turned into stable, monotonic per-feeder totals.

Only the persistent-TCP client feeders are measured this way (feeders.py's
THROUGHPUT_KERNEL): their feed rides an ESTABLISHED TCP socket we can attribute
by inode -> /proc/<pid>/fd. fr24 (UDP) and pfclient report their own throughput
(app_reports.py); community aggregators aren't split per-connector by readsb and
radarvirtuel/sdrmap POST over short-lived connections — none get a kernel byte
sensor.

The kernel's tcpi_bytes_acked/received are per-SOCKET and restart at 0 when a
feeder reconnects (new socket = new inode), so a naive current-minus-previous
goes negative. This tracks each inode's last-seen value and folds only the
non-negative delta into a per-feeder running total — monotonic across reconnects
(correct for an HA total_increasing sensor). Loopback is excluded so a feeder's
local readsb read isn't counted as feed traffic."""

import ipaddress
from collections.abc import Iterable

from .feeders import (
    PROPRIETARY_FEEDERS,
    THROUGHPUT_KERNEL,
    _truthy,
    running_cmdlines_by_pid,
    socket_inodes_for_pids,
)
from .netdiag import SockStat, established_sockets


def _is_loopback(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) — a feeder reading readsb over a
    # dual-stack v6 socket. is_loopback only follows the mapping on Python 3.13+,
    # so check it explicitly for older interpreters.
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


class ThroughputAccumulator:
    def __init__(self) -> None:
        self._last: dict[int, tuple[int, int]] = {}  # inode -> (sent, recv)
        self._totals: dict[str, list[int]] = {}  # feeder_key -> [sent, recv]

    def _inode_to_key(self, options, cmd_by_pid, inode_provider) -> dict[int, str]:
        """Map socket inodes -> feeder key for ENABLED kernel-throughput feeders."""
        out: dict[int, str] = {}
        for key, _name, flag, token, _mode in PROPRIETARY_FEEDERS:
            if key not in THROUGHPUT_KERNEL or not _truthy(options.get(flag)):
                continue
            pids = [pid for pid, cmd in cmd_by_pid.items() if token in cmd]
            for inode in inode_provider(pids):
                out[inode] = key
        return out

    def update(
        self,
        options: dict,
        socks: Iterable[SockStat] | None = None,
        cmd_by_pid: dict[int, str] | None = None,
        inode_provider=socket_inodes_for_pids,
    ) -> dict[str, tuple[int, int]]:
        """Fold current socket counters into per-feeder totals.

        Returns {feeder_key: (bytes_sent_total, bytes_received_total)} for every
        kernel-throughput feeder that has been attributed a socket this process
        lifetime."""
        if socks is None:
            socks = established_sockets()
        if cmd_by_pid is None:
            cmd_by_pid = running_cmdlines_by_pid()
        socks = list(socks)

        inode_to_key = self._inode_to_key(options, cmd_by_pid, inode_provider)

        for s in socks:
            if _is_loopback(s.remote_ip):
                continue
            key = inode_to_key.get(s.inode)
            if key is None:
                continue
            prev = self._last.get(s.inode)
            if prev is None:
                d_sent, d_recv = s.bytes_sent, s.bytes_received
            else:
                d_sent = max(0, s.bytes_sent - prev[0])
                d_recv = max(0, s.bytes_received - prev[1])
            self._last[s.inode] = (s.bytes_sent, s.bytes_received)
            total = self._totals.setdefault(key, [0, 0])
            total[0] += d_sent
            total[1] += d_recv

        # Forget only inodes whose SOCKET is actually gone (not present in this
        # dump), so a reused inode number starts fresh. Prune against every
        # present socket inode, NOT just the attributed ones — else a socket that
        # momentarily isn't attributed (pid unreadable one cycle) would be
        # forgotten and its whole counter re-added as a "new" delta next cycle.
        # Skip pruning entirely on an empty dump (an errored netlink read) so a
        # still-alive socket is never dropped.
        if socks:
            present = {s.inode for s in socks}
            for inode in list(self._last):
                if inode not in present:
                    del self._last[inode]

        return {k: (v[0], v[1]) for k, v in self._totals.items()}
