# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Detect emergency squawks in readsb's aircraft.json for a Home Assistant
safety binary_sensor. The three transponder emergency codes are surfaced ON
(device_class=safety) whenever ANY tracked aircraft is squawking one — position
is not required, so an emergency anywhere in coverage is caught, not just within
the "planes near me" radius."""

from typing import Any

from .util import num as _num

# Mode-A transponder emergency codes -> human label.
EMERGENCY_SQUAWKS: dict[str, str] = {
    "7500": "hijack",
    "7600": "radio failure",
    "7700": "general emergency",
}


def compute_emergency(aircraft_json: dict[str, Any]) -> dict[str, Any]:
    """Return {active, count, aircraft}. ``active`` is True when at least one
    tracked aircraft squawks 7500/7600/7700; ``aircraft`` lists the offenders
    (hex, flight, squawk, decoded type, altitude) for the sensor attributes."""
    acs = aircraft_json.get("aircraft")
    hits: list[dict[str, Any]] = []
    if isinstance(acs, list):
        for a in acs:
            if not isinstance(a, dict):
                continue
            sq = a.get("squawk")
            if not isinstance(sq, str):
                continue
            label = EMERGENCY_SQUAWKS.get(sq.strip())
            if label is None:
                continue
            flight = (a.get("flight") or "").strip()
            hits.append(
                {
                    "hex": a.get("hex"),
                    "flight": flight or a.get("hex"),
                    "squawk": sq.strip(),
                    "type": label,
                    "altitude_ft": _num(a.get("alt_baro")),  # "ground" (str) -> None
                }
            )
    return {"active": bool(hits), "count": len(hits), "aircraft": hits}
