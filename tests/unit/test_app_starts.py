# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The add-on must actually start, and stop when told.

Every other test here exercises a function. None of them start ``main()``, so
the process could fail on its first line and the suite would stay green -- which
is exactly what happened while porting this to asyncio: ``BeastDfCounter.start``
became a coroutine, ``app.main()`` still called it from sync code, and all 190
tests passed against an add-on that could not boot.

So this drives the real entry point against a stub broker and asserts two
things nothing else does: it reaches its publish loop, and SIGTERM ends it well
inside the supervisor's stop grace.
"""

import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADDON = REPO / "aviation_feeder"

_CONNACK = b"\x20\x02\x00\x00"  # accepted, no session present

# The supervisor's default stop grace. Anything at or past this is a SIGKILL in
# production, so the assertions below sit well inside it.
_STOP_GRACE_S = 10.0


class _Broker:
    """Accepts, CONNACKs, then acknowledges nothing.

    Deliberately deaf rather than absent: a refused connection never reaches the
    publish loop, so it could not catch a teardown that outlasts the grace.
    """

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.connections = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            await reader.read(4096)  # CONNECT
            writer.write(_CONNACK)
            await writer.drain()
            await reader.read()  # swallow everything, ack nothing
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            with _Quiet():
                writer.close()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class _Quiet:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


async def _spawn(tmp: str, port: int):
    """Launch the real entry point against an options file, as run.sh does."""
    stats = Path(tmp) / "stats.json"
    stats.write_text("{}")
    aircraft = Path(tmp) / "aircraft.json"
    aircraft.write_text('{"aircraft": []}')
    options = Path(tmp) / "options.json"
    options.write_text(json.dumps({**_TEST_OPTIONS, "mqtt_port": port}))
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aviation_feeder_mqtt",
        "--options",
        str(options),
        "--stats",
        str(stats),
        "--aircraft",
        str(aircraft),
        cwd=str(ADDON),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


# The options this file writes, kept in one place so the guard below can check
# them against what the add-on actually ships.
_TEST_OPTIONS = {
    "mqtt_host": "127.0.0.1",
    "mqtt_port": 0,
    "mqtt_interval_seconds": 1,
    "mqtt_log_level": "INFO",
}


class OptionsFixtureMatchesTheSchema(unittest.TestCase):
    def test_every_key_this_file_sets_is_one_the_addon_declares(self) -> None:
        """A fixture key the add-on never reads is silently ignored.

        That is not hypothetical: this file first used ``interval_seconds`` and
        ``log_level``, which config.json does not declare, so the intended 1s
        interval quietly fell back to the 30s default and the test read as
        though it were configuring something it was not.
        """
        declared = set(
            json.loads((REPO / "aviation_feeder" / "config.json").read_text())[
                "options"
            ]
        )
        self.assertTrue(declared, "config.json declared no options")
        unknown = sorted(set(_TEST_OPTIONS) - declared)
        self.assertEqual(
            unknown,
            [],
            f"options.json fixture sets keys the add-on never reads: {unknown}",
        )


class AddonStarts(unittest.IsolatedAsyncioTestCase):
    async def test_it_boots_connects_and_stops_on_sigterm(self) -> None:
        broker = _Broker()
        port = await broker.start()

        with tempfile.TemporaryDirectory() as tmp:
            stats = Path(tmp) / "stats.json"
            stats.write_text("{}")
            proc = await _spawn(tmp, port)
            try:
                line = await _await_log(proc, "MQTT connected", timeout=20.0)
                self.assertIn("MQTT connected", line)

                started = asyncio.get_running_loop().time()
                proc.send_signal(signal.SIGTERM)
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S - 2)
                except TimeoutError:
                    self.fail(
                        "add-on did not stop inside the supervisor's grace; a "
                        "deaf broker must not hold the teardown open"
                    )
                elapsed = asyncio.get_running_loop().time() - started
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                await broker.stop()

        self.assertEqual(rc, 0, f"unexpected exit status {rc}")
        self.assertLess(elapsed, _STOP_GRACE_S - 2, f"shutdown took {elapsed:.1f}s")

    async def test_a_broker_that_is_absent_does_not_stop_it_booting(self) -> None:
        """readsb and the broker come up in any order; a missing broker must
        leave the add-on retrying, not dead."""
        with tempfile.TemporaryDirectory() as tmp:
            stats = Path(tmp) / "stats.json"
            stats.write_text("{}")
            proc = await _spawn(tmp, _closed_port())
            try:
                await _await_log(proc, "reconnecting in", timeout=20.0)
                self.assertIsNone(proc.returncode, "exited instead of retrying")

                proc.send_signal(signal.SIGTERM)
                rc = await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S - 2)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        self.assertEqual(rc, 0)


async def _await_log(proc, needle: str, timeout: float) -> str:
    """Read until a line contains ``needle``, or fail with what we did see."""
    seen: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip()
                seen.append(line)
                if needle in line:
                    return line
    except TimeoutError:
        pass
    raise AssertionError(f"never logged {needle!r}; saw:\n  " + "\n  ".join(seen[-25:]))


def _closed_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
