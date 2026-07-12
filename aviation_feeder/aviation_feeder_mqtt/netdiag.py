# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Per-socket byte counters via NETLINK_INET_DIAG (SOCK_DIAG), pure-Python.

readsb exposes no per-connector byte accounting, and client-feeder binaries
don't report their own throughput — but the kernel tracks cumulative bytes per
TCP socket (tcpi_bytes_acked / tcpi_bytes_received in tcp_info). We query
SOCK_DIAG directly (no `ss` fork, no external dependency, no special capability
beyond the container default — verified working in-container), which returns for
each ESTABLISHED socket its remote address, its socket inode, and its tcp_info.

Attribution is done by the caller by socket inode -> /proc/<pid>/fd -> feeder
process (see throughput.py, THROUGHPUT_KERNEL feeders); the remote address is
used only to exclude loopback (the feeder's local read from readsb).

tcp_info field offsets: the struct only ever *appends* fields, so the u64
tcpi_bytes_acked (offset 120) and tcpi_bytes_received (offset 128) are stable
across every kernel that has them (>= 4.6, ~2016). We still length-guard the
read (require >= 136 bytes) and never assume a fixed total struct size, so a
kernel with a shorter/longer tcp_info degrades gracefully instead of crashing.

Because these counters are per-socket, they reset to 0 when a feeder reconnects
(new socket = new inode). ThroughputAccumulator (throughput.py) turns them into
stable per-feeder totals; this module just reports the raw current values."""

import socket
import struct
from dataclasses import dataclass

# netlink / sock_diag constants
_NETLINK_INET_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLMSG_ERROR = 2
_NLMSG_DONE = 3
_NLM_F_REQUEST = 0x01
_NLM_F_DUMP = 0x0300  # ROOT | MATCH
_TCPF_ESTABLISHED = 1 << 1
_INET_DIAG_INFO = 2

# tcp_info u64 byte-counter offsets (see module docstring).
_TCPI_BYTES_ACKED_OFF = 120
_TCPI_BYTES_RECEIVED_OFF = 128
_TCPI_MIN_LEN = _TCPI_BYTES_RECEIVED_OFF + 8  # 136

# inet_diag_msg is 4 (family/state/timer/retrans) + 48 (id) + 20 (expires,
# rqueue, wqueue, uid, inode) = 72 bytes, then rtattrs.
_INET_DIAG_MSG_LEN = 72


@dataclass(frozen=True)
class SockStat:
    remote_ip: str
    remote_port: int
    inode: int
    bytes_sent: int  # tcpi_bytes_acked
    bytes_received: int


def _parse_tcp_info(info: bytes) -> tuple[int, int] | None:
    """(bytes_acked, bytes_received) from a tcp_info blob, or None if too short."""
    if len(info) < _TCPI_MIN_LEN:
        return None
    ba = struct.unpack_from("=Q", info, _TCPI_BYTES_ACKED_OFF)[0]
    br = struct.unpack_from("=Q", info, _TCPI_BYTES_RECEIVED_OFF)[0]
    return ba, br


def _parse_dump(buf: bytes, family: int) -> list[SockStat]:
    """Parse a SOCK_DIAG dump buffer into SockStats (ESTABLISHED sockets)."""
    out: list[SockStat] = []
    off = 0
    n = len(buf)
    while off + 16 <= n:
        mlen, mtype, _flags, _seq, _pid = struct.unpack_from("=IHHII", buf, off)
        if mlen < 16 or mtype == _NLMSG_DONE:
            break
        if mtype == _SOCK_DIAG_BY_FAMILY:
            body = buf[off + 16 : off + mlen]
            if len(body) >= _INET_DIAG_MSG_LEN:
                # id: sport(2, network) dport(2, network) src(16) dst(16) if(4) cookie(8)
                dport = struct.unpack_from("!H", body, 6)[0]
                dst = body[24:40]
                inode = struct.unpack_from("=I", body, 68)[0]
                if family == socket.AF_INET:
                    remote_ip = socket.inet_ntop(socket.AF_INET, dst[:4])
                else:
                    remote_ip = socket.inet_ntop(socket.AF_INET6, dst)
                # rtattrs after the fixed inet_diag_msg
                aoff = _INET_DIAG_MSG_LEN
                bytes_sent = bytes_received = 0
                while aoff + 4 <= len(body):
                    rlen, rtype = struct.unpack_from("=HH", body, aoff)
                    if rlen < 4:
                        break
                    if rtype == _INET_DIAG_INFO:
                        parsed = _parse_tcp_info(body[aoff + 4 : aoff + rlen])
                        if parsed is not None:
                            bytes_sent, bytes_received = parsed
                    aoff += (rlen + 3) & ~3
                out.append(
                    SockStat(remote_ip, dport, inode, bytes_sent, bytes_received)
                )
        off += (mlen + 3) & ~3
    return out


def _dump_family(family: int) -> bytes:
    """Send a SOCK_DIAG dump request for ESTABLISHED TCP sockets and read it."""
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_INET_DIAG)
    # Bound the blocking recv: a dump answered with NLMSG_ERROR (e.g. IPv6
    # disabled -> the AF_INET6 dump errors, or EPERM) carries no NLMSG_DONE, so
    # without a timeout the read loop would block forever. On timeout, recv
    # raises (caught by established_sockets -> []).
    s.settimeout(2.0)
    try:
        # struct inet_diag_req_v2: family, protocol, ext, pad, states(u32) + id(48)
        req = struct.pack(
            "=BBBBI",
            family,
            socket.IPPROTO_TCP,
            1 << (_INET_DIAG_INFO - 1),
            0,
            _TCPF_ESTABLISHED,
        ) + b"\x00" * 48
        hdr = struct.pack(
            "=IHHII",
            16 + len(req),
            _SOCK_DIAG_BY_FAMILY,
            _NLM_F_REQUEST | _NLM_F_DUMP,
            1,
            0,
        )
        s.send(hdr + req)
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            # stop once this datagram ends in NLMSG_DONE
            off = 0
            done = False
            while off + 16 <= len(chunk):
                mlen, mtype, _f, _s, _p = struct.unpack_from("=IHHII", chunk, off)
                if mlen < 16:
                    break
                # NLMSG_ERROR terminates a dump too (and carries no NLMSG_DONE).
                if mtype in (_NLMSG_DONE, _NLMSG_ERROR):
                    done = True
                    break
                off += (mlen + 3) & ~3
            if done:
                break
        return buf
    finally:
        s.close()


def established_sockets() -> list[SockStat]:
    """All ESTABLISHED TCP sockets (IPv4 + IPv6) with kernel byte counters.

    Returns [] on any error (missing netlink, permission, malformed dump) so a
    monitoring cycle never crashes on socket enumeration."""
    out: list[SockStat] = []
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            out.extend(_parse_dump(_dump_family(family), family))
        except (OSError, struct.error):
            # struct.error: a malformed/truncated dump -> skip this family
            # rather than crash the whole monitoring cycle.
            continue
    return out
