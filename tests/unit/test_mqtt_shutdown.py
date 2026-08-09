# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The shutdown path must not be able to outlast the supervisor's stop grace.

paho documents ``loop_stop()`` as blocking until the network thread finishes,
and that thread only exits once ``_out_packet`` and ``_out_messages`` are both
empty. A qos=1 message the broker never acknowledges -- the retained "offline"
farewell, typically -- keeps ``_out_messages`` non-empty, so the join never
returns. Measured at >75s against a wedged broker, well past the grace, at which
point the farewell is lost anyway.

Not the connect path: paho sets its own ``_connect_timeout`` of 5s and hands it
to ``socket.create_connection``, so a blackholed broker fails there in seconds
rather than at the kernel's SYN timeout. The unacked publish is the unbounded
case, and the only reason this is enforced rather than merely documented.
"""

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt.mqtt import MqttHealth, mqtt_publish

PACKAGE = (
    Path(__file__).resolve().parents[2] / "aviation_feeder" / "aviation_feeder_mqtt"
)


def _loop_stop_calls(tree: ast.AST) -> list[int]:
    """Line numbers of any ``<something>.loop_stop(...)`` call."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "loop_stop"
    ]


class LoopStopInvariant(unittest.TestCase):
    def test_sources_are_discovered(self) -> None:
        """Positive control: a glob that matched nothing would make the test
        below vacuous and permanently green."""
        sources = sorted(PACKAGE.glob("*.py"))
        self.assertGreater(len(sources), 5, sorted(p.name for p in sources))
        self.assertIn("app.py", [p.name for p in sources])

    def test_the_detector_actually_detects(self) -> None:
        """Positive control for the detector itself, so a refactor cannot
        quietly turn it into a no-op."""
        bad = ast.parse("def f(c):\n    c.loop_stop()\n")
        self.assertTrue(_loop_stop_calls(bad), "detector missed the known-bad shape")
        good = ast.parse("def f(c):\n    c.disconnect()\n")
        self.assertFalse(_loop_stop_calls(good))

    def test_no_module_joins_pahos_network_thread(self) -> None:
        for path in sorted(PACKAGE.glob("*.py")):
            with self.subTest(module=path.name):
                hits = _loop_stop_calls(ast.parse(path.read_text()))
                self.assertEqual(
                    hits,
                    [],
                    f"{path.name} calls loop_stop() at line(s) {hits}. That join "
                    "is unbounded against a broker holding an unacked qos=1 "
                    "message; disconnect() alone is sufficient and the daemon "
                    "thread is reaped at exit.",
                )


class _Info:
    """Stands in for paho's MQTTMessageInfo."""

    def __init__(self, rc: int = 0, raises: Exception | None = None) -> None:
        self.rc = rc
        self.waited: list[float] = []
        self._raises = raises

    def wait_for_publish(self, timeout: float | None = None) -> None:
        if self._raises is not None:
            raise self._raises
        self.waited.append(timeout)


class _Client:
    def __init__(self, info: _Info) -> None:
        self.info = info

    def publish(self, topic, payload="", qos=0, retain=False):
        return self.info


class FarewellFlush(unittest.TestCase):
    """The farewell must be flushed, and the flush must be bounded."""

    def test_flush_timeout_waits_for_the_broker(self) -> None:
        """Unflushed, a qos=1 farewell can be abandoned before paho's network
        thread drains it, and the broker falls back to the last will."""
        info = _Info()
        ok = mqtt_publish(
            _Client(info),
            "t/availability",
            "offline",
            qos=1,
            retain=True,
            log_level="ERROR",
            health=MqttHealth(),
            flush_timeout=2.0,
        )
        self.assertTrue(ok)
        self.assertEqual(info.waited, [2.0])

    def test_no_flush_by_default(self) -> None:
        """Steady-state publishes must not pay an ack wait each time."""
        info = _Info()
        mqtt_publish(
            _Client(info),
            "t/x/state",
            "1",
            qos=0,
            retain=False,
            log_level="ERROR",
            health=MqttHealth(),
        )
        self.assertEqual(info.waited, [])

    def test_a_raising_flush_is_survivable(self) -> None:
        """paho raises if the loop is not running or the broker has dropped.
        Shutdown must not turn that into an exception on the way out."""
        for exc in (RuntimeError("loop not running"), ValueError("bad state")):
            with self.subTest(exc=type(exc).__name__):
                ok = mqtt_publish(
                    _Client(_Info(raises=exc)),
                    "t/availability",
                    "offline",
                    qos=1,
                    retain=True,
                    log_level="ERROR",
                    health=MqttHealth(),
                    flush_timeout=2.0,
                )
                self.assertTrue(ok, "a failed flush must not report the publish failed")


if __name__ == "__main__":
    unittest.main()
