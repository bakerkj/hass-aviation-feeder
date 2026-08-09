# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""MQTT helpers: health tracking, an async publish wrapper, and Home Assistant
discovery payloads.

Publishes are awaitable against an ``aiomqtt.Client``: the add-on runs on one
event loop, so a synchronous send would stall the loop that drains the Beast
stream. Session lifetime belongs to app.py -- this publishes into whatever
session it is handed. Everything below ``_device`` is pure payload construction
and needs no broker to test.
"""

import time
from typing import Any, Protocol

from .metadata import (
    BROKER_METRICS,
    DEVICE_ID,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DF_DEVICE_ID,
    DF_DEVICE_NAME,
    DF_METRICS,
    EMERGENCY_SQUAWK_KEY,
    FEEDERS_DEVICE_ID,
    METRICS,
    NEARBY_DEVICE_ID,
    NEARBY_DEVICE_NAME,
    NEARBY_METRICS,
    NEARBY_STATE_KEY,
    PERFORMANCE_METRICS,
    REMOTE_METRICS,
    REPORT_BINARY_SENSORS,
    SDR_METRICS,
    UAT_DEVICE_ID,
    UAT_DEVICE_NAME,
    UAT_METRICS,
    UNIQUE_METRICS,
    Metric,
)


class MqttHealth:
    def __init__(self) -> None:
        self.connected: bool = False
        self.last_connect_ok: float = 0.0
        self.last_disconnect: float = 0.0
        self.last_state_publish_ok: float = 0.0
        self.connect_count: int = 0  # successful connects; reconnects = count - 1


class Client(Protocol):
    """Anything with an awaitable ``publish``.

    A Protocol so the discovery tests can record what would go on the wire
    without standing up a broker.
    """

    async def publish(
        self,
        topic: str,
        payload: bytes | str = "",
        qos: int = 0,
        retain: bool = False,
    ) -> Any: ...


async def mqtt_publish(
    client: Client,
    topic: str,
    payload: str,
    *,
    qos: int,
    retain: bool,
    log_level: str,
    health: MqttHealth,
    mark_state: bool = False,
) -> None:
    """Publish one message, stamping the health clock on success.

    Broker faults are deliberately left to propagate: aiomqtt reports them as
    ``MqttError``, and the session loop above uses that to tear down and
    reconnect. Swallowing it here would leave the publisher cycling against a
    dead session with every sensor silently frozen.

    The stamp sits after the await, so a publish that never landed cannot
    refresh the clock the stall watchdog reads.
    """
    await client.publish(topic, payload, qos=qos, retain=retain)
    if mark_state:
        health.last_state_publish_ok = time.time()
    del log_level  # kept for call-site symmetry; failures now raise instead


def _device(device_id: str, name: str) -> dict[str, Any]:
    return {
        "identifiers": [device_id],
        "name": name,
        "manufacturer": DEVICE_MANUFACTURER,
        "model": DEVICE_MODEL,
    }


def _feeder_device(key: str, name: str, via_parent: bool = True) -> dict[str, Any]:
    """Each feeder is its own HA device (so its sensors group together), nested
    under the main Aviation Feeder device via via_device. via_parent is False
    when the main device isn't registered (ha_feeder_health off) so we don't
    emit a dangling via_device reference."""
    dev: dict[str, Any] = {
        "identifiers": [f"{FEEDERS_DEVICE_ID}_{key}"],
        "name": f"{DEVICE_NAME} — {name}",
        "manufacturer": DEVICE_MANUFACTURER,
        "model": DEVICE_MODEL,
    }
    if via_parent:
        dev["via_device"] = DEVICE_ID
    return dev


def _sensor_payload(
    *,
    name: str,
    unique_id: str,
    state_topic: str,
    icon: str,
    device: dict[str, Any],
    state_class: str,
    precision: int,
    availability_topic: str,
    expire_after_s: int,
    unit: str | None = None,
    device_class: str | None = None,
    diagnostic: bool = False,
    has_entity_name: bool = False,
    enabled_by_default: bool = True,
) -> dict[str, Any]:
    """Shared MQTT-discovery sensor payload for both the main-device `Metric`
    sensors and the per-feeder `FeederMetric` sensors — they differ only in
    has_entity_name / always-diagnostic / enabled_by_default."""
    payload: dict[str, Any] = {
        "name": name,
        "unique_id": unique_id,
        "state_topic": state_topic,
        "icon": icon,
        "device": device,
        "state_class": state_class,
        "suggested_display_precision": precision,
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "expire_after": expire_after_s,
    }
    if has_entity_name:
        payload["has_entity_name"] = True
    if diagnostic:
        payload["entity_category"] = "diagnostic"
    if unit is not None:
        payload["unit_of_measurement"] = unit
    if device_class is not None:
        payload["device_class"] = device_class
    if not enabled_by_default:
        payload["enabled_by_default"] = False
    return payload


def _metric_config(
    m: Metric,
    device: dict[str, Any],
    device_id: str,
    state_topic: str,
    availability_topic: str,
    expire_after_s: int,
    *,
    diagnostic: bool,
) -> dict[str, Any]:
    return _sensor_payload(
        name=m.name,
        unique_id=f"{device_id}_{m.key}",
        state_topic=state_topic,
        icon=m.icon,
        device=device,
        state_class=m.state_class,
        precision=m.precision,
        availability_topic=availability_topic,
        expire_after_s=expire_after_s,
        unit=m.unit,
        device_class=m.device_class,
        diagnostic=diagnostic,
        enabled_by_default=m.enabled_default,
    )


def build_discovery_payloads(
    discovery_prefix: str,
    base_topic: str,
    availability_topic: str,
    expire_after_s: int,
) -> dict[str, dict[str, Any]]:
    """Feeder-health discovery: one retained config per metric (diagnostic).

    REMOTE_METRICS ride along here: they come from the same stats.json and
    belong to the same device, they just describe network ingest rather than
    the local SDR."""
    out: dict[str, dict[str, Any]] = {}
    device = _device(DEVICE_ID, DEVICE_NAME)
    for m in (*METRICS, *REMOTE_METRICS, *PERFORMANCE_METRICS):
        cfg = _metric_config(
            m,
            device,
            DEVICE_ID,
            f"{base_topic}/{m.key}/state",
            availability_topic,
            expire_after_s,
            diagnostic=True,
        )
        out[f"{discovery_prefix}/sensor/{DEVICE_ID}/{m.key}/config"] = cfg
    return out


def build_broker_discovery(
    discovery_prefix: str,
    base_topic: str,
    availability_topic: str,
    expire_after_s: int,
) -> dict[str, dict[str, Any]]:
    """MQTT broker-link diagnostic sensors on the main device (uptime,
    reconnects) — surfaces the connection's own health, not just the LWT."""
    out: dict[str, dict[str, Any]] = {}
    device = _device(DEVICE_ID, DEVICE_NAME)
    for m in BROKER_METRICS:
        cfg = _metric_config(
            m,
            device,
            DEVICE_ID,
            f"{base_topic}/{m.key}/state",
            availability_topic,
            expire_after_s,
            diagnostic=True,
        )
        out[f"{discovery_prefix}/sensor/{DEVICE_ID}/{m.key}/config"] = cfg
    return out


def build_unique_discovery(
    discovery_prefix: str,
    base_topic: str,
    availability_topic: str,
    expire_after_s: int,
) -> dict[str, dict[str, Any]]:
    """ "Unique aircraft today" sensor on the main device — a primary (non-
    diagnostic) daily count of distinct aircraft seen since local midnight."""
    out: dict[str, dict[str, Any]] = {}
    device = _device(DEVICE_ID, DEVICE_NAME)
    for m in UNIQUE_METRICS:
        cfg = _metric_config(
            m,
            device,
            DEVICE_ID,
            f"{base_topic}/{m.key}/state",
            availability_topic,
            expire_after_s,
            diagnostic=False,
        )
        out[f"{discovery_prefix}/sensor/{DEVICE_ID}/{m.key}/config"] = cfg
    return out


def build_emergency_discovery(
    discovery_prefix: str,
    base_topic: str,
    availability_topic: str,
    expire_after_s: int,
) -> dict[str, dict[str, Any]]:
    """Emergency-squawk safety binary_sensor on the main device: ON when any
    tracked aircraft squawks 7500/7600/7700, with the offenders as JSON
    attributes. A primary entity (not diagnostic) so the alert is prominent and
    easy to automate on."""
    device = _device(DEVICE_ID, DEVICE_NAME)
    key = EMERGENCY_SQUAWK_KEY
    return {
        f"{discovery_prefix}/binary_sensor/{DEVICE_ID}/{key}/config": {
            "name": "Emergency Squawk",
            "unique_id": f"{DEVICE_ID}_{key}",
            "state_topic": f"{base_topic}/{key}/state",
            "json_attributes_topic": f"{base_topic}/{key}/attributes",
            "payload_on": "on",
            "payload_off": "off",
            "device_class": "safety",
            "icon": "mdi:alert",
            "device": device,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "expire_after": expire_after_s,
        }
    }


def build_sdr_discovery(
    discovery_prefix: str,
    sdr_topic: str,
    availability_topic: str,
    expire_after_s: int,
) -> dict[str, dict[str, Any]]:
    """Local-SDR health sensors (gain, ppm, signal/noise, samples dropped) — on
    the main Aviation Feeder device (they're receiver stats, not a separate
    thing). Only published when receiver_mode is a local SDR."""
    out: dict[str, dict[str, Any]] = {}
    device = _device(DEVICE_ID, DEVICE_NAME)
    for m in SDR_METRICS:
        cfg = _metric_config(
            m,
            device,
            DEVICE_ID,
            f"{sdr_topic}/{m.key}/state",
            availability_topic,
            expire_after_s,
            diagnostic=True,
        )
        out[f"{discovery_prefix}/sensor/{DEVICE_ID}/{m.key}/config"] = cfg
    return out


def build_uat_discovery(
    discovery_prefix: str,
    uat_topic: str,
    availability_topic: str,
    expire_after_s: int,
) -> dict[str, dict[str, Any]]:
    """978/UAT receiver stats (aircraft, message rate, max range, signal) on their
    OWN "Aviation Feeder — UAT" device, so they group together and appear only
    when 978 is decoded locally. Fed from dump978's stats.json (uat-stats svc)."""
    out: dict[str, dict[str, Any]] = {}
    device = _device(UAT_DEVICE_ID, UAT_DEVICE_NAME)
    for m in UAT_METRICS:
        cfg = _metric_config(
            m,
            device,
            UAT_DEVICE_ID,
            f"{uat_topic}/{m.key}/state",
            availability_topic,
            expire_after_s,
            diagnostic=False,
        )
        out[f"{discovery_prefix}/sensor/{UAT_DEVICE_ID}/{m.key}/config"] = cfg
    return out


def build_df_discovery(
    discovery_prefix: str,
    df_topic: str,
    availability_topic: str,
    expire_after_s: int,
) -> dict[str, dict[str, Any]]:
    """Mode S downlink-format message rates on their OWN "Message Types" device.
    Counted off readsb's Beast stream (see beast.py) because readsb reports no
    per-DF breakdown itself. Not diagnostic -- the traffic mix is a dashboard
    number, not a fault indicator."""
    out: dict[str, dict[str, Any]] = {}
    device = _device(DF_DEVICE_ID, DF_DEVICE_NAME)
    for m in DF_METRICS:
        cfg = _metric_config(
            m,
            device,
            DF_DEVICE_ID,
            f"{df_topic}/{m.key}/state",
            availability_topic,
            expire_after_s,
            diagnostic=False,
        )
        out[f"{discovery_prefix}/sensor/{DF_DEVICE_ID}/{m.key}/config"] = cfg
    return out


def build_nearby_discovery(
    discovery_prefix: str,
    nearby_topic: str,
    availability_topic: str,
    expire_after_s: int,
) -> dict[str, dict[str, Any]]:
    """Planes-near-me discovery: numeric sensors + the nearest-aircraft text
    entity (state + JSON attributes), all under the Nearby device."""
    out: dict[str, dict[str, Any]] = {}
    device = _device(NEARBY_DEVICE_ID, NEARBY_DEVICE_NAME)

    for m in NEARBY_METRICS:
        cfg = _metric_config(
            m,
            device,
            NEARBY_DEVICE_ID,
            f"{nearby_topic}/{m.key}/state",
            availability_topic,
            expire_after_s,
            diagnostic=False,
        )
        out[f"{discovery_prefix}/sensor/{NEARBY_DEVICE_ID}/{m.key}/config"] = cfg

    # Nearest aircraft: state is the callsign; extra fields (distance, altitude,
    # bearing, speed, hex) ride along as JSON attributes.
    out[f"{discovery_prefix}/sensor/{NEARBY_DEVICE_ID}/{NEARBY_STATE_KEY}/config"] = {
        "name": "Nearest Aircraft",
        "unique_id": f"{NEARBY_DEVICE_ID}_{NEARBY_STATE_KEY}",
        "state_topic": f"{nearby_topic}/{NEARBY_STATE_KEY}/state",
        "json_attributes_topic": f"{nearby_topic}/{NEARBY_STATE_KEY}/attributes",
        "icon": "mdi:airplane-marker",
        "device": device,
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "expire_after": expire_after_s,
    }
    return out


def build_feeders_discovery(
    discovery_prefix: str,
    feeders_topic: str,
    availability_topic: str,
    expire_after_s: int,
    feeders: list[tuple[str, str, bool]],
    via_parent: bool = True,
) -> dict[str, dict[str, Any]]:
    """Per-feeder connectivity binary_sensor (one per enabled feeder), each on its
    OWN feeder device. `feeders` is compute_feeder_status()'s output."""
    out: dict[str, dict[str, Any]] = {}
    for key, name, _connected in feeders:
        out[f"{discovery_prefix}/binary_sensor/{FEEDERS_DEVICE_ID}/{key}/config"] = {
            # has_entity_name -> HA shows "<feeder device> Connection".
            "name": "Connection",
            "has_entity_name": True,
            "unique_id": f"{FEEDERS_DEVICE_ID}_{key}",
            "state_topic": f"{feeders_topic}/{key}/state",
            # A few feeders self-report semantic health (piaware MLAT/radio, fr24
            # feed status); it rides along as attributes on the connectivity
            # sensor. Absent for feeders with no self-report.
            "json_attributes_topic": f"{feeders_topic}/{key}/attributes",
            "payload_on": "on",
            "payload_off": "off",
            "device_class": "connectivity",
            "device": _feeder_device(key, name, via_parent),
            "entity_category": "diagnostic",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "expire_after": expire_after_s,
        }
    return out


def build_report_binary_discovery(
    discovery_prefix: str,
    feeders_topic: str,
    availability_topic: str,
    expire_after_s: int,
    feeders,
    via_parent: bool = True,
) -> dict[str, dict[str, Any]]:
    """Feeder-specific health binary_sensors from an app self-report (piaware
    MLAT/radio), each on its feeder device. `feeders` items are (key, name)."""
    names = {e[0]: e[1] for e in feeders}
    out: dict[str, dict[str, Any]] = {}
    for key, suffix, ename, _field, icon in REPORT_BINARY_SENSORS:
        if key not in names:
            continue
        out[
            f"{discovery_prefix}/binary_sensor/{FEEDERS_DEVICE_ID}/{key}_{suffix}/config"
        ] = {
            "name": ename,
            "has_entity_name": True,
            "unique_id": f"{FEEDERS_DEVICE_ID}_{key}_{suffix}",
            "state_topic": f"{feeders_topic}/{key}/{suffix}/state",
            "payload_on": "on",
            "payload_off": "off",
            "icon": icon,
            "device": _feeder_device(key, names[key], via_parent),
            "entity_category": "diagnostic",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "expire_after": expire_after_s,
        }
    return out


def build_feeder_metrics_discovery(
    discovery_prefix: str,
    feeders_topic: str,
    availability_topic: str,
    expire_after_s: int,
    feeders,
    metrics,
    via_parent: bool = True,
) -> dict[str, dict[str, Any]]:
    """Per-feeder numeric sensors — one per (feeder, metric) from the given
    FeederMetric list, each on its OWN feeder device. `feeders` items may be
    (key, name) or (key, name, connected)."""
    out: dict[str, dict[str, Any]] = {}
    for entry in feeders:
        key, name = entry[0], entry[1]
        device = _feeder_device(key, name, via_parent)
        for m in metrics:
            # has_entity_name -> HA shows "<feeder device> <metric>".
            payload = _sensor_payload(
                name=m.name_suffix,
                unique_id=f"{FEEDERS_DEVICE_ID}_{key}_{m.suffix}",
                state_topic=f"{feeders_topic}/{key}/{m.suffix}/state",
                icon=m.icon,
                device=device,
                state_class=m.state_class,
                precision=m.precision,
                availability_topic=availability_topic,
                expire_after_s=expire_after_s,
                unit=m.unit,
                device_class=m.device_class,
                diagnostic=True,
                has_entity_name=True,
                enabled_by_default=m.enabled_default,
            )
            out[
                f"{discovery_prefix}/sensor/{FEEDERS_DEVICE_ID}/{key}_{m.suffix}/config"
            ] = payload
    return out
