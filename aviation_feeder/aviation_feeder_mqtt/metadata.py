# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Feeder-health metric definitions: how to read each value out of readsb's
stats.json, plus its Home Assistant sensor metadata (name/unit/class/icon).

This is the single source of truth for the metric set; the publisher iterates
METRICS to build both the discovery payloads and the per-cycle state values."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
    # False hides the entity in HA until the user enables it. Used for metrics
    # that read 0 on a healthy station, so they don't add permanent noise.
    enabled_default: bool = True


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


def _remote_rate(s: dict[str, Any], field: str) -> float | None:
    """last1min.remote.<field> as a per-second rate. `remote` counts messages
    arriving over readsb's NETWORK connectors rather than the local SDR."""
    start = _num(_get(s, "last1min", "start"))
    end = _num(_get(s, "last1min", "end"))
    v = _num(_get(s, "last1min", "remote", field))
    if start is None or end is None or v is None:
        return None
    dur = end - start
    return v / dur if dur > 0 else None


# Network-ingest message rates, split Mode-S vs Mode A/C: how much traffic readsb
# is taking in over its NETWORK connectors rather than the local SDR. This is a
# station-wide receiver statistic and belongs to the main device.
#
# NOT per-feeder, and specifically NOT OpenSky stats. The retired Multi-Portal
# add-on published these same two fields as opensky_mode_s_rate /
# opensky_mode_ac_rate, but that name was wrong at the source: its "opensky"
# sensors read the shared decoder's stats.json, not anything OpenSky reported.
# openskyd exposes no status endpoint or file at all, so no OpenSky-specific
# figure exists to publish -- do not present these as one.
#
# Mode A/C stays ~0 unless readsb runs with --modeac (it does not by default);
# a non-zero value here is Mode A/C arriving from a network peer.
REMOTE_METRICS: list[Metric] = [
    Metric(
        "remote_message_rate",
        "Network Message Rate",
        "msg/s",
        None,
        "measurement",
        "mdi:lan-pending",
        1,
        lambda s: _remote_rate(s, "modes"),
    ),
    Metric(
        "remote_modeac_rate",
        "Network Mode A/C Rate",
        "msg/s",
        None,
        "measurement",
        "mdi:radio-tower",
        2,
        lambda s: _remote_rate(s, "modeac"),
    ),
]


def compute_remote_metrics(stats: dict[str, Any]) -> dict[str, float | int | None]:
    """Map a parsed stats.json into {remote_metric_key: value} (None when absent)."""
    return {m.key: m.extract(stats) for m in REMOTE_METRICS}


def _cpu_pct(s: dict[str, Any], task: str) -> float | None:
    """One readsb worker's CPU use over the last1min window, as a percentage of
    ONE core. last1min.cpu holds per-task milliseconds, so ms / (seconds * 10)
    gives percent. Reported per task rather than summed because the tasks fail
    for different reasons: `reader` is USB/SDR I/O pressure, `demod` is signal
    processing load, `background` is housekeeping. A rising `reader` is the
    early warning that samples are about to start dropping."""
    start = _num(_get(s, "last1min", "start"))
    end = _num(_get(s, "last1min", "end"))
    ms = _num(_get(s, "last1min", "cpu", task))
    if start is None or end is None or ms is None:
        return None
    dur = end - start
    if dur <= 0:
        return None
    return ms / (dur * 10.0)


# The rest of readsb's last1min.cpu block: the json/API writers and housekeeping
# workers. Each runs at roughly 0.03% of a core on a live station -- real work,
# but eight near-zero tiles would drown the three that carry signal, so these
# ship hidden and can be enabled when profiling something specific.
# (task, metric key, display name, icon)
_MINOR_CPU_TASKS: list[tuple[str, str, str, str]] = [
    ("aircraft_json", "cpu_aircraft_json_pct", "aircraft.json", "mdi:code-json"),
    ("globe_json", "cpu_globe_json_pct", "globe.json", "mdi:earth"),
    ("binCraft", "cpu_bincraft_pct", "binCraft", "mdi:file-code-outline"),
    ("trace_json", "cpu_trace_json_pct", "traces", "mdi:chart-timeline-variant"),
    (
        "heatmap_and_state",
        "cpu_heatmap_state_pct",
        "heatmap/state",
        "mdi:grid",
    ),
    ("api_workers", "cpu_api_workers_pct", "API workers", "mdi:api"),
    ("api_update", "cpu_api_update_pct", "API update", "mdi:api"),
    ("remove_stale", "cpu_remove_stale_pct", "remove stale", "mdi:broom"),
]


def _minor_cpu_metric(task: str, key: str, label: str, icon: str) -> "Metric":
    """Build one hidden CPU sensor. `task` is a parameter of THIS function, so
    each call closes over its own value -- no late-binding hazard even though
    the callers build these in a comprehension."""

    def extract(s: dict[str, Any]) -> float | int | None:
        return _cpu_pct(s, task)

    return Metric(
        key,
        f"readsb CPU ({label})",
        "%",
        None,
        "measurement",
        icon,
        2,  # these sit near 0.03%, so 1dp would round most of them to 0.0
        extract,
        enabled_default=False,
    )


# readsb's own performance, from the same stats.json. Diagnostics: they answer
# "is the receiver keeping up?", which nothing else in HA exposes. The three
# headline CPU workers and the SDR health sensors are visible; anything that
# reads ~0 on a healthy station ships hidden so it isn't permanent noise.
PERFORMANCE_METRICS: list[Metric] = [
    Metric(
        "cpu_reader_pct",
        "readsb CPU (reader)",
        "%",
        None,
        "measurement",
        "mdi:usb-port",
        1,
        lambda s: _cpu_pct(s, "reader"),
    ),
    Metric(
        "cpu_demod_pct",
        "readsb CPU (demod)",
        "%",
        None,
        "measurement",
        "mdi:sine-wave",
        1,
        lambda s: _cpu_pct(s, "demod"),
    ),
    Metric(
        "cpu_background_pct",
        "readsb CPU (background)",
        "%",
        None,
        "measurement",
        "mdi:cog-outline",
        1,
        lambda s: _cpu_pct(s, "background"),
    ),
    # Positions readsb decoded but threw out as impossible -- the CPR global
    # decode failing its consistency check. Cumulative, so a flat line is
    # healthy and a climbing one means interference, multipath or a bad frame
    # source.
    Metric(
        "cpr_bad_positions",
        "Bad Position Decodes",
        None,
        None,
        "total_increasing",
        "mdi:map-marker-alert",
        0,
        lambda s: _num(_get(s, "total", "cpr", "global_bad")),
        enabled_default=False,
    ),
    *(_minor_cpu_metric(*t) for t in _MINOR_CPU_TASKS),
]


def compute_performance_metrics(
    stats: dict[str, Any],
) -> dict[str, float | int | None]:
    """Map a parsed stats.json into {performance_metric_key: value}."""
    return {m.key: m.extract(stats) for m in PERFORMANCE_METRICS}


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
# Per-portal decode rates, reported by the feeder client itself (already
# per-second, no rate maths). These are the equivalents of the retired
# Multi-Portal add-on's planefinder_mode_s_rate / _mode_ac_rate / _bandwidth.
PORTAL_RATE_METRICS: list[FeederMetric] = [
    FeederMetric(
        "portal_message_rate",
        "Message Rate",
        "msg/s",
        None,
        "measurement",
        "mdi:message-fast-outline",
        0,
    ),
    FeederMetric(
        "portal_modeac_rate",
        "Mode A/C Rate",
        "msg/s",
        None,
        "measurement",
        "mdi:radio-tower",
        0,
        enabled_default=False,
    ),
    FeederMetric(
        "portal_receive_rate",
        "Receiver Data Rate",
        "B/s",
        "data_rate",
        "measurement",
        "mdi:download-network",
        0,
    ),
]

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
#   SYNC   (peer_count / good_sync %) come from the mlat *server* push -- not
#          every server sends it (see MLAT_SYNC_CAPABLE). Enabled.
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
    # Distinct from samples_dropped: "dropped" is readsb discarding samples it
    # could not keep up with, "lost" is the USB layer never delivering them.
    # Both mean the receiver is falling behind, but they point at different
    # causes (CPU vs USB/cabling), so they are separate sensors.
    Metric(
        "sdr_samples_lost",
        "SDR Samples Lost",
        None,
        None,
        "total_increasing",
        "mdi:alert-octagon-outline",
        0,
        lambda s: _num(_get(s, "total", "local", "samples_lost")),
        enabled_default=False,
    ),
    # Messages received above the strong-signal threshold in the last minute.
    # The classic "gain is too high" indicator -- read it alongside SDR Gain.
    Metric(
        "sdr_strong_signals",
        "SDR Strong Signals",
        None,
        None,
        "measurement",
        "mdi:signal-cellular-3",
        0,
        lambda s: _num(_get(s, "last1min", "local", "strong_signals")),
    ),
    Metric(
        "sdr_peak_signal_dbfs",
        "SDR Peak Signal",
        "dBFS",
        None,
        "measurement",
        "mdi:waveform",
        1,
        lambda s: _num(_get(s, "last1min", "local", "peak_signal")),
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
# --- "Message Types" device (Mode S downlink-format breakdown) --------------
# DF (Downlink Format) is the first five bits of every Mode S message and says
# what kind of message it is. readsb publishes no per-DF breakdown anywhere, so
# these rates are counted off its Beast stream by beast.BeastDfCounter; the
# publisher hands us the {df: rate} snapshot, so the extract functions are unused
# (like the broker metrics). Own device so the nine sensors group together.
#
# The three that sit near zero on a typical station ship hidden, matching how the
# minor CPU workers are handled: DF5 and DF21 (identity replies, only sent when a
# radar asks for a squawk) and DF18 (TIS-B/ADS-R, rebroadcast ground services).
DF_DEVICE_ID = "aviation_feeder_message_types"
DF_DEVICE_NAME = "Aviation Feeder — Message Types"

# (df, key, display name, icon, enabled_default)
_DF_SENSORS: list[tuple[int, str, str, str, bool]] = [
    (17, "df17_rate", "ADS-B (DF17)", "mdi:broadcast", True),
    (11, "df11_rate", "All-Call Reply (DF11)", "mdi:account-question-outline", True),
    (0, "df0_rate", "TCAS Short (DF0)", "mdi:airplane-alert", True),
    (16, "df16_rate", "TCAS Long (DF16)", "mdi:airplane-alert", True),
    (4, "df4_rate", "Altitude Reply (DF4)", "mdi:altimeter", True),
    (20, "df20_rate", "Comm-B Altitude (DF20)", "mdi:altimeter", True),
    (5, "df5_rate", "Identity Reply (DF5)", "mdi:identifier", False),
    (21, "df21_rate", "Comm-B Identity (DF21)", "mdi:identifier", False),
    (18, "df18_rate", "TIS-B / ADS-R (DF18)", "mdi:satellite-uplink", False),
]

DF_METRICS: list[Metric] = [
    Metric(
        key,
        name,
        "msg/s",
        None,
        "measurement",
        icon,
        2,
        lambda s: None,  # filled by the publisher from the Beast snapshot
        enabled_default=on,
    )
    for _df, key, name, icon, on in _DF_SENSORS
]

# metric key -> the DF number the counter reports it under.
DF_KEY_BY_NUMBER: dict[int, str] = {df: key for df, key, _n, _i, _e in _DF_SENSORS}


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
