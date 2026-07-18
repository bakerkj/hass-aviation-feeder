# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Compute "planes near me" metrics from readsb's aircraft.json + the station
location: how many aircraft are within a radius, and details of the nearest."""

import math
from typing import Any

from .util import num as _num, read_json_dict

_EARTH_RADIUS_NM = 3440.065  # mean Earth radius in nautical miles


def read_aircraft(path: str) -> dict[str, Any] | None:
    """Return the parsed aircraft.json, or None if missing/unreadable/partial."""
    return read_json_dict(path)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compute_nearby(
    aircraft_json: dict[str, Any],
    station_lat: float,
    station_lon: float,
    radius_nm: float,
) -> dict[str, Any]:
    """Return {aircraft_in_range, nearest_distance_nm, nearest_altitude_ft,
    nearest}. ``nearest`` is None when nothing is in range; the numeric
    nearest_* keys are None too so their HA entities go unavailable."""
    acs = aircraft_json.get("aircraft")
    in_range: list[tuple[float, dict[str, Any]]] = []
    if isinstance(acs, list):
        for a in acs:
            if not isinstance(a, dict):
                continue
            lat = _num(a.get("lat"))
            lon = _num(a.get("lon"))
            if lat is None or lon is None:
                continue
            d = haversine_nm(station_lat, station_lon, lat, lon)
            if d <= radius_nm:
                in_range.append((d, a))

    in_range.sort(key=lambda t: t[0])

    result: dict[str, Any] = {
        "aircraft_in_range": len(in_range),
        "nearest_distance_nm": None,
        "nearest_altitude_ft": None,
        "nearest": None,
    }
    if in_range:
        d, a = in_range[0]
        alt = _num(a.get("alt_baro"))  # "ground" (str) -> None
        flight = (a.get("flight") or "").strip()
        result["nearest_distance_nm"] = round(d, 1)
        result["nearest_altitude_ft"] = alt
        result["nearest"] = {
            "flight": flight or a.get("hex"),
            "hex": a.get("hex"),
            "distance_nm": round(d, 1),
            "altitude_ft": alt,
            "bearing_deg": round(
                bearing_deg(station_lat, station_lon, a["lat"], a["lon"])
            ),
            "ground_speed_kt": _num(a.get("gs")),
        }
    return result
