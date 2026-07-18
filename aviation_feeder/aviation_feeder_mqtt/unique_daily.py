# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Track the count of distinct aircraft (ICAO hex) seen since local midnight,
for a Home Assistant "unique aircraft today" sensor.

In-memory by design: a container restart resets the running set (Home Assistant
sees a single total_increasing reset and carries on), and the set resets at
local midnight so the number is per-calendar-day in the station's timezone. The
current day is passed in by the caller (from time.localtime) rather than read
here, which keeps the rollover deterministic and unit-testable."""

from typing import Any


class UniqueDailyTracker:
    """Accumulates distinct aircraft hex ids for the current local day."""

    def __init__(self) -> None:
        self._day: tuple[int, int, int] | None = None
        self._hexes: set[str] = set()

    def update(self, aircraft_json: dict[str, Any], today: tuple[int, int, int]) -> int:
        """Union this cycle's aircraft hex ids into the day's set and return the
        distinct count. ``today`` is (year, month, day) in the station's local
        time; when it changes from the previous call the set resets (new day)."""
        if today != self._day:
            self._day = today
            self._hexes = set()
        acs = aircraft_json.get("aircraft")
        if isinstance(acs, list):
            for a in acs:
                if not isinstance(a, dict):
                    continue
                hx = a.get("hex")
                if isinstance(hx, str) and hx.strip():
                    self._hexes.add(hx.strip().lower())
        return len(self._hexes)
