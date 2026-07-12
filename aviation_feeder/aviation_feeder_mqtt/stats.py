# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Read and parse readsb's stats.json (written by --write-json)."""

from typing import Any

from .util import read_json_dict


def read_stats(path: str) -> dict[str, Any] | None:
    """Return the parsed stats.json, or None if it is missing/unreadable/mid
    -write (readsb rewrites it atomically, but be defensive about a partial
    read racing the writer)."""
    return read_json_dict(path)
