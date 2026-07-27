# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for supervisor.resolve_mqtt_service — the broker-resolution /
auth path (no token, URL override, transport errors, malformed body)."""

import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import supervisor


class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ResolveMqtt(unittest.TestCase):
    def test_no_token_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(supervisor.resolve_mqtt_service("ERROR"))

    def test_returns_data_dict(self):
        body = json.dumps(
            {"result": "ok", "data": {"host": "h", "port": 1883, "ssl": False}}
        ).encode()
        with (
            mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "tok"}, clear=True),
            mock.patch("urllib.request.urlopen", return_value=_Resp(body)),
        ):
            self.assertEqual(
                supervisor.resolve_mqtt_service("ERROR"),
                {"host": "h", "port": 1883, "ssl": False},
            )

    def test_url_override_is_used(self):
        seen = {}

        def fake(req, timeout=10):
            seen["url"] = req.full_url
            return _Resp(b'{"data":{}}')

        with (
            mock.patch.dict(
                os.environ,
                {
                    "SUPERVISOR_TOKEN": "tok",
                    "SUPERVISOR_MQTT_SERVICE_URL": "http://custom/mqtt",
                },
                clear=True,
            ),
            mock.patch("urllib.request.urlopen", side_effect=fake),
        ):
            supervisor.resolve_mqtt_service("ERROR")
        self.assertEqual(seen["url"], "http://custom/mqtt")

    def test_transport_errors_return_none(self):
        for exc in (urllib.error.URLError("x"), ValueError("x"), OSError("x")):
            with (
                mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "tok"}, clear=True),
                mock.patch("urllib.request.urlopen", side_effect=exc),
            ):
                self.assertIsNone(supervisor.resolve_mqtt_service("ERROR"))

    def test_missing_or_nondict_data_returns_none(self):
        for body in (b'{"result":"ok"}', b'{"data":"nope"}', b'"nope"', b"[]"):
            with (
                mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "tok"}, clear=True),
                mock.patch("urllib.request.urlopen", return_value=_Resp(body)),
            ):
                self.assertIsNone(supervisor.resolve_mqtt_service("ERROR"))


if __name__ == "__main__":
    unittest.main()
