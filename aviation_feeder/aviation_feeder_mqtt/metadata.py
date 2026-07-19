# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Feeder-health metric definitions: how to read each value out of readsb's
stats.json, plus its Home Assistant sensor metadata (name/unit/class/icon).

This is the single source of truth for the metric set; the publisher iterates
METRICS to build both the discovery payloads and the per-cycle state values."""

from dataclasses import dataclass
from typing import Any, Callable

from .util import num as _num

# readsb reports distances in metres; 1 nautical mile = 1852 m.
_METERS_PER_NM = 1852.0

DEVICE_ID = "aviation_feeder"
DEVICE_NAME = "Aviation Feeder"
DEVICE_MODEL = "readsb / ultrafeeder"
DEVICE_MANUFACTURER = "hass-aviation-feeder"

# Emergency-squawk safety binary_sensor on the main device (see emergency.py);
# state is a plain on/off topic with the offenders as JSON attributes, so it
# isn't a METRIC — just the topic key, shared by the publisher and discovery.
EMERGENCY_SQUAWK_KEY = "emergency_squawk"


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    unit: str | None
    device_class: str | None
    state_class: str
    icon: str
    precision: int
    # Extract this metric's value from a parsed stats.json dict. Returns None
    # when the source field is absent (the HA entity then expires/unavailable).
    extract: Callable[[dict[str, Any]], float | int | None]


def _get(d: dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _aircraft_total(s: dict[str, Any]) -> int | None:
    wp = _num(s.get("aircraft_with_pos"))
    wo = _num(s.get("aircraft_without_pos"))
    if wp is None and wo is None:
        return None
    return int((wp or 0) + (wo or 0))


def _messages_per_sec(s: dict[str, Any]) -> float | None:
    start = _num(_get(s, "last1min", "start"))
    end = _num(_get(s, "last1min", "end"))
    msgs = _num(_get(s, "last1min", "messages"))
    if start is None or end is None or msgs is None:
        return None
    dur = end - start
    if dur <= 0:
        return None
    return msgs / dur


def _max_range_nm(s: dict[str, Any]) -> float | None:
    m = _num(_get(s, "total", "max_distance"))
    if m is None:
        return None
    return m / _METERS_PER_NM


METRICS: list[Metric] = [
    Metric(
        "aircraft_total",
        "Aircraft Tracked",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane",
        0,
        _aircraft_total,
    ),
    Metric(
        "aircraft_adsb",
        "Aircraft ADS-B",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane",
        0,
        lambda s: _num(_get(s, "aircraft_count_by_type", "adsb_icao")),
    ),
    Metric(
        "aircraft_mode_s",
        "Aircraft Mode-S",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane",
        0,
        lambda s: _num(_get(s, "aircraft_count_by_type", "mode_s")),
    ),
    Metric(
        "aircraft_mlat",
        "Aircraft MLAT",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane-marker",
        0,
        lambda s: _num(_get(s, "aircraft_count_by_type", "mlat")),
    ),
    Metric(
        "aircraft_positions",
        "Aircraft with Position",
        "aircraft",
        None,
        "measurement",
        "mdi:map-marker",
        0,
        lambda s: _num(s.get("aircraft_with_pos")),
    ),
    Metric(
        "messages_per_sec",
        "Message Rate",
        "msg/s",
        None,
        "measurement",
        "mdi:message-processing",
        1,
        _messages_per_sec,
    ),
    Metric(
        "max_range_nm",
        "Max Range",
        "nmi",
        None,
        "measurement",
        "mdi:map-marker-distance",
        1,
        _max_range_nm,
    ),
    Metric(
        "tracks_total",
        "Tracks (session)",
        "tracks",
        None,
        "total_increasing",
        "mdi:chart-line",
        0,
        lambda s: _num(_get(s, "total", "tracks", "all")),
    ),
]


def compute_metrics(stats: dict[str, Any]) -> dict[str, float | int | None]:
    """Map a parsed stats.json into {metric_key: value}. Missing values are
    None (skipped when publishing so the HA entity expires cleanly)."""
    return {m.key: m.extract(stats) for m in METRICS}


# MQTT broker-link diagnostics on the main device. These come from MqttHealth,
# not stats.json, so their extract is unused (the publisher fills the state
# directly); they exist here only to drive discovery metadata. LWT already marks
# the whole device unavailable when the link is down; these surface *how* the
# link has behaved (uptime since the last connect, reconnect count = flapping).
BROKER_METRICS: list[Metric] = [
    Metric(
        "mqtt_uptime",
        "MQTT Link Uptime",
        "s",
        "duration",
        "measurement",
        "mdi:lan-connect",
        0,
        lambda s: None,
    ),
    Metric(
        "mqtt_reconnects",
        "MQTT Reconnects",
        None,
        None,
        "total_increasing",
        "mdi:lan-disconnect",
        0,
        lambda s: None,
    ),
]


# "Unique aircraft today" sensor on the main device: distinct ICAO hex ids seen
# since local midnight (see unique_daily.py). Computed by the publisher, not from
# stats.json, so its extract is unused (like the broker metrics above). A primary
# entity (not diagnostic) — it's a dashboard number, not a diagnostic.
UNIQUE_TODAY_KEY = "unique_today"
UNIQUE_METRICS: list[Metric] = [
    Metric(
        UNIQUE_TODAY_KEY,
        "Unique Aircraft Today",
        "aircraft",
        None,
        "total_increasing",
        "mdi:airplane-search",
        0,
        lambda s: None,
    ),
]


# --- "Planes near me" device -----------------------------------------------
# A second HA device fed from aircraft.json (see nearby.compute_nearby). Its
# numeric sensors' extract functions read the compute_nearby() result dict; the
# text "nearest aircraft" entity (state + JSON attributes) is handled directly
# by the publisher since it isn't a plain scalar.
NEARBY_DEVICE_ID = "aviation_feeder_nearby"
NEARBY_DEVICE_NAME = "Aviation Feeder — Nearby"
NEARBY_STATE_KEY = "nearest_aircraft"  # the text entity's key

# --- "Feeders" device (per-feeder connection status; see feeders.py) --------
FEEDERS_DEVICE_ID = "aviation_feeder_feeders"
FEEDERS_DEVICE_NAME = "Aviation Feeder — Feeders"


@dataclass(frozen=True)
class FeederMetric:
    """A numeric metric attached to every enabled feeder under the Feeders
    device (e.g. throughput). Values are computed per-feeder at publish time.
    enabled_default=False hides the entity by default in HA (user can enable it)."""

    suffix: str
    name_suffix: str
    unit: str | None
    device_class: str | None
    state_class: str
    icon: str
    precision: int
    enabled_default: bool = True


# Per-feeder metric groups, attached selectively per feeder (see the applicability
# in app.py). The RATE sensors (bytes/s, msg/s) are the primary, enabled ones;
# the cumulative Data Sent/Received + Messages counters are still published but
# disabled by default (available for anyone who wants totals).
THROUGHPUT_METRICS: list[FeederMetric] = [
    FeederMetric(
        "bytes_sent",
        "Data Sent",
        "B",
        "data_size",
        "total_increasing",
        "mdi:upload",
        0,
        enabled_default=False,
    ),
    FeederMetric(
        "bytes_received",
        "Data Received",
        "B",
        "data_size",
        "total_increasing",
        "mdi:download",
        0,
        enabled_default=False,
    ),
]
THROUGHPUT_RATE_METRICS: list[FeederMetric] = [
    FeederMetric(
        "bytes_sent_rate",
        "Send Rate",
        "B/s",
        "data_rate",
        "measurement",
        "mdi:upload-network",
        0,
    ),
    FeederMetric(
        "bytes_received_rate",
        "Receive Rate",
        "B/s",
        "data_rate",
        "measurement",
        "mdi:download-network",
        0,
    ),
]
MESSAGES_METRICS: list[FeederMetric] = [
    FeederMetric(
        "messages",
        "Messages",
        None,
        None,
        "total_increasing",
        "mdi:message-badge-outline",
        0,
        enabled_default=False,
    ),
]
MESSAGES_RATE_METRICS: list[FeederMetric] = [
    FeederMetric(
        "messages_rate",
        "Message Rate",
        "msg/s",
        None,
        "measurement",
        "mdi:message-fast-outline",
        1,
    ),
]
UPTIME_METRICS: list[FeederMetric] = [
    FeederMetric(
        "uptime", "Uptime", "s", "duration", "measurement", "mdi:timer-outline", 0
    ),
]

# Per-portal aircraft counts: the aggregator's OWN view of the station, read from
# that feeder client's self-report (see app_reports). Every feeder reads the same
# in-container readsb, so expect the portal's TOTAL to sit close to the main
# device's -- they are watching the same aircraft. What differs, by design, is
# the ADS-B/non-ADS-B SPLIT, since each portal classifies for itself; that split
# is the informative part, not a bug. Names stay unqualified because these hang
# off the per-feeder sub-device, which already carries the attribution.
PORTAL_AIRCRAFT_METRICS: list[FeederMetric] = [
    FeederMetric(
        "portal_aircraft",
        "Aircraft Tracked",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane",
        0,
    ),
    FeederMetric(
        "portal_aircraft_adsb",
        "Aircraft ADS-B",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane",
        0,
    ),
    FeederMetric(
        "portal_aircraft_other",
        "Aircraft non-ADS-B",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane-marker",
        0,
    ),
]

# Feeder-specific health binary_sensors derived from an app self-report — currently
# only piaware (from its status.json), whose MLAT/radio health has no mlat-client
# --stats-json equivalent. (feeder_key, suffix, entity_name, report_field, icon):
# state is ON when the reported status is "green".
REPORT_BINARY_SENSORS: list[tuple[str, str, str, str, str]] = [
    ("piaware", "mlat_ok", "MLAT", "mlat", "mdi:crosshairs-gps"),
    ("piaware", "radio_ok", "Radio", "radio", "mdi:radio-tower"),
]

# MLAT metrics — for feeders whose mlat-client writes a --stats-json file (see
# mlat_stats.py); attached per-feeder under the Feeders device like throughput.
# Two groups by data source:
#   SYNC   (peer_count / good_sync %) come from the mlat *server* push -- every
#          MLAT feeder EXCEPT RadarBox (see MLAT_SYNC_CAPABLE). Enabled.
#   RESULT (positions/minute, aircraft-used) are written client-side by our
#          mlat-client patch (patch-mlat-client.py) -- present for EVERY MLAT
#          feeder incl. RadarBox. Enabled by default like the sync metrics.
MLAT_SYNC_METRICS: list[FeederMetric] = [
    FeederMetric(
        "mlat_peers",
        "MLAT Peers",
        None,
        None,
        "measurement",
        "mdi:account-group-outline",
        0,
    ),
    FeederMetric("mlat_sync", "MLAT Sync", "%", None, "measurement", "mdi:sync", 0),
]
MLAT_RESULT_METRICS: list[FeederMetric] = [
    FeederMetric(
        "mlat_positions_rate",
        "MLAT Positions",
        "/min",
        None,
        "measurement",
        "mdi:map-marker-radius",
        1,
    ),
    FeederMetric(
        "mlat_aircraft",
        "MLAT Aircraft Used",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane-marker",
        0,
    ),
]

NEARBY_METRICS: list[Metric] = [
    Metric(
        "aircraft_in_range",
        "Aircraft in Range",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane",
        0,
        lambda n: _num(n.get("aircraft_in_range")),
    ),
    Metric(
        "nearest_distance_nm",
        "Nearest Aircraft Distance",
        "nmi",
        None,
        "measurement",
        "mdi:map-marker-distance",
        1,
        lambda n: _num(n.get("nearest_distance_nm")),
    ),
    Metric(
        "nearest_altitude_ft",
        "Nearest Aircraft Altitude",
        "ft",
        None,
        "measurement",
        "mdi:altimeter",
        0,
        lambda n: _num(n.get("nearest_altitude_ft")),
    ),
]


# --- "SDR" device (local RTL-SDR health; only when receiver_mode=rtlsdr) ------
# readsb only populates these when it owns a local SDR: `gain_db`/`estimated_ppm`
# at the top level, and the demodulator sample stats under total/last1min.local
# ({signal,noise} dBFS levels, samples_dropped = the SDR couldn't keep up = USB/
# CPU overload). In remote/net-only mode there is no local SDR, so the publisher
# skips this device entirely (see app.py's sdr_present guard).
SDR_DEVICE_ID = "aviation_feeder_sdr"
SDR_DEVICE_NAME = "Aviation Feeder — SDR"

SDR_METRICS: list[Metric] = [
    Metric(
        "sdr_gain_db",
        "SDR Gain",
        "dB",
        None,
        "measurement",
        "mdi:antenna",
        1,
        lambda s: _num(s.get("gain_db")),
    ),
    Metric(
        "sdr_ppm",
        "SDR Frequency Error",
        "ppm",
        None,
        "measurement",
        "mdi:sine-wave",
        1,
        lambda s: _num(s.get("estimated_ppm")),
    ),
    Metric(
        "sdr_signal_dbfs",
        "SDR Signal Level",
        "dBFS",
        None,
        "measurement",
        "mdi:signal",
        1,
        lambda s: _num(_get(s, "last1min", "local", "signal")),
    ),
    Metric(
        "sdr_noise_dbfs",
        "SDR Noise Floor",
        "dBFS",
        None,
        "measurement",
        "mdi:volume-mute",
        1,
        lambda s: _num(_get(s, "last1min", "local", "noise")),
    ),
    Metric(
        "sdr_samples_dropped",
        "SDR Samples Dropped",
        None,
        None,
        "total_increasing",
        "mdi:alert-circle-outline",
        0,
        lambda s: _num(_get(s, "total", "local", "samples_dropped")),
    ),
]


def compute_sdr_metrics(stats: dict[str, Any]) -> dict[str, float | int | None]:
    """Map a parsed stats.json into {sdr_metric_key: value} (None when absent)."""
    return {m.key: m.extract(stats) for m in SDR_METRICS}


# --- "UAT" device (978 MHz decode health; only when 978 is decoded locally) ---
# Fed from dump978's own aggregator (stats.py -> /run/stats/stats.json; see the
# uat-stats s6 service). Its schema is period-bucketed (total / last_1min /
# last_5min / last_15min), each a {stat_name: value} dict. We surface the 978
# equivalents of the 1090 receiver stats on a SEPARATE device so they group on
# their own and appear/disappear with local UAT decode. max_distance_m is only
# present when the receiver location (LAT/LON) is set.
UAT_DEVICE_ID = "aviation_feeder_uat"
UAT_DEVICE_NAME = "Aviation Feeder — UAT"


def _uat_msg_rate(s: dict[str, Any]) -> float | None:
    m = _num(_get(s, "last_1min", "total_accepted_messages"))
    if m is None:
        return None
    return m / 60.0  # last_1min is a ~60s bucket -> messages/second


def _uat_range_nm(s: dict[str, Any]) -> float | None:
    m = _num(_get(s, "total", "max_distance_m"))
    if m is None:
        return None
    return m / _METERS_PER_NM


UAT_METRICS: list[Metric] = [
    Metric(
        "uat_aircraft",
        "UAT Aircraft",
        "aircraft",
        None,
        "measurement",
        "mdi:airplane",
        0,
        lambda s: _num(_get(s, "last_1min", "total_tracks")),
    ),
    Metric(
        "uat_message_rate",
        "UAT Message Rate",
        "msg/s",
        None,
        "measurement",
        "mdi:message-processing",
        1,
        _uat_msg_rate,
    ),
    Metric(
        "uat_max_range_nm",
        "UAT Max Range",
        "nmi",
        None,
        "measurement",
        "mdi:map-marker-distance",
        1,
        _uat_range_nm,
    ),
    Metric(
        "uat_signal_dbfs",
        "UAT Signal Level",
        "dBFS",
        None,
        "measurement",
        "mdi:signal",
        1,
        lambda s: _num(_get(s, "last_1min", "avg_accepted_rssi")),
    ),
]


def compute_uat_metrics(stats: dict[str, Any]) -> dict[str, float | int | None]:
    """Map a parsed /run/stats/stats.json into {uat_metric_key: value} (None when
    a source field is absent, e.g. max range before the location is known)."""
    return {m.key: m.extract(stats) for m in UAT_METRICS}
