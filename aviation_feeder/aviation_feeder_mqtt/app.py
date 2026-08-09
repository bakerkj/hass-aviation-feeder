# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Entry-point orchestration: option parsing, MQTT lifecycle, and the
stats.json read/publish loop. Built around MqttHealth, a reconnect loop with
capped backoff, LWT availability, HA discovery + birth-message resubscribe,
disconnect/publish-stall watchdogs that exit for the supervisor to restart, and
a bounded graceful offline-on-shutdown.

One asyncio loop drives all of it: aiomqtt for the broker, an asyncio task for
readsb's Beast stream, and to_thread for the few calls that still block. There
are no threads of our own and no ``signal.signal``, so a signal cannot be lost
-- ``add_signal_handler`` rides ``set_wakeup_fd``, which wakes the selector when
the signal lands rather than needing the main thread to be between bytecodes.

Every wait is bounded and every teardown is on a leash, because the supervisor
gives us ten seconds to stop and takes SIGKILL after."""

import argparse
import asyncio
import contextlib
import json
import os
import signal
import time
from typing import Any

import aiomqtt

from . import __version__
from .app_reports import filter_report, gather_reports
from .beast import BeastDfCounter
from .emergency import compute_emergency
from .feeders import (
    ALL_FEEDER_KEYS,
    THROUGHPUT_KERNEL,
    _truthy,
    compute_feeder_status,
    compute_feeder_uptime,
    read_connector_status,
    running_cmdlines_by_pid,
)
from .metadata import (
    DF_KEY_BY_NUMBER,
    EMERGENCY_SQUAWK_KEY,
    FEEDERS_DEVICE_ID,
    MESSAGES_METRICS,
    MESSAGES_RATE_METRICS,
    MLAT_RESULT_METRICS,
    MLAT_SYNC_METRICS,
    NEARBY_METRICS,
    NEARBY_STATE_KEY,
    PORTAL_AIRCRAFT_METRICS,
    PORTAL_RATE_METRICS,
    REPORT_BINARY_SENSORS,
    THROUGHPUT_METRICS,
    THROUGHPUT_RATE_METRICS,
    UNIQUE_TODAY_KEY,
    UPTIME_METRICS,
    compute_metrics,
    compute_performance_metrics,
    compute_remote_metrics,
    compute_sdr_metrics,
    compute_uat_metrics,
)
from .mlat_stats import MLAT_CAPABLE, MLAT_SYNC_CAPABLE, read_mlat_stats
from .mqtt import (
    Client,
    MqttHealth,
    build_broker_discovery,
    build_df_discovery,
    build_discovery_payloads,
    build_emergency_discovery,
    build_feeder_metrics_discovery,
    build_feeders_discovery,
    build_nearby_discovery,
    build_report_binary_discovery,
    build_sdr_discovery,
    build_uat_discovery,
    build_unique_discovery,
    mqtt_publish,
)
from .nearby import compute_nearby, read_aircraft
from .stats import read_stats
from .supervisor import resolve_mqtt_service
from .throughput import ThroughputAccumulator
from .uat_stats import UAT_STATS_PATH, read_uat_stats
from .unique_daily import UniqueDailyTracker
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
        PORTAL_RATE_METRICS,
    )
    for m in grp
)

# Report binary_sensors per feeder (piaware mlat_ok / radio_ok). Keyed by feeder
# because the association is load-bearing: only the owning feeder ever gets that
# topic, so flattening to a bare suffix list would make the retraction generate
# combinations that can never exist (adsblol_mlat_ok, fr24_radio_ok, ...).
_REPORT_BINARY_SUFFIXES_BY_KEY: dict[str, list[str]] = {}
for _rk, _rsuf, _rn, _rf, _ri in REPORT_BINARY_SENSORS:
    _REPORT_BINARY_SUFFIXES_BY_KEY.setdefault(_rk, []).append(_rsuf)

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
_PORTAL_AIRCRAFT_FEEDERS = frozenset({"fr24", "adsbexchange"})
# Feeders whose client reports its own per-second decode rates.
_PORTAL_RATE_FEEDERS = frozenset({"planefinder"})


EXIT_SIGNALS = (signal.SIGTERM, signal.SIGINT)

# Broker reconnect backoff, doubling to a cap. Capped rather than flat: a broker
# down for hours would otherwise draw a fresh TCP connect -- and a fresh DNS
# lookup, when mqtt_host is a name -- every few seconds for the whole outage.
_RECONNECT_MIN_SECONDS = 3
_RECONNECT_MAX_SECONDS = 60

# Ceiling on any single aiomqtt operation. aiomqtt defaults to 10s, which is the
# whole budget the supervisor gives us to stop: a qos=1 publish awaiting a PUBACK
# and the disconnect in __aexit__ can each burn the full 10s, so a broker holding
# the connection open without acking would push us past SIGKILL.
_MQTT_TIMEOUT_SECONDS = 5

# SUBSCRIBE needs a SUBACK too, and runs at session start where a signal cannot
# shorten it -- so it stays inside the stop budget rather than outside it.
_SUBSCRIBE_TIMEOUT_SECONDS = 2

# The farewell is best-effort on an even shorter leash: if the broker is not
# acking we are being killed regardless, and the retained will already says
# "offline", which is precisely the case it exists for.
_FAREWELL_TIMEOUT_SECONDS = 2


def _read_options(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise TypeError("options file must contain a JSON object")
    return data


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but return early once shutdown is requested."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _watch_birth(
    mq: aiomqtt.Client, need_discovery: dict[str, bool], log_level: str
) -> None:
    """Republish discovery when HA announces it has restarted."""
    async for message in mq.messages:
        if (
            message.payload
            and bytes(message.payload).decode(errors="replace").strip() == "online"
        ):
            log(
                "INFO",
                "HA birth message received — will republish discovery",
                log_level,
            )
            need_discovery["v"] = True


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


def mlat_states(stats, enabled_keys):
    """{(feeder_key, metric_suffix): value} for every MLAT sensor that has
    discovery, defaulting to 0 when there are no fresh stats.

    Policy: for an ENABLED MLAT feeder, absent or stale stats are reported as
    0 rather than not published. Nothing here inspects whether that feeder's
    mlat-client process is alive -- the function only sees the parsed stats and
    the enabled set.

    That policy is sound because mlat-client writes its --stats-json only AFTER
    establishing sync, so for a feeder the user has switched on, no stats means
    it is not syncing. Leaving the sensor to expire to "unavailable" hid that;
    0 states it. The residual case -- feeder enabled but its client crashed --
    also reports 0, which remains the right answer: MLAT is not working.

    This is only defensible because a feeder the user disabled no longer has
    entities at all (see stale_feeder_topics): every MLAT entity that exists
    belongs to an enabled feeder, so there is no remaining case where we are
    genuinely ignorant.

    Applicability mirrors assemble_feeder_discovery exactly -- peers/sync only
    for MLAT_SYNC_CAPABLE feeders -- so this can never publish a state for a
    sensor that was never advertised.
    """
    out: dict[tuple[str, str], float | int] = {}
    for key in sorted(MLAT_CAPABLE & set(enabled_keys)):
        vals = stats.get(key) or {}
        suffixes = [m.suffix for m in MLAT_RESULT_METRICS]
        if key in MLAT_SYNC_CAPABLE:
            suffixes += [m.suffix for m in MLAT_SYNC_METRICS]
        for suf in suffixes:
            v = vals.get(suf)
            out[(key, suf)] = v if isinstance(v, (int, float)) else 0
    return out


def stale_feeder_topics(discovery_prefix, published) -> list[str]:
    """Per-feeder discovery topics that should be retracted (empty retained
    payload) because this cycle did not publish them.

    Covers EVERY known feeder, not just the enabled ones. compute_feeder_status
    enumerates only enabled feeders, so a feeder the user has switched off is
    absent from that list -- anything iterating it can never reach the disabled
    feeder's topics, and its retained configs would sit in the broker forever
    while HA shows the entities as permanently "unavailable".

    All three per-feeder topic shapes are covered: the connection binary_sensor,
    the report binary_sensors (piaware mlat_ok / radio_ok), and the metrics.
    """
    out: list[str] = []
    for key in sorted(ALL_FEEDER_KEYS):
        candidates = [
            f"{discovery_prefix}/binary_sensor/{FEEDERS_DEVICE_ID}/{key}/config"
        ]
        candidates += [
            f"{discovery_prefix}/binary_sensor/{FEEDERS_DEVICE_ID}/{key}_{suf}/config"
            for suf in _REPORT_BINARY_SUFFIXES_BY_KEY.get(key, ())
        ]
        candidates += [
            f"{discovery_prefix}/sensor/{FEEDERS_DEVICE_ID}/{key}_{suf}/config"
            for suf in _ALL_FEEDER_METRIC_SUFFIXES
        ]
        out.extend(t for t in candidates if t not in published)
    return out


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
        **fm(sub(lambda k: k in _PORTAL_RATE_FEEDERS), PORTAL_RATE_METRICS),
        # MLAT peers/sync (server-pushed; not every server does) + positions/aircraft (all)
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


async def _publish_toggleable_discovery(
    client: Client,
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
        await mqtt_publish(
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


async def _run() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--options", required=True)
    ap.add_argument("--stats", default=STATS_PATH)
    ap.add_argument("--aircraft", default=AIRCRAFT_PATH)
    ap.add_argument("--uat-stats", default=UAT_STATS_PATH)
    args = ap.parse_args()

    # Off-loop like the other blocking reads, for one rule rather than an
    # exception to it -- even though this one runs once, before anything else
    # is scheduled.
    opts = await asyncio.to_thread(_read_options, args.options)

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
        # Off-loop: a Supervisor that is slow to answer must not delay
        # installing the signal handlers below.
        svc = await asyncio.to_thread(resolve_mqtt_service, log_level)
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
    df_topic = f"{base_topic}/message_types"
    # Mode S downlink-format rates: readsb publishes no per-DF breakdown, so a
    # dedicated task counts them off its Beast stream and the loop below
    # samples the tally each cycle (see beast.py). Separate toggle because it
    # holds a persistent socket, which the other sensor groups do not.
    df_on = bool(opts.get("ha_message_types", True))
    df_counter = BeastDfCounter() if df_on else None
    if df_counter is not None:
        df_counter.start()
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

    stop = asyncio.Event()
    event_loop = asyncio.get_running_loop()
    for signum in EXIT_SIGNALS:
        event_loop.add_signal_handler(signum, stop.set)

    last_stats_ok = 0.0

    async def publish_loop(mq: Client) -> int:
        """One connected session's worth of publishing.

        Nested so it closes over this function's configuration rather than
        taking thirty parameters. Broker faults propagate so the caller
        reconnects rather than treating them as fatal.
        """
        nonlocal last_stats_ok
        while not stop.is_set():
            now = time.time()

            # No disconnect check here: broker faults now propagate out of
            # mqtt_publish, so this loop has already exited by the time
            # health.connected can go False. The 300s watchdog lives in the
            # reconnect loop below, which is the only place that sees an
            # outage. Under paho this had to be here, because its network
            # thread could flip the flag while this loop kept running.
            stats = await asyncio.to_thread(read_stats, args.stats)
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
                    await mqtt_publish(
                        mq,
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
                    await mqtt_publish(
                        mq,
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
                        await mqtt_publish(
                            mq,
                            topic,
                            json.dumps(cfg, separators=(",", ":")),
                            qos=1,
                            retain=True,
                            log_level=log_level,
                            health=health,
                        )
                        published += 1
                    # Retract anything this cycle did not publish -- including
                    # every entity of a feeder the user has since disabled.
                    for topic in stale_feeder_topics(discovery_prefix, feeders_disc):
                        await mqtt_publish(
                            mq,
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
                published += await _publish_toggleable_discovery(
                    mq,
                    build_sdr_discovery(
                        discovery_prefix, sdr_topic, availability_topic, expire_after_s
                    ),
                    sdr_on,
                    log_level=log_level,
                    health=health,
                )
                published += await _publish_toggleable_discovery(
                    mq,
                    build_unique_discovery(
                        discovery_prefix, base_topic, availability_topic, expire_after_s
                    ),
                    unique_on,
                    log_level=log_level,
                    health=health,
                )
                published += await _publish_toggleable_discovery(
                    mq,
                    build_emergency_discovery(
                        discovery_prefix, base_topic, availability_topic, expire_after_s
                    ),
                    emergency_on,
                    log_level=log_level,
                    health=health,
                )
                published += await _publish_toggleable_discovery(
                    mq,
                    build_uat_discovery(
                        discovery_prefix, uat_topic, availability_topic, expire_after_s
                    ),
                    uat_on,
                    log_level=log_level,
                    health=health,
                )
                published += await _publish_toggleable_discovery(
                    mq,
                    build_df_discovery(
                        discovery_prefix, df_topic, availability_topic, expire_after_s
                    ),
                    df_on,
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
                    await mqtt_publish(
                        mq,
                        topic,
                        body,
                        qos=1,
                        retain=True,
                        log_level=log_level,
                        health=health,
                    )
                    published += 1 if feeder_health else 0
                await mqtt_publish(
                    mq,
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

            # Every sensor-group toggle must appear here, or that group's state
            # topics are never published while its discovery configs still are --
            # the entities register in HA and sit permanently unavailable.
            # ha_message_types was omitted when it was added, which is exactly
            # this failure. Evaluated at gate time because planes_near_me can be
            # switched off above when the station has no location.
            if health.connected and any(
                (
                    feeder_health,
                    planes_near_me,
                    feeder_status,
                    emergency_on,
                    unique_on,
                    df_on,
                )
            ):
                if feeder_health and stats is not None:
                    metrics = {
                        **compute_metrics(stats),
                        **compute_remote_metrics(stats),
                        **compute_performance_metrics(stats),
                    }
                    n = 0
                    for key, val in metrics.items():
                        if val is None:
                            continue
                        await mqtt_publish(
                            mq,
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
                        await mqtt_publish(
                            mq,
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
                    ustats = await asyncio.to_thread(read_uat_stats, args.uat_stats)
                    if ustats is not None:
                        for key, val in compute_uat_metrics(ustats).items():
                            if val is None:
                                continue
                            await mqtt_publish(
                                mq,
                                f"{uat_topic}/{key}/state",
                                str(val),
                                qos=0,
                                retain=False,
                                log_level=log_level,
                                health=health,
                                mark_state=True,
                            )

                # Mode S downlink-format rates. snapshot() returns {} on the
                # first cycle (no baseline interval yet) and while the Beast
                # reader is disconnected, so nothing fabricated is published.
                if df_counter is not None:
                    for df_num, rate in df_counter.snapshot().items():
                        df_key = DF_KEY_BY_NUMBER.get(df_num)
                        if df_key is None:
                            continue  # a DF we do not publish a sensor for
                        await mqtt_publish(
                            mq,
                            f"{df_topic}/{df_key}/state",
                            f"{rate:.2f}",
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
                    await mqtt_publish(
                        mq,
                        f"{base_topic}/mqtt_uptime/state",
                        str(uptime_s),
                        qos=0,
                        retain=False,
                        log_level=log_level,
                        health=health,
                        mark_state=True,
                    )
                    await mqtt_publish(
                        mq,
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
                acj = (
                    await asyncio.to_thread(read_aircraft, args.aircraft)
                    if want_acj
                    else None
                )
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
                        await mqtt_publish(
                            mq,
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
                        await mqtt_publish(
                            mq,
                            f"{nearby_topic}/{NEARBY_STATE_KEY}/state",
                            str(nearest.get("flight") or ""),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                            mark_state=True,
                        )
                        await mqtt_publish(
                            mq,
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
                    await mqtt_publish(
                        mq,
                        f"{base_topic}/{EMERGENCY_SQUAWK_KEY}/state",
                        "on" if em["active"] else "off",
                        qos=0,
                        retain=False,
                        log_level=log_level,
                        health=health,
                        mark_state=True,
                    )
                    await mqtt_publish(
                        mq,
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
                    await mqtt_publish(
                        mq,
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
                    # Off-loop: gather_reports shells out to HTTP endpoints
                    # (and a netlink socket) with their own timeouts, and this
                    # loop now owns MQTT I/O and the shutdown signal. Blocking
                    # here would stall both for seconds every cycle.
                    reports = await asyncio.to_thread(gather_reports, opts, _truthy)
                    # pfclient feeding = its master-server bytes INCREASED since the
                    # last cycle (the raw counter is cumulative, so >0 is true
                    # forever). First cycle has no baseline -> optimistic if it has
                    # ever sent; self-corrects next cycle if the feed is actually dead.
                    pf_rep = reports.get("planefinder")
                    if pf_rep is not None:
                        pf_rep["connected"] = pf_state.connected(
                            pf_rep.get("bytes_sent")
                        )

                    async def _pub(suffix, key, val):
                        await mqtt_publish(
                            mq,
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
                    # Off-loop: this opens /proc/<pid>/cmdline for every
                    # running process, and stats.prom on top. On one loop that
                    # stalls MQTT keepalive, the birth watcher and the shutdown
                    # signal for as long as the scan takes.
                    cmd_by_pid = await asyncio.to_thread(running_cmdlines_by_pid)
                    connectors = await asyncio.to_thread(read_connector_status)
                    enabled_keys = set()
                    for key, _name, connected in compute_feeder_status(
                        opts,
                        connectors=connectors,
                        cmd_by_pid=cmd_by_pid,
                        reports=reports,
                    ):
                        enabled_keys.add(key)
                        await mqtt_publish(
                            mq,
                            f"{feeders_topic}/{key}/state",
                            "on" if connected else "off",
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                            mark_state=True,
                        )

                    async def _bytes(key, sent, recv, now=now):
                        # cumulative counters (disabled-by-default entities) + the
                        # primary per-second rates.
                        await _pub("bytes_sent", key, sent)
                        await _pub("bytes_received", key, recv)
                        rs = rates.rate(key, "bytes_sent", sent, now)
                        if rs is not None:
                            await _pub("bytes_sent_rate", key, round(rs, 1))
                        rr = rates.rate(key, "bytes_received", recv, now)
                        if rr is not None:
                            await _pub("bytes_received_rate", key, round(rr, 1))

                    # Byte throughput: kernel per-socket counters (TCP feeders)...
                    # Off-loop: reads /proc/net/tcp via NETLINK_INET_DIAG.
                    throughput_by_key = await asyncio.to_thread(
                        throughput.update, opts, cmd_by_pid=cmd_by_pid
                    )
                    for key, (sent, recv) in throughput_by_key.items():
                        await _bytes(key, sent, recv)
                    # ...plus pfclient's own byte counters (feeds off-TCP).
                    pf = reports.get("planefinder")
                    if pf and "planefinder" in enabled_keys and "bytes_sent" in pf:
                        await _bytes(
                            "planefinder", pf["bytes_sent"], pf.get("bytes_received", 0)
                        )
                    # fr24 message count (UDP feed has no byte counter) + msg/s.
                    fr = reports.get("fr24")
                    if fr and "fr24" in enabled_keys and "messages" in fr:
                        await _pub("messages", "fr24", fr["messages"])
                        mr = rates.rate("fr24", "messages", fr["messages"], now)
                        if mr is not None:
                            await _pub("messages_rate", "fr24", round(mr, 1))
                    # Per-portal aircraft counts (the aggregator's own view).
                    for feeder_set, group in (
                        (_PORTAL_AIRCRAFT_FEEDERS, PORTAL_AIRCRAFT_METRICS),
                        (_PORTAL_RATE_FEEDERS, PORTAL_RATE_METRICS),
                    ):
                        for fkey in feeder_set & enabled_keys:
                            rep = reports.get(fkey)
                            if not rep:
                                continue
                            for pm in group:
                                if pm.suffix in rep:
                                    await _pub(pm.suffix, fkey, rep[pm.suffix])
                    # Per-feeder MLAT (mlat-client --stats-json files). Every
                    # advertised sensor gets a value each cycle -- 0 when the
                    # feeder is not syncing -- rather than being left to expire
                    # to "unavailable", which hid a known state.
                    mlat_raw = await asyncio.to_thread(read_mlat_stats)
                    for (key, suffix), val in mlat_states(
                        mlat_raw, enabled_keys
                    ).items():
                        await _pub(suffix, key, val)
                    # Per-feeder uptime (aggregator connect-seconds / process age).
                    # Off-loop: opens /proc/uptime plus /proc/<pid>/stat
                    # for each feeder.
                    uptime_by_key = await asyncio.to_thread(
                        compute_feeder_uptime,
                        opts,
                        connectors=connectors,
                        cmd_by_pid=cmd_by_pid,
                    )
                    for key, secs in uptime_by_key.items():
                        if key not in enabled_keys:
                            continue
                        await _pub("uptime", key, secs)
                    # App self-reports -> attributes (semantic health: piaware
                    # MLAT/radio, fr24 feed status, …). Re-apply the publish
                    # allowlist here: reports are enriched above (pfclient's
                    # derived `connected`) after gather_reports already filtered
                    # them, so this is the real last barrier before a vendor
                    # payload could reach the broker.
                    for key, attrs in reports.items():
                        if key not in enabled_keys:
                            continue
                        await mqtt_publish(
                            mq,
                            f"{feeders_topic}/{key}/attributes",
                            json.dumps(
                                filter_report(key, attrs), separators=(",", ":")
                            ),
                            qos=0,
                            retain=False,
                            log_level=log_level,
                            health=health,
                        )
                    # feeder self-report binary_sensors (piaware MLAT/Radio: on=green)
                    for key, suffix, _n, field, _icon in REPORT_BINARY_SENSORS:
                        rep = reports.get(key)
                        if key in enabled_keys and rep and field in rep:
                            await _pub(
                                suffix, key, "on" if rep[field] == "green" else "off"
                            )

            # Heartbeat (diagnostic; not an HA entity).
            hb = {
                "ts_ms": int(now * 1000),
                "connected": health.connected,
                "stats_age_s": round(now - last_stats_ok, 1) if last_stats_ok else None,
            }
            await mqtt_publish(
                mq,
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

            # Waits on the stop event rather than polling, so a signal ends
            # the cycle at once instead of at the next 0.2s tick.
            await _wait_or_stop(stop, interval)

        # Shutdown was requested; a clean stop is not a failure.
        return 0

    log("INFO", f"Connecting MQTT to {mqtt_host}:{mqtt_port}", log_level)
    delay = _RECONNECT_MIN_SECONDS
    try:
        while not stop.is_set():
            connected_at = health.last_connect_ok
            try:
                async with aiomqtt.Client(
                    hostname=mqtt_host,
                    port=mqtt_port,
                    username=mqtt_username or None,
                    password=mqtt_password or None,
                    identifier=client_id,
                    will=aiomqtt.Will(
                        topic=availability_topic, payload=b"offline", qos=1, retain=True
                    ),
                    keepalive=60,
                    timeout=_MQTT_TIMEOUT_SECONDS,
                ) as mq:
                    health.connected = True
                    health.last_connect_ok = time.time()
                    health.connect_count += 1
                    log("INFO", f"MQTT connected to {mqtt_host}:{mqtt_port}", log_level)
                    # Every session rediscovers: a broker restart drops retained
                    # config, and HA needs it back before any state lands.
                    need_discovery["v"] = True
                    try:
                        await mq.subscribe(
                            f"{discovery_prefix}/status",
                            qos=1,
                            timeout=_SUBSCRIBE_TIMEOUT_SECONDS,
                        )
                    except (ValueError, aiomqtt.MqttError) as e:
                        # A malformed prefix is a config typo; MqttError is the
                        # broker refusing, which an ACL granting publish but not
                        # subscribe will do. Neither may end the session, or we
                        # would reconnect and die here every time and never
                        # publish at all. Costs only birth-triggered rediscovery.
                        log(
                            "ERROR",
                            f"cannot subscribe to {discovery_prefix}/status: {e}; "
                            "HA restart will not trigger rediscovery",
                            log_level,
                        )
                    await mqtt_publish(
                        mq,
                        availability_topic,
                        "online",
                        qos=1,
                        retain=True,
                        log_level=log_level,
                        health=health,
                    )
                    birth = asyncio.create_task(
                        _watch_birth(mq, need_discovery, log_level), name="birth"
                    )
                    try:
                        return await publish_loop(mq)
                    finally:
                        birth.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await birth
                        # Supersede the will while the session is still up, on a
                        # short leash so an unresponsive broker cannot spend the
                        # whole stop budget here.
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                mqtt_publish(
                                    mq,
                                    availability_topic,
                                    "offline",
                                    qos=1,
                                    retain=True,
                                    log_level=log_level,
                                    health=health,
                                ),
                                timeout=_FAREWELL_TIMEOUT_SECONDS,
                            )
            except aiomqtt.MqttError as e:
                health.connected = False
                # Stamp the *start* of an outage, not each retry: restamping
                # would reset the clock the disconnect check below reads, and
                # since retries cap well inside the timeout it could never fire.
                if (
                    health.last_disconnect == 0.0
                    or health.last_connect_ok != connected_at
                ):
                    health.last_disconnect = time.time()
                if health.last_connect_ok != connected_at:
                    delay = _RECONNECT_MIN_SECONDS  # a real session; start over
                log("WARNING", f"MQTT: {e}; reconnecting in {delay}s", log_level)
                down_for = time.time() - health.last_disconnect
                if down_for > disconnect_timeout:
                    log(
                        "ERROR",
                        f"MQTT disconnected for {down_for:.0f}s "
                        f"(> {disconnect_timeout}s). Exiting for supervisor restart.",
                        log_level,
                    )
                    return EXIT_DISCONNECTED
                await _wait_or_stop(stop, delay)
                delay = min(delay * 2, _RECONNECT_MAX_SECONDS)
    except Exception as e:  # noqa: BLE001 - last-resort guard, logged + restarted
        log("ERROR", f"Main loop exception: {e}", log_level)
        return EXIT_LOOP_ERROR
    finally:
        stop.set()
        # Wind the Beast reader up so its socket closes now, rather than leaving
        # readsb a dangling client until the process exits.
        if df_counter is not None:
            await df_counter.aclose()

    return 0


def main() -> int:
    """Sync entry point: run the whole add-on on one event loop."""
    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001 - supervisor safety net
        print(f"[ERROR] Main loop exception: {e!r}", flush=True)
        return EXIT_LOOP_ERROR
