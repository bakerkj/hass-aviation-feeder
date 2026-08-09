# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The shutdown path must not be able to outlast the supervisor's stop grace.

The add-on runs on aiomqtt now, so there is no paho network thread to join; the
loop_stop invariant below is kept as a regression guard against reintroducing
one. What replaces the old flush test is the publish contract the session loop
depends on: state publishes stamp the health clock, others do not, and broker
faults propagate rather than being swallowed.

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

import aiomqtt
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


class _Recorder:
    """Stands in for aiomqtt.Client, recording what would go on the wire."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple] = []
        self.error = error

    async def publish(self, topic, payload="", qos=0, retain=False):
        if self.error is not None:
            raise self.error
        self.sent.append((topic, payload, qos, retain))


class PublishSemantics(unittest.IsolatedAsyncioTestCase):
    """What the session loop above depends on mqtt_publish doing."""

    async def test_a_state_publish_stamps_the_health_clock(self) -> None:
        health = MqttHealth()
        mq = _Recorder()
        await mqtt_publish(
            mq,
            "t/x/state",
            "1",
            qos=0,
            retain=False,
            log_level="ERROR",
            health=health,
            mark_state=True,
        )
        self.assertEqual(mq.sent, [("t/x/state", "1", 0, False)])
        self.assertGreater(health.last_state_publish_ok, 0)

    async def test_a_non_state_publish_leaves_the_clock_alone(self) -> None:
        """Only real state publishes may refresh the stall watchdog's clock."""
        health = MqttHealth()
        await mqtt_publish(
            _Recorder(),
            "t/availability",
            "online",
            qos=1,
            retain=True,
            log_level="ERROR",
            health=health,
        )
        self.assertEqual(health.last_state_publish_ok, 0.0)

    async def test_a_broker_fault_propagates_so_the_session_reconnects(self) -> None:
        """Swallowed here, the loop would cycle against a dead session with
        every sensor silently frozen. The session loop catches MqttError to
        tear down and reconnect, so it has to get there."""
        health = MqttHealth()
        with self.assertRaises(aiomqtt.MqttError):
            await mqtt_publish(
                _Recorder(aiomqtt.MqttError("broker went away")),
                "t/x/state",
                "1",
                qos=0,
                retain=False,
                log_level="ERROR",
                health=health,
                mark_state=True,
            )
        self.assertEqual(
            health.last_state_publish_ok,
            0.0,
            "a publish that never landed must not stamp the clock",
        )


if __name__ == "__main__":
    unittest.main()
