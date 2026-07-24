# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Mode S Downlink-Format breakdown, decoded from readsb's Beast output.

readsb does not report a per-DF message breakdown anywhere: it is absent from
stats.json and stats.prom, and the API port serves aircraft rather than stats.
The only other in-container sources are rbfeeder and pfclient, which would tie a
receiver-level statistic to whether a particular commercial feeder is enabled.
So we count it ourselves off the Beast stream readsb always exposes.

DF is the first five bits of a Mode S message: ``payload[0] >> 3``.

Beast framing::

    0x1a <type> <6-byte MLAT timestamp> <1-byte signal> <payload>
    type 0x31 Mode A/C   payload  2 bytes
    type 0x32 Mode-S short        7 bytes
    type 0x33 Mode-S long        14 bytes

**The escaping is the whole difficulty.** A literal 0x1a anywhere after the type
byte is doubled (``0x1a 0x1a``). A parser that ignores this loses frame sync and
produces a roughly uniform spread across DF0..DF31 -- which is physically
impossible, since real traffic is dominated by DF17/DF11/DF0. If a change here
ever yields a flat distribution, that is the bug.

Reading is done on a daemon thread because the Beast stream is continuous while
the publisher works on a timer: the thread keeps a running tally, and the publish
loop takes a snapshot each cycle and converts it to per-second rates.

Summed across all DFs these rates equal readsb's own ``last1min.messages``, so
this is a breakdown of the same message population readsb reports -- not a
separate or partial view of the stream.
"""

import socket
import threading
import time
from typing import Any

BEAST_HOST = "127.0.0.1"
BEAST_PORT = 30005

_ESC = 0x1A
# Beast frame type -> payload length in bytes.
_PAYLOAD_LEN = {0x31: 2, 0x32: 7, 0x33: 14}
_HEADER_LEN = 6 + 1  # MLAT timestamp + signal level

# Mode A/C frames carry no DF, so they are tallied under this key instead.
MODEAC_KEY = "modeac"


def parse_frames(buf: bytes) -> tuple[list[tuple[int, bytes]], int]:
    """Split a Beast byte stream into ``[(frame_type, payload)]``.

    Returns the frames plus the number of bytes consumed, so the caller can keep
    the trailing partial frame and prepend it to the next read. Unescapes ``0x1a
    0x1a`` while reading; a lone 0x1a means the current frame was truncated and a
    new one starts there, so the partial frame is abandoned rather than
    mis-parsed.
    """
    frames: list[tuple[int, bytes]] = []
    n = len(buf)
    i = 0
    consumed = 0
    while i < n:
        if buf[i] != _ESC:
            i += 1
            continue
        if i + 1 >= n:
            break  # need the type byte
        ftype = buf[i + 1]
        size = _PAYLOAD_LEN.get(ftype)
        if size is None:
            # Not a frame type we know (0x34 config messages, or a resync);
            # step past this 0x1a and keep looking.
            i += 1
            continue
        need = _HEADER_LEN + size
        out = bytearray()
        j = i + 2
        truncated = False
        while len(out) < need and j < n:
            b = buf[j]
            if b != _ESC:
                out.append(b)
                j += 1
                continue
            if j + 1 >= n:
                truncated = True  # escape split across reads
                break
            if buf[j + 1] == _ESC:
                out.append(_ESC)
                j += 2
            else:
                # Unescaped 0x1a => this frame was cut short and a new frame
                # begins here. Drop the partial and resync.
                truncated = True
                break
        if truncated and j >= n:
            break  # incomplete at the end of the buffer; wait for more data
        if len(out) < need:
            i = j  # resync at the 0x1a we just found
            continue
        frames.append((ftype, bytes(out[_HEADER_LEN:])))
        i = j
        consumed = j
    return frames, consumed


def count_dfs(frames: list[tuple[int, bytes]], into: dict[Any, int]) -> None:
    """Tally frames by DF (or MODEAC_KEY for Mode A/C) into ``into``."""
    for ftype, payload in frames:
        if ftype == 0x31:
            into[MODEAC_KEY] = into.get(MODEAC_KEY, 0) + 1
        elif payload:
            df = payload[0] >> 3
            into[df] = into.get(df, 0) + 1


class BeastDfCounter:
    """Background reader that keeps a running per-DF tally of readsb's Beast
    output. Start it once; call snapshot() each publish cycle.

    Everything is best-effort: if readsb is not listening yet, or the connection
    drops, the thread retries and snapshot() simply reports nothing new. It never
    raises into the publish loop and never blocks it.
    """

    def __init__(
        self,
        host: str = BEAST_HOST,
        port: int = BEAST_PORT,
        reconnect_delay_s: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._reconnect_delay_s = reconnect_delay_s
        self._lock = threading.Lock()
        self._counts: dict[Any, int] = {}
        self._last_snapshot_at: float | None = None
        self._connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="beast-df", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    # -- reader ------------------------------------------------------------
    def _run(self) -> None:
        buf = b""
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.create_connection((self._host, self._port), timeout=10.0)
                sock.settimeout(5.0)
                with self._lock:
                    self._connected = True
                buf = b""
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(65536)
                    except TimeoutError:
                        continue  # quiet stream is not an error
                    if not chunk:
                        break  # readsb closed the connection
                    buf += chunk
                    frames, consumed = parse_frames(buf)
                    if consumed:
                        buf = buf[consumed:]
                    # Guard against unbounded growth if we never resync.
                    if len(buf) > 1 << 20:
                        buf = b""
                    if frames:
                        with self._lock:
                            count_dfs(frames, self._counts)
            except OSError:
                pass  # readsb down / restarting -- retry below
            finally:
                with self._lock:
                    self._connected = False
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if not self._stop.is_set():
                self._stop.wait(self._reconnect_delay_s)

    # -- consumer ----------------------------------------------------------
    def snapshot(self, now: float | None = None) -> dict[Any, float]:
        """Per-second rates since the previous snapshot, then reset the tally.

        Returns {} on the very first call (no baseline interval yet) and
        whenever no time has passed, so a caller can publish only real numbers.
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            counts = self._counts
            self._counts = {}
            prev = self._last_snapshot_at
            self._last_snapshot_at = now
        if prev is None:
            return {}
        elapsed = now - prev
        if elapsed <= 0:
            return {}
        return {k: v / elapsed for k, v in counts.items()}
