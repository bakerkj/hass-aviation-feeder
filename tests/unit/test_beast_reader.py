# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The Beast reader's socket loop, which the frame-parsing tests never reached.

test_beast.py covers ``parse_frames``/``count_dfs``/``snapshot`` -- all pure --
so the half that actually talks to readsb had no coverage at all: connecting,
tallying what arrives, surviving a drop, and winding up when asked. Those are
the parts that can hang the process, so they are what is exercised here, against
a real loopback server rather than a mock.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import beast

# One Mode-S long frame (type 0x33): 0x1a, type, 6-byte MLAT, signal, 14 payload.
# payload[0] = 0x8d -> DF 17, which is what real ADS-B traffic is dominated by.
_DF17 = bytes([0x1A, 0x33]) + bytes(7) + bytes([0x8D]) + bytes(13)


class _Server:
    """A loopback stand-in for readsb's Beast port."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.connections = 0
        self._to_send = b""
        self._drop_after_send = False
        self._repeat_every: float | None = None

    async def start(
        self,
        *,
        send: bytes = b"",
        drop_after_send: bool = False,
        repeat_every: float | None = None,
    ) -> int:
        self._to_send, self._drop_after_send = send, drop_after_send
        self._repeat_every = repeat_every
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        try:
            if self._to_send:
                writer.write(self._to_send)
                await writer.drain()
            if self._drop_after_send:
                writer.close()
                return
            if self._repeat_every is not None:
                # Keep emitting, so a test can set its rate baseline and then
                # wait for fresh frames without racing the initial burst.
                while True:
                    writer.write(self._to_send)
                    await writer.drain()
                    await asyncio.sleep(self._repeat_every)
            # Hold the connection open and silent, so the reader is parked in
            # its read with nothing arriving -- the case that matters for stop.
            # Waiting on the client's EOF rather than sleeping: from 3.12
            # Server.wait_closed() blocks until every handler returns, so a
            # sleeping handler would hang teardown rather than the code we are
            # testing.
            await reader.read()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            with contextlib_suppress():
                writer.close()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class contextlib_suppress:
    """Tiny local suppressor: closing an already-closed writer is not news."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


class BeastReaderLoop(unittest.IsolatedAsyncioTestCase):
    async def test_frames_from_the_wire_land_in_the_tally(self) -> None:
        srv = _Server()
        port = await srv.start(send=_DF17, repeat_every=0.005)
        counter = beast.BeastDfCounter(host="127.0.0.1", port=port)
        counter.start()
        try:
            await _until(lambda: counter.connected)
            # Baseline first: snapshot() resets the tally, so anything counted
            # before this point is deliberately discarded.
            counter.snapshot(now=100.0)
            await _until(lambda: counter._counts)
            rates = counter.snapshot(now=101.0)
        finally:
            await counter.aclose()
            await srv.stop()
        self.assertIn(17, rates, f"expected DF17 in {rates}")

    async def test_connected_reflects_the_socket(self) -> None:
        srv = _Server()
        port = await srv.start()
        counter = beast.BeastDfCounter(host="127.0.0.1", port=port)
        self.assertFalse(counter.connected, "not connected before start()")
        counter.start()
        try:
            await _until(lambda: counter.connected)
            self.assertTrue(counter.connected)
        finally:
            await counter.aclose()
        self.assertFalse(counter.connected, "must not claim connected after close")

    async def test_a_dropped_connection_is_retried(self) -> None:
        """readsb restarting must not permanently stop the tally."""
        srv = _Server()
        port = await srv.start(send=_DF17, drop_after_send=True)
        counter = beast.BeastDfCounter(
            host="127.0.0.1", port=port, reconnect_delay_s=0.01
        )
        counter.start()
        try:
            await _until(lambda: srv.connections >= 3, timeout=5.0)
        finally:
            await counter.aclose()
            await srv.stop()
        self.assertGreaterEqual(srv.connections, 3)

    async def test_a_broker_that_is_not_listening_is_survivable(self) -> None:
        """readsb not up yet must not raise out of the task or spin hot."""
        counter = beast.BeastDfCounter(
            host="127.0.0.1", port=_closed_port(), reconnect_delay_s=0.01
        )
        counter.start()
        try:
            await asyncio.sleep(0.15)
            self.assertFalse(counter.connected)
            self.assertFalse(
                counter._task.done(), "reader task died instead of retrying"
            )
        finally:
            await counter.aclose()

    async def test_stop_is_prompt_while_the_stream_is_silent(self) -> None:
        """The elapsed assertion is the point.

        A reader that waits only on the socket notices a shutdown request when
        traffic next arrives -- which on a quiet Beast stream may be never. The
        loop races the read against the stop event, so this returns at once;
        waiting on the socket alone it would never return at all here.
        """
        srv = _Server()
        port = await srv.start()  # connects, then silent forever
        counter = beast.BeastDfCounter(host="127.0.0.1", port=port)
        counter.start()
        await _until(lambda: counter.connected)

        started = asyncio.get_running_loop().time()
        await counter.aclose()
        elapsed = asyncio.get_running_loop().time() - started
        await srv.stop()

        self.assertLess(elapsed, 2.0, f"shutdown took {elapsed:.1f}s on a quiet stream")


async def _until(pred, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


def _closed_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
