# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for netdiag tcp_info parsing and the throughput accumulator
(per-inode delta folding, reconnect safety, attribution, loopback exclusion)."""

import os
import struct
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import netdiag  # noqa: E402
from aviation_feeder_mqtt.netdiag import SockStat  # noqa: E402
from aviation_feeder_mqtt.throughput import ThroughputAccumulator  # noqa: E402


class ParseTcpInfo(unittest.TestCase):
    def test_reads_byte_counters_at_stable_offsets(self):
        info = bytearray(140)
        struct.pack_into("=Q", info, 120, 52590619)  # tcpi_bytes_acked
        struct.pack_into("=Q", info, 128, 4567)  # tcpi_bytes_received
        self.assertEqual(netdiag._parse_tcp_info(bytes(info)), (52590619, 4567))

    def test_too_short_returns_none(self):
        self.assertIsNone(netdiag._parse_tcp_info(b"\x00" * 100))


class Throughput(unittest.TestCase):
    def test_client_feeder_by_inode(self):
        acc = ThroughputAccumulator()
        out = acc.update(
            {"enable_planewatch": True},
            socks=[SockStat("1.2.3.4", 12345, 5, 100, 10)],
            cmd_by_pid={100: "/usr/local/sbin/pw-feeder --apikey x"},
            inode_provider=lambda pids: {5} if 100 in pids else set(),
        )
        self.assertEqual(out["planewatch"], (100, 10))

    def test_loopback_excluded(self):
        acc = ThroughputAccumulator()
        out = acc.update(
            {"enable_planewatch": True},
            socks=[SockStat("127.0.0.1", 30005, 5, 999, 999)],
            cmd_by_pid={100: "pw-feeder"},
            inode_provider=lambda pids: {5},
        )
        self.assertNotIn("planewatch", out)

    def test_delta_accumulation_and_reconnect(self):
        acc = ThroughputAccumulator()
        opts = {"enable_planewatch": True}
        cmd = {100: "pw-feeder"}
        # poll 1: socket inode 5 has shipped 100 bytes
        acc.update(opts, socks=[SockStat("1.2.3.4", 12345, 5, 100, 0)],
                   cmd_by_pid=cmd, inode_provider=lambda p: {5})
        # poll 2: same socket now 250 -> +150 -> total 250
        out = acc.update(opts, socks=[SockStat("1.2.3.4", 12345, 5, 250, 0)],
                         cmd_by_pid=cmd, inode_provider=lambda p: {5})
        self.assertEqual(out["planewatch"][0], 250)
        # poll 3: reconnect -> new inode 6, counter restarts at 50. Must ADD 50,
        # never go negative.
        out = acc.update(opts, socks=[SockStat("1.2.3.4", 12345, 6, 50, 0)],
                         cmd_by_pid=cmd, inode_provider=lambda p: {6})
        self.assertEqual(out["planewatch"][0], 300)

    def test_transient_gap_does_not_double_count(self):
        # A still-alive socket momentarily absent from the dump (e.g. an errored
        # netlink read -> []) must NOT be forgotten and re-added as a fresh delta.
        acc = ThroughputAccumulator()
        opts = {"enable_planewatch": True}
        cmd = {100: "pw-feeder"}
        acc.update(opts, socks=[SockStat("1.2.3.4", 12345, 5, 1000, 0)],
                   cmd_by_pid=cmd, inode_provider=lambda p: {5})
        # empty dump (netlink hiccup) — must not prune the still-alive inode 5
        acc.update(opts, socks=[], cmd_by_pid=cmd, inode_provider=lambda p: {5})
        out = acc.update(opts, socks=[SockStat("1.2.3.4", 12345, 5, 1500, 0)],
                         cmd_by_pid=cmd, inode_provider=lambda p: {5})
        self.assertEqual(out["planewatch"][0], 1500)  # not 2500

    def test_ipv4_mapped_loopback_excluded(self):
        acc = ThroughputAccumulator()
        out = acc.update(
            {"enable_planewatch": True},
            socks=[SockStat("::ffff:127.0.0.1", 30005, 5, 999, 999)],
            cmd_by_pid={100: "pw-feeder"},
            inode_provider=lambda pids: {5},
        )
        self.assertNotIn("planewatch", out)

    def test_non_kernel_feeder_ignored(self):
        # radarvirtuel is proc-mode (not in THROUGHPUT_KERNEL) -> no kernel bytes
        # even if it owns a socket.
        acc = ThroughputAccumulator()
        out = acc.update(
            {"enable_radarvirtuel": True},
            socks=[SockStat("9.9.9.9", 443, 7, 500, 5)],
            cmd_by_pid={200: "python3 /docker-entrypoint.py"},
            inode_provider=lambda pids: {7},
        )
        self.assertEqual(out, {})

    def test_unattributed_socket_ignored(self):
        acc = ThroughputAccumulator()
        out = acc.update(
            {"enable_planewatch": True},
            socks=[SockStat("8.8.8.8", 443, 9, 1000, 1000)],  # not a feeder
            cmd_by_pid={100: "pw-feeder"},
            inode_provider=lambda pids: {5},  # feeder owns inode 5, not 9
        )
        self.assertEqual(out, {})

    def test_same_inode_counter_regression_clamped(self):
        # A same-inode counter that goes backwards (shouldn't happen, but be
        # defensive) folds a 0 delta, never a negative one.
        acc = ThroughputAccumulator()
        opts, cmd = {"enable_planewatch": True}, {100: "pw-feeder"}
        acc.update(opts, socks=[SockStat("1.2.3.4", 1, 5, 1000, 500)],
                   cmd_by_pid=cmd, inode_provider=lambda p: {5})
        out = acc.update(opts, socks=[SockStat("1.2.3.4", 1, 5, 800, 400)],
                         cmd_by_pid=cmd, inode_provider=lambda p: {5})
        self.assertEqual(out["planewatch"], (1000, 500))  # unchanged, not negative

    def test_multiple_inodes_one_feeder_accumulate(self):
        # A feeder that owns two sockets (two inodes) sums both into its total.
        acc = ThroughputAccumulator()
        out = acc.update(
            {"enable_planewatch": True},
            socks=[SockStat("1.2.3.4", 1, 5, 100, 10),
                   SockStat("1.2.3.4", 2, 6, 50, 5)],
            cmd_by_pid={100: "pw-feeder"},
            inode_provider=lambda pids: {5, 6},
        )
        self.assertEqual(out["planewatch"], (150, 15))


if __name__ == "__main__":
    unittest.main()
