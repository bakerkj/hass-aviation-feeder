# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for per-feeder metric discovery — the MLAT positions/aircraft
metrics are enabled by default for every MLAT feeder, and the byte/message
applicability sets are single-sourced."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import mqtt  # noqa: E402
from aviation_feeder_mqtt.metadata import (  # noqa: E402
    FEEDERS_DEVICE_ID,
    MLAT_RESULT_METRICS,
)


class ResultMetricsEnabled(unittest.TestCase):
    def test_result_metrics_enabled_by_default(self):
        # positions/min + aircraft-used are enabled by default for every feeder
        # (no disabling key in the discovery payload).
        cfgs = mqtt.build_feeder_metrics_discovery(
            "homeassistant",
            "hafeed/feeders",
            "hafeed/status",
            90,
            [("radarbox", "RadarBox"), ("planewatch", "Plane.watch")],
            MLAT_RESULT_METRICS,
            via_parent=False,
        )
        for key in ("radarbox", "planewatch"):
            for m in MLAT_RESULT_METRICS:
                topic = (
                    f"homeassistant/sensor/{FEEDERS_DEVICE_ID}/{key}_{m.suffix}/config"
                )
                self.assertNotIn(
                    "enabled_by_default",
                    cfgs[topic],
                    f"{topic} should be enabled by default",
                )


class FeederDeviceNesting(unittest.TestCase):
    def test_via_parent_toggles_via_device(self):
        d = mqtt._feeder_device("radarbox", "RadarBox", via_parent=True)
        self.assertEqual(d["identifiers"], [f"{FEEDERS_DEVICE_ID}_radarbox"])
        self.assertEqual(d["via_device"], mqtt.DEVICE_ID)
        # via_parent False (main device not registered) -> no dangling via_device
        d2 = mqtt._feeder_device("radarbox", "RadarBox", via_parent=False)
        self.assertNotIn("via_device", d2)


class PayloadShape(unittest.TestCase):
    REQUIRED = {
        "name",
        "unique_id",
        "state_topic",
        "device",
        "state_class",
        "availability_topic",
        "payload_available",
        "payload_not_available",
        "expire_after",
        "entity_category",
    }

    def test_feeder_metric_payload_fields(self):
        from aviation_feeder_mqtt.metadata import UPTIME_METRICS

        cfgs = mqtt.build_feeder_metrics_discovery(
            "homeassistant",
            "hafeed/feeders",
            "hafeed/status",
            90,
            [("radarbox", "RadarBox")],
            UPTIME_METRICS,
            via_parent=True,
        )
        ((topic, p),) = cfgs.items()
        self.assertTrue(self.REQUIRED <= set(p), self.REQUIRED - set(p))
        self.assertTrue(p["has_entity_name"])
        self.assertEqual(p["entity_category"], "diagnostic")
        self.assertEqual(p["unique_id"], f"{FEEDERS_DEVICE_ID}_radarbox_uptime")
        self.assertEqual(p["state_topic"], "hafeed/feeders/radarbox/uptime/state")
        self.assertEqual(p["device"]["via_device"], mqtt.DEVICE_ID)

    def test_main_device_metric_payload_fields(self):
        cfgs = mqtt.build_discovery_payloads(
            "homeassistant", "hafeed", "hafeed/status", 90
        )
        self.assertTrue(cfgs)
        for p in cfgs.values():
            self.assertTrue(
                {
                    "name",
                    "unique_id",
                    "state_topic",
                    "device",
                    "availability_topic",
                    "expire_after",
                }
                <= set(p)
            )
            self.assertEqual(p["entity_category"], "diagnostic")
            self.assertEqual(p["device"]["identifiers"], [mqtt.DEVICE_ID])


class Applicability(unittest.TestCase):
    def test_byte_and_message_feeder_sets(self):
        from aviation_feeder_mqtt import app
        from aviation_feeder_mqtt.feeders import THROUGHPUT_KERNEL

        # byte throughput = kernel-TCP feeders + pfclient; messages = fr24 only
        self.assertEqual(
            app._BYTE_FEEDERS, frozenset(THROUGHPUT_KERNEL) | {"planefinder"}
        )
        self.assertEqual(app._MESSAGE_FEEDERS, frozenset({"fr24"}))


if __name__ == "__main__":
    unittest.main()
