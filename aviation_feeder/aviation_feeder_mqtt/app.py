# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Entry-point orchestration: option parsing, MQTT lifecycle, and the
stats.json read/publish loop. Built around MqttHealth, connect-with-retry,
LWT availability, HA discovery + birth-message resubscribe, disconnect/publish-
stall watchdogs that exit for the supervisor to restart, and graceful
offline-on-shutdown. Targets paho-mqtt 2.x."""

import argparse
import json
import os
import signal
import time
from typing import Any

import paho.mqtt.client as mqtt

from . import __version__
from .feeders import (
    THROUGHPUT_KERNEL,
    _truthy,
    compute_feeder_status,
    compute_feeder_uptime,
    read_connector_status,
    running_cmdlines_by_pid,
)
from .metadata import (
    EMERGENCY_SQUAWK_KEY,
    FEEDERS_DEVICE_ID,
    MESSAGES_METRICS,
    UNIQUE_TODAY_KEY,
    MESSAGES_RATE_METRICS,
    MLAT_RESULT_METRICS,
    MLAT_SYNC_METRICS,
    NEARBY_METRICS,
    NEARBY_STATE_KEY,
    PORTAL_AIRCRAFT_METRICS,
    REPORT_BINARY_SENSORS,
    THROUGHPUT_METRICS,
    THROUGHPUT_RATE_METRICS,
    UPTIME_METRICS,
    compute_metrics,
    compute_sdr_metrics,
    compute_uat_metrics,
)
from .app_reports import gather_reports
from .mlat_stats import MLAT_CAPABLE, MLAT_SYNC_CAPABLE, read_mlat_stats
from .mqtt import (
    MqttHealth,
    build_broker_discovery,
    build_discovery_payloads,
    build_emergency_discovery,
    build_feeder_metrics_discovery,
    build_feeders_discovery,
    build_nearby_discovery,
    build_report_binary_discovery,
    build_sdr_discovery,
    build_uat_discovery,
    build_unique_discovery,
    connect_mqtt_with_retry,
    mqtt_publish,
)
from .emergency import compute_emergency
from .nearby import compute_nearby, read_aircraft
from .unique_daily import UniqueDailyTracker
from .stats import read_stats
from .uat_stats import UAT_STATS_PATH, read_uat_stats
from .throughput import ThroughputAccumulator
from .supervisor import resolve_mqtt_service
from .util import log

# Every per-feeder metric suffix that CAN exist, derived from the metric groups
# themselves so the stale-entity cleanup below can't drift out of sync when a
# group changes (add a FeederMetric -> its removal is handled automatically).
_ALL_FEEDER_METRIC_SUFFIXES: tuple[str, ...] = tuple(
    m.suffix
    for grp in (
        THROUGHPUT_METRICS,
        THROUGHPUT_RATE_METRICS,
        MESSAGES_METRICS,
        MESSAGES_RATE_METRICS,
        UPTIME_METRICS,
        MLAT_SYNC_METRICS,
        MLAT_RESULT_METRICS,
        PORTAL_AIRCRAFT_METRICS,
    )
    for m in grp
)

# Per-feeder metric applicability — single source of truth for discovery (the
# state-publish loops below feed the same suffixes from each metric's data
# source). Byte throughput is measurable for the kernel-TCP feeders plus pfclient
# (its own byte counters); fr24's UDP feed has no byte counter, so it exposes a
# message count instead. (MLAT applicability lives in mlat_stats: MLAT_CAPABLE /
# MLAT_SYNC_CAPABLE.)
_BYTE_FEEDERS = frozenset(THROUGHPUT_KERNEL) | {"planefinder"}
_MESSAGE_FEEDERS = frozenset({"fr24"})
# Feeders whose client reports the aggregator's own aircraft view (app_reports).
# Only fr24 so far; radarbox/adsbx/planefinder follow in their own changes.
_PORTAL_AIRCRAFT_FEEDERS = frozenset({"fr24"})


class RateTracker:
    """Per-second rate from cumulative counters across publish cycles. rate()
    returns None until a baseline exists, and clamps a negative delta (a counter
    reset) to 0."""

    def __init__(self) -> None:
        self._prev: dict = {}  # (key, suffix) -> (cumulative_value, timestamp)

    def rate(self, key, suffix, cur, ts):
        prev = self._prev.get((key, suffix))
        self._prev[(key, suffix)] = (cur, ts)
        if prev is None or ts <= prev[1]:
            return None
        return max(0.0, (cur - prev[0]) / (ts - prev[1]))


class PlanefinderFeedState:
    """pfclient's 'connected' derived from its cumulative master_server_bytes_out:
    a positive delta between cycles = feeding. First cycle is optimistic
    (bool(cur)) and self-corrects next cycle. Mirrors PlaneFinder's healthcheck;
    pfclient_report deliberately omits 'connected' so this is the only source."""

    def __init__(self) -> None:
        self._prev = None

    def connected(self, bytes_sent):
        prev = self._prev
        self._prev = bytes_sent
        if prev is None:
            return bool(bytes_sent)
        return bytes_sent is not None and bytes_sent > prev


def assemble_feeder_discovery(
    discovery_prefix,
    feeders_topic,
    availability_topic,
    expire_after_s,
    fstat,
    via_parent,
):
    """Full per-feeder discovery dict (connection binary_sensor + the applicable
    metric groups per feeder + report binary_sensors), keyed by config topic.
    `fstat` is compute_feeder_status()'s [(key, name, connected)]."""

    def sub(pred):
        return [(k, n) for k, n, _c in fstat if pred(k)]

    def fm(feeders, metrics):
        return build_feeder_metrics_discovery(
            discovery_prefix,
            feeders_topic,
            availability_topic,
            expire_after_s,
            feeders,
            metrics,
            via_parent,
        )

    return {
        # connection binary_sensor + uptime for every enabled feeder
        **build_feeders_discovery(
            discovery_prefix,
            feeders_topic,
            availability_topic,
            expire_after_s,
            fstat,
            via_parent,
        ),
        **fm(fstat, UPTIME_METRICS),
        # byte throughput (kernel-TCP feeders + pfclient) + its rates; fr24 msgs + rate
        **fm(sub(lambda k: k in _BYTE_FEEDERS), THROUGHPUT_METRICS),
        **fm(sub(lambda k: k in _MESSAGE_FEEDERS), MESSAGES_METRICS),
        **fm(sub(lambda k: k in _BYTE_FEEDERS), THROUGHPUT_RATE_METRICS),
        **fm(sub(lambda k: k in _MESSAGE_FEEDERS), MESSAGES_RATE_METRICS),
        # the aggregator's own aircraft view (differs from ours, by design)
        **fm(sub(lambda k: k in _PORTAL_AIRCRAFT_FEEDERS), PORTAL_AIRCRAFT_METRICS),
        # MLAT peers/sync (server-pushed; all but RadarBox) + positions/aircraft (all)
        **fm(sub(lambda k: k in MLAT_SYNC_CAPABLE), MLAT_SYNC_METRICS),
        **fm(sub(lambda k: k in MLAT_CAPABLE), MLAT_RESULT_METRICS),
        # feeder self-report binary_sensors (piaware MLAT / Radio)
        **build_report_binary_discovery(
            discovery_prefix,
            feeders_topic,
            availability_topic,
            expire_after_s,
            sub(lambda k: any(k == e[0] for e in REPORT_BINARY_SENSORS)),
            via_parent,
        ),
    }


def _coord(opt_val: Any, env_val: str | None) -> float | None:
    """Station coordinate from the option, else the LAT/LONG env the config
    bridge resolved (e.g. inherited from Home Assistant when the option is left
    blank). Without this fallback, blank lat/long silently disables planes-near-me
    even though the receiver is geolocated."""
    for v in (opt_val, env_val):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.strip():
            try:
                return float(v)
            except ValueError:
                pass
    return None


def _publish_toggleable_discovery(
    client: mqtt.Client,
    disc: dict[str, dict[str, Any]],
    enabled: bool,
    *,
    log_level: str,
    health: MqttHealth,
) -> int:
    """Publish a main-device discovery dict retained: the real config when
    `enabled`, else an empty payload so HA removes the entity when the feature is
    toggled off. Returns the number of live configs published (0 when disabled)
    for the discovery-count log. Shared by the toggleable main-device sensors
    (SDR, UAT, unique-today, emergency-squawk)."""
    for topic, cfg in disc.items():
        body = json.dumps(cfg, separators=(",", ":")) if enabled else ""
        mqtt_publish(
            client,
            topic,
            body,
            qos=1,
            retain=True,
            log_level=log_level,
            health=health,
        )
    return len(disc) if enabled else 0


STATS_PATH = "/run/readsb/stats.json"
AIRCRAFT_PATH = "/run/readsb/aircraft.json"

# Exit codes: s6 supervises this longrun and restarts it on non-zero exit.
EXIT_DISCONNECTED = 11  # MQTT down longer than the configured timeout
EXIT_PUBLISH_STALL = 12  # connected but state publishes stopped landing
EXIT_LOOP_ERROR = 14  # unexpected exception in the main loop

# Internal tuning (not user-facing options).
CLIENT_ID = "aviation-feeder-mqtt"
DISCONNECT_TIMEOUT_S = 300  # exit for supervisor restart if MQTT is down this long
EXPIRE_AFTER_MULTIPLIER = 4  # HA expire_after = interval * this (floored at 60s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--options", required=True)
    ap.add_argument("--stats", default=STATS_PATH)
    ap.add_argument("--aircraft", default=AIRCRAFT_PATH)
    ap.add_argument("--uat-stats", default=UAT_STATS_PATH)
    args = ap.parse_args()

    with open(args.options, "r", encoding="utf-8") as f:
        opts = json.load(f)

    log_level = (opts.get("mqtt_log_level") or "INFO").upper()
    log("INFO", f"Aviation Feeder MQTT v{__version__} starting", log_level)

    interval = max(1.0, float(opts.get("mqtt_interval_seconds", 30)))
    discovery_prefix = opts.get("mqtt_discovery_prefix", "homeassistant")
    base_topic = (opts.get("mqtt_base_topic") or "aviation_feeder").rstrip("/")

    mqtt_host = (opts.get("mqtt_host") or "").strip()
    mqtt_port = int(opts.get("mqtt_port") or 1883)
    mqtt_username = opts.get("mqtt_username", "") or ""
    mqtt_password = opts.get("mqtt_password", "") or ""
    client_id = CLIENT_ID

    # Blank broker host -> resolve from the Supervisor `mqtt` service (the add-on
    # declares services: [mqtt:want]), so an authenticated Mosquitto add-on works
    # with no manual host/user/pass. Falls back to anonymous core-mosquitto.
    if not mqtt_host:
        svc = resolve_mqtt_service(log_level)
        if svc and svc.get("host"):
            mqtt_host = str(svc["host"])
            mqtt_port = int(svc.get("port") or mqtt_port)
            if not mqtt_username:
                mqtt_username = svc.get("username") or ""
            if not mqtt_password:
                mqtt_password = svc.get("password") or ""
            log("INFO", f"MQTT broker from Supervisor service: {mqtt_host}", log_level)
        else:
            mqtt_host = "core-mosquitto"

    disconnect_timeout = DISCONNECT_TIMEOUT_S
    expire_after_s = max(60, int(interval * EXPIRE_AFTER_MULTIPLIER))

    availability_topic = f"{base_topic}/availability"
    heartbeat_topic = f"{base_topic}/heartbeat"
    nearby_topic = f"{base_topic}/nearby"

    feeder_health = bool(opts.get("ha_feeder_health", True))
    planes_near_me = bool(opts.get("ha_planes_near_me", True))
    feeder_status = bool(opts.get("ha_feeder_status", True))
    emergency_on = bool(opts.get("ha_emergency_squawk", True))
    unique_on = bool(opts.get("ha_unique_today", True))
    near_me_radius = max(1.0, float(opts.get("ha_near_me_radius", 50)))
    feeders_topic = f"{base_topic}/feeders"
    # Local-SDR health only makes sense with a local dongle; in remote/net-only
    # mode readsb owns no SDR, so skip the SDR device entirely. Decided from
    # config (deterministic at startup), not from stats timing.
    receiver_mode = (opts.get("receiver_mode") or "rtlsdr").strip().lower()
    sdr_present = receiver_mode != "remote"
    sdr_topic = f"{base_topic}/sdr"
    # UAT device: only when 978 is decoded locally — uat-only mode, or rtlsdr mode
    # with enable_uat on (the same gate as the uat-stats service). In remote mode
    # there is no local dump978, so no UAT stats device.
    uat_present = receiver_mode == "uat" or (
        receiver_mode == "rtlsdr" and bool(opts.get("enable_uat"))
    )
    uat_topic = f"{base_topic}/uat"
    # Fall back to the LAT/LONG the bridge resolved (incl. HA-inherited location)
    # so blank lat/long options don't disable planes-near-me.
    station_lat = _coord(opts.get("lat"), os.environ.get("LAT"))
    station_lon = _coord(opts.get("long"), os.environ.get("LONG"))
    station_ok = station_lat is not None and station_lon is not None
    if planes_near_me and not station_ok:
        log(
            "WARNING",
            "planes-near-me enabled but lat/long is not set; disabling it",
            log_level,
        )
        planes_near_me = False

    log(
        "INFO",
        "\n".join(
            [
                "Configuration:",
                f"  base_topic:         {base_topic}",
                f"  client_id:          {client_id}",
                f"  disconnect_timeout: {disconnect_timeout}s",
                f"  discovery_prefix:   {discovery_prefix}",
                f"  interval:           {interval}s",
                f"  log_level:          {log_level}",
                f"  mqtt_host:          {mqtt_host}:{mqtt_port}",
                f"  mqtt_username:      {mqtt_username or '(none)'}",
                f"  expire_after:       {expire_after_s}s",
                f"  feeder_health:      {feeder_health}",
                f"  planes_near_me:     {planes_near_me}"
                + (f" (radius {near_me_radius:g} nmi)" if planes_near_me else ""),
                f"  stats_path:         {args.stats}",
            ]
        ),
        log_level,
    )

    health = MqttHealth()
    need_discovery = {"v": True}
    throughput = ThroughputAccumulator()
    rates = RateTracker()
    pf_state = PlanefinderFeedState()
    unique_tracker = UniqueDailyTracker()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
    )
    if mqtt_username:
        client.username_pw_set(mqtt_username, mqtt_password)
    client.will_set(availability_topic, "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        if not reason_code.is_failure:
            health.connected = True
            health.last_connect_ok = time.time()
            health.connect_count += 1
            log("INFO", f"MQTT connected to {mqtt_host}:{mqtt_port}", log_level)
            # HA republishes "online" on restart; resubscribe so we re-send
            # discovery when it comes back.
            _client.subscribe(f"{discovery_prefix}/status", qos=1)
            need_discovery["v"] = True
            mqtt_publish(
                _client,
                availability_topic,
                "online",
                qos=1,
                retain=True,
                log_level=log_level,
                health=health,
            )
        else:
            health.connected = False
            log("ERROR", f"MQTT connect failed: {reason_code}", log_level)

    def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
        health.connected = False
        health.last_disconnect = time.time()
        log("WARNING", f"MQTT disconnected ({reason_code})", log_level)

    def on_message(_client, _userdata, msg):
        if msg.payload.decode(errors="replace").strip() == "online":
            log(
                "INFO",
                "HA birth message received — will republish discovery",
                log_level,
            )
            need_discovery["v"] = True

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    log("INFO", f"Connecting MQTT to {mqtt_host}:{mqtt_port}", log_level)
    connect_mqtt_with_retry(client, mqtt_host, mqtt_port, log_level)
    client.loop_start()

    stop = {"v": False}

    def handle(_sig, _frame):
        stop["v"] = True

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    last_stats_ok = 0.0

    try:
        while not stop["v"]:
            now = time.time()

            if (
                not health.connected
                and health.last_disconnect > 0
                and (now - health.last_disconnect) > disconnect_timeout
            ):
                log(
                    "ERROR",
                    f"MQTT disconnected for {now - health.last_disconnect:.0f}s "
                    f"(> {disconnect_timeout}s). Exiting for supervisor restart.",
                    log_level,
                )
                return EXIT_DISCONNECTED

            stats = read_stats(args.stats)
            if stats is None:
                log("WARNING", f"stats.json not readable at {args.stats}", log_level)
            else:
                last_stats_ok = now

            if health.connected and need_discovery["v"]:
                # Publish (or, for a disabled category, retained-empty to remove)
                # discovery for both devices so toggling ha_feeder_health /
                # ha_planes_near_me adds or cleans up entities in HA.
                feeder_disc = build_discovery_payloads(
                    discovery_prefix, base_topic, availability_topic, expire_after_s
                )
                nearby_disc = build_nearby_discovery(
                    discovery_prefix, nearby_topic, availability_topic, expire_after_s
                )
                published = 0
                for topic, cfg in feeder_disc.items():
                    body = (
                        json.dumps(cfg, separators=(",", ":")) if feeder_health else ""
                    )
                    mqtt_publish(
                        client,
                        topic,
                        body,
                        qos=1,
                        retain=True,
                        log_level=log_level,
                        health=health,
                    )
                    published += 1 if feeder_health else 0
                for topic, cfg in nearby_disc.items():
                    body = (
                        json.dumps(cfg, separators=(",", ":")) if planes_near_me else ""
                    )
                    mqtt_publish(
                        client,
                        topic,
                        body,
                        qos=1,
                        retain=True,
                        log_level=log_level,
                        health=health,
                    )
                    published += 1 if planes_near_me else 0
                if feeder_status:
                    fstat = compute_feeder_status(opts)  # enumeration only here

                    # via_parent: only nest feeder devices under the main device
                    # when it's actually registered (ha_feeder_health on).
                    feeders_disc = assemble_feeder_discovery(
                        discovery_prefix,
                        feeders_topic,
                        availability_topic,
                        expire_after_s,
                        fstat,
                        feeder_health,
                    )
                    for topic, cfg in feeders_disc.items():
                        mqtt_publish(
                            client,
                            topic,
                            json.dumps(cfg, separators=(",", ":")),
                            qos=1,
                            retain=True,
                            log_level=log_level,
                            health=health,
                        )
                        published += 1
                    # Remove any per-feeder metric entity that no longer applies
                    # (e.g. the dropped aggregator/fr24 byte sensors from an older
                    # build): publish an empty retained config so HA deletes it
                    # instead of leaving it "unavailable".
                    for key, _n, _c in fstat:
                        for suf in _ALL_FEEDER_METRIC_SUFFIXES:
                            topic = (
                                f"{discovery_prefix}/sensor/{FEEDERS_DEVICE_ID}"
                                f"/{key}_{suf}/config"
                            )
                            if topic not in feeders_disc:
                                mqtt_publish(
                                    client,
                                    topic,
                                    "",
                                    qos=1,
                                    retain=True,
                                    log_level=log_level,
                                    health=health,
                                )
                # Toggleable main-device discovery. Each publishes its real config
                # when its feature is on, else retained-empty to remove the entity.
                # SDR + UAT are hardware-gated (local dongle / local 978 decode);
                # unique-today + emergency-squawk are option-gated. All share
                # _publish_toggleable_discovery (returns the live-config count).
                sdr_on = feeder_health and sdr_present
                uat_on = feeder_health and uat_present
                published += _publish_toggleable_discovery(
                    client,
                    build_sdr_discovery(
                        discovery_prefix, sdr_topic, availability_topic, expire_after_s
                    ),
                    sdr_on,
                    log_level=log_level,
                    health=health,
                )
                published += _publish_toggleable_discovery(
                    client,
                    build_unique_discovery(
                        discovery_prefix, base_topic, availability_topic, expire_after_s
                    ),
                    unique_on,
                    log_level=log_level,
                    health=health,
                )
                published += _publish_toggleable_discovery(
                    client,
                    build_emergency_discovery(
                        discovery_prefix, base_topic, availability_topic, expire_after_s
                    ),
                    emergency_on,
                    log_level=log_level,
                    health=health,
                )
                published += _publish_toggleable_discovery(
                    client,
                    build_uat_discovery(
                        discovery_prefix, uat_topic, availability_topic, expire_after_s
                    ),
                    uat_on,
                    log_level=log_level,
                    health=health,
                )
                # MQTT broker-link diagnostics (main device), under feeder_health.
                broker_disc = build_broker_discovery(
                    discovery_prefix, base_topic, availability_topic, expire_after_s
                )
                for topic, cfg in broker_disc.items():
                    body = (
                        json.dumps(cfg, separators=(",", ":")) if feeder_health else ""
                    )
                    mqtt_publish(
                        client,
                        topic,
                        body,
                        qos=1,
                        retain=True,
                        log_level=log_level,
                        health=health,
                    )
                    published += 1 if feeder_health else 0
                mqtt_publish(
                    client,
                    availability_topic,
                    "online",
                    qos=1,
                    retain=True,
                    log_level=log_level,
                    health=health,
                )
                need_discovery["v"] = False
                log(
                    "INFO",
                    f"Published discovery for {published} sensors "
                    f"(feeder_health={feeder_health}, planes_near_me={planes_near_me})",
                    log_level,
                )

            if health.connected and (
                feeder_health
                or planes_near_me
                or feeder_status
                or emergency_on
                or unique_on
            ):
                if feeder_health and stats is not None:
                    metrics = compute_metrics(stats)
                    n = 0
                    for key, val in metrics.items():
                        if val is None:
                            continue
                        mqtt_publish(
                            client,
                            f"{base_topic}/{key}/state",
                            str(val),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                            mark_state=True,
                        )
                        n += 1
                    log("DEBUG", f"Published {n} feeder-health states", log_level)

                if feeder_health and sdr_present and stats is not None:
                    for key, val in compute_sdr_metrics(stats).items():
                        if val is None:
                            continue
                        mqtt_publish(
                            client,
                            f"{sdr_topic}/{key}/state",
                            str(val),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                            mark_state=True,
                        )

                # UAT stats.json is written by the uat-stats service ~once/minute;
                # absent until then (or in remote mode) -> read returns None, skip.
                if feeder_health and uat_present:
                    ustats = read_uat_stats(args.uat_stats)
                    if ustats is not None:
                        for key, val in compute_uat_metrics(ustats).items():
                            if val is None:
                                continue
                            mqtt_publish(
                                client,
                                f"{uat_topic}/{key}/state",
                                str(val),
                                qos=0,
                                retain=False,
                                log_level=log_level,
                                health=health,
                                mark_state=True,
                            )

                if feeder_health:
                    uptime_s = (
                        int(now - health.last_connect_ok)
                        if health.last_connect_ok
                        else 0
                    )
                    mqtt_publish(
                        client,
                        f"{base_topic}/mqtt_uptime/state",
                        str(uptime_s),
                        qos=0,
                        retain=False,
                        log_level=log_level,
                        health=health,
                        mark_state=True,
                    )
                    mqtt_publish(
                        client,
                        f"{base_topic}/mqtt_reconnects/state",
                        str(max(0, health.connect_count - 1)),
                        qos=0,
                        retain=False,
                        log_level=log_level,
                        health=health,
                        mark_state=True,
                    )

                # aircraft.json feeds planes-near-me, the emergency-squawk sensor,
                # and the unique-aircraft-today counter; read it once per cycle
                # when any of them is enabled.
                want_nearby = (
                    planes_near_me
                    and station_lat is not None
                    and station_lon is not None
                )
                want_acj = want_nearby or emergency_on or unique_on
                acj = read_aircraft(args.aircraft) if want_acj else None
                if want_acj and acj is None:
                    log(
                        "WARNING",
                        f"aircraft.json not readable at {args.aircraft}",
                        log_level,
                    )

                if (
                    want_nearby
                    and acj is not None
                    and station_lat is not None
                    and station_lon is not None
                ):
                    nb = compute_nearby(acj, station_lat, station_lon, near_me_radius)
                    for m in NEARBY_METRICS:
                        v = nb.get(m.key)
                        if v is None:
                            continue
                        mqtt_publish(
                            client,
                            f"{nearby_topic}/{m.key}/state",
                            str(v),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                            mark_state=True,
                        )
                    nearest = nb.get("nearest")
                    if nearest:
                        mqtt_publish(
                            client,
                            f"{nearby_topic}/{NEARBY_STATE_KEY}/state",
                            str(nearest.get("flight") or ""),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                            mark_state=True,
                        )
                        mqtt_publish(
                            client,
                            f"{nearby_topic}/{NEARBY_STATE_KEY}/attributes",
                            json.dumps(nearest, separators=(",", ":")),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                        )
                    log(
                        "DEBUG",
                        f"nearby: in_range={nb.get('aircraft_in_range')}",
                        log_level,
                    )

                if emergency_on and acj is not None:
                    em = compute_emergency(acj)
                    mqtt_publish(
                        client,
                        f"{base_topic}/{EMERGENCY_SQUAWK_KEY}/state",
                        "on" if em["active"] else "off",
                        qos=0,
                        retain=False,
                        log_level=log_level,
                        health=health,
                        mark_state=True,
                    )
                    mqtt_publish(
                        client,
                        f"{base_topic}/{EMERGENCY_SQUAWK_KEY}/attributes",
                        json.dumps(
                            {"count": em["count"], "aircraft": em["aircraft"]},
                            separators=(",", ":"),
                        ),
                        qos=0,
                        retain=False,
                        log_level=log_level,
                        health=health,
                    )
                    if em["active"]:
                        log(
                            "INFO",
                            f"emergency squawk active: {em['count']} aircraft",
                            log_level,
                        )

                if unique_on and acj is not None:
                    count = unique_tracker.update(acj, time.localtime()[:3])
                    mqtt_publish(
                        client,
                        f"{base_topic}/{UNIQUE_TODAY_KEY}/state",
                        str(count),
                        qos=0,
                        retain=False,
                        log_level=log_level,
                        health=health,
                        mark_state=True,
                    )

                if feeder_status:
                    # Gather the app self-reports once: authoritative feeding-state
                    # + throughput for the TCP-invisible feeders (fr24 UDP, pfclient).
                    reports = gather_reports(opts, _truthy)
                    # pfclient feeding = its master-server bytes INCREASED since the
                    # last cycle (the raw counter is cumulative, so >0 is true
                    # forever). First cycle has no baseline -> optimistic if it has
                    # ever sent; self-corrects next cycle if the feed is actually dead.
                    pf_rep = reports.get("planefinder")
                    if pf_rep is not None:
                        pf_rep["connected"] = pf_state.connected(
                            pf_rep.get("bytes_sent")
                        )

                    def _pub(suffix, key, val):
                        mqtt_publish(
                            client,
                            f"{feeders_topic}/{key}/{suffix}/state",
                            str(val),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                            mark_state=True,
                        )

                    # Scan /proc + stats.prom ONCE this cycle and thread the
                    # results into every consumer (status, throughput, uptime), so
                    # we don't re-scan 2-3x and can't get a mid-cycle-inconsistent
                    # view between them.
                    cmd_by_pid = running_cmdlines_by_pid()
                    connectors = read_connector_status()
                    enabled_keys = set()
                    for key, _name, connected in compute_feeder_status(
                        opts,
                        connectors=connectors,
                        cmd_by_pid=cmd_by_pid,
                        reports=reports,
                    ):
                        enabled_keys.add(key)
                        mqtt_publish(
                            client,
                            f"{feeders_topic}/{key}/state",
                            "on" if connected else "off",
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                            mark_state=True,
                        )

                    def _bytes(key, sent, recv):
                        # cumulative counters (disabled-by-default entities) + the
                        # primary per-second rates.
                        _pub("bytes_sent", key, sent)
                        _pub("bytes_received", key, recv)
                        rs = rates.rate(key, "bytes_sent", sent, now)
                        if rs is not None:
                            _pub("bytes_sent_rate", key, round(rs, 1))
                        rr = rates.rate(key, "bytes_received", recv, now)
                        if rr is not None:
                            _pub("bytes_received_rate", key, round(rr, 1))

                    # Byte throughput: kernel per-socket counters (TCP feeders)...
                    for key, (sent, recv) in throughput.update(
                        opts, cmd_by_pid=cmd_by_pid
                    ).items():
                        _bytes(key, sent, recv)
                    # ...plus pfclient's own byte counters (feeds off-TCP).
                    pf = reports.get("planefinder")
                    if pf and "planefinder" in enabled_keys and "bytes_sent" in pf:
                        _bytes(
                            "planefinder", pf["bytes_sent"], pf.get("bytes_received", 0)
                        )
                    # fr24 message count (UDP feed has no byte counter) + msg/s.
                    fr = reports.get("fr24")
                    if fr and "fr24" in enabled_keys and "messages" in fr:
                        _pub("messages", "fr24", fr["messages"])
                        mr = rates.rate("fr24", "messages", fr["messages"], now)
                        if mr is not None:
                            _pub("messages_rate", "fr24", round(mr, 1))
                    # Per-portal aircraft counts (the aggregator's own view).
                    for key in _PORTAL_AIRCRAFT_FEEDERS & enabled_keys:
                        rep = reports.get(key)
                        if not rep:
                            continue
                        for pm in PORTAL_AIRCRAFT_METRICS:
                            if pm.suffix in rep:
                                _pub(pm.suffix, key, rep[pm.suffix])
                    # Per-feeder MLAT sync (mlat-client --stats-json files).
                    for key, vals in read_mlat_stats().items():
                        if key not in enabled_keys:
                            continue
                        for suffix, val in vals.items():
                            _pub(suffix, key, val)
                    # Per-feeder uptime (aggregator connect-seconds / process age).
                    for key, secs in compute_feeder_uptime(
                        opts, connectors=connectors, cmd_by_pid=cmd_by_pid
                    ).items():
                        if key not in enabled_keys:
                            continue
                        _pub("uptime", key, secs)
                    # App self-reports -> attributes (semantic health: piaware
                    # MLAT/radio, fr24 feed status, …).
                    for key, attrs in reports.items():
                        if key not in enabled_keys:
                            continue
                        mqtt_publish(
                            client,
                            f"{feeders_topic}/{key}/attributes",
                            json.dumps(attrs, separators=(",", ":")),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                        )
                    # feeder self-report binary_sensors (piaware MLAT/Radio: on=green)
                    for key, suffix, _n, field, _icon in REPORT_BINARY_SENSORS:
                        rep = reports.get(key)
                        if key in enabled_keys and rep and field in rep:
                            _pub(suffix, key, "on" if rep[field] == "green" else "off")

            # Heartbeat (diagnostic; not an HA entity).
            hb = {
                "ts_ms": int(now * 1000),
                "connected": health.connected,
                "stats_age_s": round(now - last_stats_ok, 1) if last_stats_ok else None,
            }
            mqtt_publish(
                client,
                heartbeat_topic,
                json.dumps(hb, separators=(",", ":")),
                qos=0,
                retain=False,
                log_level=log_level,
                health=health,
            )

            # Publish-stall watchdog: only fire once we WERE publishing and then
            # stopped (a real MQTT stall). "Connected but the readsb source JSON
            # isn't readable yet" is not a stall -> don't crash-loop the service
            # when there is simply no data to publish.
            if (
                health.connected
                and health.last_state_publish_ok > 0
                and (now - health.last_state_publish_ok) > expire_after_s
            ):
                log(
                    "ERROR",
                    "MQTT state publishes stopped landing within the expire "
                    "window. Exiting for supervisor restart.",
                    log_level,
                )
                return EXIT_PUBLISH_STALL

            # Responsive sleep so SIGTERM is handled promptly.
            deadline = time.time() + interval
            while not stop["v"] and time.time() < deadline:
                time.sleep(0.2)

    except Exception as e:  # noqa: BLE001 - last-resort guard, logged + restarted
        log("ERROR", f"Main loop exception: {e}", log_level)
        return EXIT_LOOP_ERROR
    finally:
        stop["v"] = True
        try:
            mqtt_publish(
                client,
                availability_topic,
                "offline",
                qos=1,
                retain=True,
                log_level=log_level,
                health=health,
            )
            time.sleep(0.2)
        except Exception:
            pass
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    return 0
