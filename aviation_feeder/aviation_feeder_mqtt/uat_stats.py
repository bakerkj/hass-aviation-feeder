# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Read dump978's aggregated UAT stats (/run/stats/stats.json, produced by the
uat-stats s6 service running the dump978 stats.py) for the Home Assistant "UAT"
sensor device. The per-metric extraction lives in metadata.compute_uat_metrics;
this module only handles the file read."""

from typing import Any

from .util import read_json_dict

UAT_STATS_PATH = "/run/stats/stats.json"


def read_uat_stats(path: str) -> dict[str, Any] | None:
    """Return the parsed UAT stats.json, or None if missing/unreadable/partial."""
    return read_json_dict(path)
