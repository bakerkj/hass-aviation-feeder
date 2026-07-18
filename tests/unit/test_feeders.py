# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for aviation_feeder_mqtt.feeders — the feeding-state logic.

Run from the repo root:  python3 -m unittest discover -s tests/unit
(no third-party deps; stdlib unittest only)."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import feeders  # noqa: E402


# A tiny /proc/net/tcp sample: header + three sockets.
#   inode 111111 — ESTABLISHED to 192.168.1.1:12345 (remote)   -> counts
#   inode 222222 — ESTABLISHED to 127.0.0.1:8080  (loopback)   -> excluded
#   inode 333333 — TIME_WAIT   to 192.168.1.1:80    (st != 01) -> excluded
_PROC_NET_TCP = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    "   0: 0100007F:A2C1 0101A8C0:3039 01 00000000:00000000 00:00000000 00000000  1000        0 111111 1 x\n"
    "   1: 0100007F:1F41 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 222222 1 x\n"
    "   2: 0100007F:A2C2 0101A8C0:0050 06 00000000:00000000 00:00000000 00000000  1000        0 333333 1 x\n"
)


class HexAddrIsLocal(unittest.TestCase):
    def test_ipv4_loopback(self):
        self.assertTrue(feeders._hex_addr_is_local("0100007F"))  # 127.0.0.1

    def test_ipv4_unspecified(self):
        self.assertTrue(feeders._hex_addr_is_local("00000000"))  # 0.0.0.0

    def test_ipv4_remote(self):
        self.assertFalse(feeders._hex_addr_is_local("0101A8C0"))  # 192.168.1.1

    def test_ipv6_loopback(self):
        # ::1 network order is 15 zero bytes + 0x01, stored as four
        # little-endian 32-bit words -> "00…000001000000".
        raw = b"\x00" * 15 + b"\x01"
        le = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        self.assertTrue(feeders._hex_addr_is_local(le.hex()))

    def test_ipv6_mapped_loopback(self):
        # ::ffff:127.0.0.1 -> network order 00..00 ffff 7f000001,
        # stored as four little-endian words.
        raw = b"\x00" * 10 + b"\xff\xff" + bytes([127, 0, 0, 1])
        le = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        self.assertTrue(feeders._hex_addr_is_local(le.hex()))

    def test_garbage(self):
        self.assertFalse(feeders._hex_addr_is_local("nothex"))


class ParseEstablished(unittest.TestCase):
    def test_only_remote_established(self):
        self.assertEqual(feeders.parse_established(_PROC_NET_TCP), {111111})

    def test_empty(self):
        self.assertEqual(feeders.parse_established(""), set())


class ComputeFeederStatus(unittest.TestCase):
    def _status(self, options, **kw):
        kw.setdefault("connectors", {})
        kw.setdefault("cmd_by_pid", {})
        kw.setdefault("established", set())
        kw.setdefault("inode_provider", lambda pids: set())
        return {
            k: connected
            for k, _n, connected in feeders.compute_feeder_status(options, **kw)
        }

    # --- community aggregators (readsb connector status) ---
    def test_community_up(self):
        s = self._status({"feed_adsblol": True}, connectors={"in.adsb.lol:30004": 1})
        self.assertTrue(s["adsblol"])

    def test_community_down(self):
        s = self._status({"feed_adsblol": True}, connectors={"in.adsb.lol:30004": 0})
        self.assertFalse(s["adsblol"])

    def test_community_down_negative(self):
        # readsb reports a NEGATIVE status when a connector is down; must read as
        # not-connected (the bug was `!= 0`, which treated -30 as connected).
        s = self._status({"feed_adsblol": True}, connectors={"in.adsb.lol:30004": -30})
        self.assertFalse(s["adsblol"])

    def test_community_absent_metric(self):
        s = self._status({"feed_adsblol": True}, connectors={})
        self.assertFalse(s["adsblol"])

    def test_disabled_excluded(self):
        s = self._status({"feed_adsblol": False})
        self.assertNotIn("adsblol", s)

    # --- conn-mode client feeders (established-socket = feeding) ---
    def test_conn_running_and_feeding(self):
        s = self._status(
            {"enable_planewatch": True},
            cmd_by_pid={100: "/usr/local/sbin/pw-feeder --apikey x"},
            established={111111},
            inode_provider=lambda pids: {111111} if 100 in pids else set(),
        )
        self.assertTrue(s["planewatch"])

    def test_conn_running_but_not_feeding(self):
        # pw-feeder up but no established remote socket (the x509 case).
        s = self._status(
            {"enable_planewatch": True},
            cmd_by_pid={100: "/usr/local/sbin/pw-feeder --apikey x"},
            established=set(),
            inode_provider=lambda pids: set(),
        )
        self.assertFalse(s["planewatch"])

    def test_conn_not_running(self):
        s = self._status({"enable_planewatch": True}, cmd_by_pid={})
        self.assertFalse(s["planewatch"])

    def test_conn_ignores_s6_supervise(self):
        # running_cmdlines_by_pid already strips s6-supervise; here just confirm
        # a bare supervisor-name match without a pid doesn't feed.
        s = self._status(
            {"enable_planewatch": True},
            cmd_by_pid={},  # gate idled it: no pw-feeder pid
            established={111111},
            inode_provider=lambda pids: {111111},
        )
        self.assertFalse(s["planewatch"])

    # --- report-mode feeders (fr24 UDP / pfclient: own status endpoint) ---
    def test_report_connected(self):
        s = self._status({"enable_fr24": True}, reports={"fr24": {"connected": True}})
        self.assertTrue(s["fr24"])

    def test_report_disconnected(self):
        # fr24's binary is up, but its own status says not feeding -> off.
        s = self._status(
            {"enable_fr24": True},
            cmd_by_pid={100: "fr24feed"},
            reports={"fr24": {"connected": False}},
        )
        self.assertFalse(s["fr24"])

    def test_report_endpoint_down_falls_back_to_process(self):
        # No report (endpoint unreachable) -> fall back to process-running.
        s = self._status(
            {"enable_fr24": True}, cmd_by_pid={100: "fr24feed"}, reports={}
        )
        self.assertTrue(s["fr24"])

    def test_report_endpoint_down_not_running(self):
        s = self._status({"enable_fr24": True}, cmd_by_pid={}, reports={})
        self.assertFalse(s["fr24"])

    # --- proc-mode client feeders (periodic POST -> process-running signal) ---
    def test_proc_running(self):
        s = self._status(
            {"enable_sdrmap": True},
            cmd_by_pid={200: "/bin/bash /usr/lib/sdrmapfeeder/sdrmapfeeder.sh"},
        )
        self.assertTrue(s["sdrmap"])

    def test_proc_not_running(self):
        s = self._status({"enable_sdrmap": True}, cmd_by_pid={})
        self.assertFalse(s["sdrmap"])

    def test_proc_ignores_missing_socket(self):
        # proc-mode must NOT require an established socket.
        s = self._status(
            {"enable_radarvirtuel": True},
            cmd_by_pid={201: "python3 /docker-entrypoint.py"},
            established=set(),
            inode_provider=lambda pids: set(),
        )
        self.assertTrue(s["radarvirtuel"])


class ComputeFeederUptime(unittest.TestCase):
    def test_community_connect_seconds(self):
        out = feeders.compute_feeder_uptime(
            {"feed_adsblol": True},
            connectors={"in.adsb.lol:30004": 5509},
            cmd_by_pid={},
        )
        self.assertEqual(out["adsblol"], 5509)

    def test_community_down_no_uptime(self):
        out = feeders.compute_feeder_uptime(
            {"feed_adsblol": True},
            connectors={"in.adsb.lol:30004": 0},
            cmd_by_pid={},
        )
        self.assertNotIn("adsblol", out)

    def test_client_process_uptime_longest_pid(self):
        out = feeders.compute_feeder_uptime(
            {"enable_planewatch": True},
            connectors={},
            cmd_by_pid={100: "pw-feeder", 101: "pw-feeder helper"},
            uptime_provider=lambda pid: {100: 50.0, 101: 123.9}.get(pid),
        )
        self.assertEqual(out["planewatch"], 123)  # max, floored to int

    def test_client_not_running(self):
        out = feeders.compute_feeder_uptime(
            {"enable_planewatch": True}, connectors={}, cmd_by_pid={}
        )
        self.assertNotIn("planewatch", out)


class ReadConnectorStatus(unittest.TestCase):
    def _parse(self, text):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".prom")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        try:
            return feeders.read_connector_status(path)
        finally:
            os.unlink(path)

    def test_label_order_independent(self):
        host_first = (
            'readsb_net_connector_status{host="feed.adsb.fi",port="30004"} 42\n'
        )
        port_first = (
            'readsb_net_connector_status{port="30004",host="feed.adsb.fi"} 42\n'
        )
        self.assertEqual(self._parse(host_first), {"feed.adsb.fi:30004": 42})
        self.assertEqual(self._parse(port_first), {"feed.adsb.fi:30004": 42})

    def test_negative_value_parsed(self):
        self.assertEqual(
            self._parse('readsb_net_connector_status{host="h",port="1"} -30\n'),
            {"h:1": -30},
        )


if __name__ == "__main__":
    unittest.main()
