# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Home Assistant Supervisor helper: resolve the MQTT broker connection info
from the Supervisor `mqtt` service."""

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .util import log

DEFAULT_MQTT_SERVICE = "http://supervisor/services/mqtt"


def resolve_mqtt_service(log_level: str) -> dict[str, Any] | None:
    """Fetch broker connection info from the Supervisor `mqtt` service (the
    add-on declares services: [mqtt:want]). Returns {host, port, username,
    password, ssl} or None if unavailable."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    url = os.environ.get("SUPERVISOR_MQTT_SERVICE_URL") or DEFAULT_MQTT_SERVICE
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, OSError) as e:
        log("WARNING", f"could not resolve MQTT service: {e}", log_level)
        return None
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else None
