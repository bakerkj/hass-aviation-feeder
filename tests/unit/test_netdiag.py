# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for netdiag._parse_dump (the SOCK_DIAG binary parser) and that
established_sockets degrades to [] on a malformed dump (struct.error), rather
than crash-looping the publisher."""

import os
import socket
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import netdiag  # noqa: E402


def _one_ipv4_dump(ip="1.2.3.4", dport=12345, inode=99999, acked=1000, recv=2000):
    """Build one valid SOCK_DIAG_BY_FAMILY message: inet_diag_msg + an
    INET_DIAG_INFO rtattr carrying a tcp_info with the two byte counters."""
    tcp_info = bytearray(netdiag._TCPI_MIN_LEN)  # 136
    struct.pack_into("=Q", tcp_info, netdiag._TCPI_BYTES_ACKED_OFF, acked)
    struct.pack_into("=Q", tcp_info, netdiag._TCPI_BYTES_RECEIVED_OFF, recv)
    rlen = 4 + len(tcp_info)
    rtattr = struct.pack("=HH", rlen, netdiag._INET_DIAG_INFO) + bytes(tcp_info)
    rtattr += b"\x00" * ((-len(rtattr)) % 4)

    body = bytearray(netdiag._INET_DIAG_MSG_LEN)  # 72
    body[0] = socket.AF_INET                       # family
    struct.pack_into("!H", body, 6, dport)         # id.dport (network order)
    body[24:28] = socket.inet_pton(socket.AF_INET, ip)  # id.dst
    struct.pack_into("=I", body, 68, inode)        # inode
    body = bytes(body) + rtattr

    mlen = 16 + len(body)
    return struct.pack("=IHHII", mlen, netdiag._SOCK_DIAG_BY_FAMILY, 0, 0, 0) + body


class ParseDump(unittest.TestCase):
    def test_parses_one_socket(self):
        socks = netdiag._parse_dump(_one_ipv4_dump(), socket.AF_INET)
        self.assertEqual(len(socks), 1)
        s = socks[0]
        self.assertEqual(s.remote_ip, "1.2.3.4")
        self.assertEqual(s.remote_port, 12345)
        self.assertEqual(s.inode, 99999)
        self.assertEqual(s.bytes_sent, 1000)
        self.assertEqual(s.bytes_received, 2000)

    def test_truncated_and_garbage_do_not_raise(self):
        for buf in (b"", b"\x10\x00", b"\xff" * 20, b"\x00" * 40, _one_ipv4_dump()[:50]):
            self.assertIsInstance(netdiag._parse_dump(buf, socket.AF_INET), list)


class EstablishedSockets(unittest.TestCase):
    def test_degrades_on_struct_error(self):
        # A malformed dump makes _parse_dump raise struct.error; established_sockets
        # must catch it and return [] (not crash the monitoring cycle).
        with mock.patch.object(netdiag, "_dump_family", return_value=b""), \
             mock.patch.object(netdiag, "_parse_dump", side_effect=struct.error("boom")):
            self.assertEqual(netdiag.established_sockets(), [])

    def test_degrades_on_oserror(self):
        with mock.patch.object(netdiag, "_dump_family", side_effect=OSError("nope")):
            self.assertEqual(netdiag.established_sockets(), [])


if __name__ == "__main__":
    unittest.main()
