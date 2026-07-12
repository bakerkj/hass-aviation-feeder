# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Small utilities shared across the aviation_feeder_mqtt package."""

import json
import time
from typing import Any

_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def read_json_dict(path: str) -> dict[str, Any] | None:
    """Parse a JSON file into a dict, or None if it is missing/unreadable/partial
    (a read racing an atomic rewrite) or is not a JSON object."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def num(v: Any) -> float | int | None:
    """v if it is a real number (int/float), else None."""
    return v if isinstance(v, (int, float)) else None


def log(level: str, msg: str, min_level: str = "INFO") -> None:
    """Print a timestamped, level-filtered log line."""
    if _ORDER.get(level, 20) < _ORDER.get(min_level, 20):
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"{ts} [{level}] [aviation-mqtt] {msg}", flush=True)
