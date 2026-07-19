# Aviation Feeder — Documentation

This is the configuration and usage reference for the **Aviation Feeder**
add-on. For a high-level overview of the project see the
[repository README](https://github.com/bakerkj/hass-aviation-feeder); for the
internal architecture (image build, s6 services, ports map, MQTT publisher) see
[DEVELOPING.md](DEVELOPING.md).

Aviation Feeder layers the account-based ADS-B/UAT feeders onto the
[`docker-adsb-ultrafeeder`](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder)
base: readsb (1090), dump978 (978/UAT), tar1090, graphs1090, mlat-client, and
the community-aggregator net connectors. Everything runs in **one container**;
every feeder points at the in-container readsb (Beast on `localhost:30005`) and
dump978 (UAT on `localhost:30978`). See [How it works](#how-it-works) for a
short architecture overview.

Each option below is given as **Display name** (`yaml_key`): the display name is
what the Configuration tab shows, and the `yaml_key` is the field name if you
edit the configuration in YAML.

## Quick start

1. Install the add-on and open **Configuration**.
2. Set **Latitude** (`lat`), **Longitude** (`long`), **Altitude** (`alt`), and
   **Time zone** (`tz`) — or leave the location fields at their defaults
   (`HOMEASSISTANT_LATITUDE`, `HOMEASSISTANT_LONGITUDE`,
   `HOMEASSISTANT_ELEVATION`) to inherit Home Assistant's own location.
3. Pick a **Receiver mode** (`receiver_mode`); for a local dongle set its serial
   — see [SDR settings](#sdr-settings).
4. Turn on the aggregators you want and paste their keys/IDs — see
   [Aggregators](#aggregators).
5. Start the add-on and open the **Web UI** (sidebar) to see aircraft.

## Station identity

| Option (`yaml key`)                                      | Notes                                                                                                                                                                                                          |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Latitude / Longitude / Altitude** (`lat` `long` `alt`) | Defaults `HOMEASSISTANT_LATITUDE` / `HOMEASSISTANT_LONGITUDE` / `HOMEASSISTANT_ELEVATION` inherit HA's location (blank works too). Set a plain number to override. Altitude is in **metres** — no unit suffix. |
| **Time zone** (`tz`)                                     | IANA name, e.g. `America/Detroit`. FR24 always runs GMT.                                                                                                                                                       |
| **Site name** (`site_name`)                              | Shown on the tar1090 map.                                                                                                                                                                                      |
| **Station UUID** (`uuid`)                                | One UUID for all community aggregators and MLAT. Generate with `uuidgen`.                                                                                                                                      |

## Receiver mode (single box vs split site)

Set by **Receiver mode** (`receiver_mode`):

- **rtlsdr** (default): decode 1090 MHz from a local dongle. Add 978 MHz UAT on
  a second dongle by turning on **Enable 978 MHz UAT** (`enable_uat`).
- **uat**: decode **978 MHz UAT only** from a local dongle — for a receiver with
  just a 978 stick and no 1090. readsb runs net-only and ingests the local UAT
  stream; `enable_uat` is implied. Set the **978 MHz RTL-SDR device**
  (`dump978_rtlsdr_device`) serial/gain/ppm.
- **remote**: run net-only (no SDR). readsb ingests Beast from another Aviation
  Feeder instance set by **Remote Beast host** (`remote_beast_host`) / **port**
  (`remote_beast_port`). Use this to run a display/aggregator node separately
  from the antenna/extractor node — the extractor runs in `rtlsdr` mode and
  exposes Beast on port 30005, and the aggregator node points at it and does all
  the feeding + the map.

## SDR settings

Each RTL-SDR band has three settings: a **device serial**, a **gain**, and a
**ppm** frequency correction. 1090 MHz and 978 MHz UAT are separate bands, each
on its own dongle: 1090 drives readsb, 978 drives dump978. Run 1090 alone
(`rtlsdr` mode), 978 alone (`uat` mode), or both together (`rtlsdr` mode with
**Enable 978 MHz UAT** (`enable_uat`) on). 978 MHz UAT is used mainly in the US.

### Device serials — pinning the right dongle

readsb and dump978 select their RTL-SDR by **serial number**:

- **1090 MHz RTL-SDR device** (`readsb_rtlsdr_device`) → readsb.
- **978 MHz RTL-SDR device** (`dump978_rtlsdr_device`) → dump978.

You can leave a serial blank only in the simplest case — exactly one RTL-SDR on
the host and no other SDR software — where readsb just grabs the sole device.
Otherwise set a serial. Reasons to pin one **even with a single dongle**:

- **Stability.** With no serial, readsb takes whatever enumerates as "device 0",
  and that index can shift when you reboot, replug into another USB port, or add
  any other USB SDR — after which readsb may bind the wrong device (or none). A
  serial always resolves to the same physical stick.
- **Two dongles at once (1090 + 978 together).** When you run both bands
  (`rtlsdr` mode with 978 enabled), one stick decodes 1090 and one decodes 978
  at the same time, so readsb and dump978 must each claim the right one; index
  order isn't stable, so set both serials. (Running a single band — 1090-only or
  978-only — needs just the one dongle.)
- **Multiple SDR apps/containers on the host, or simply an explicit,
  reproducible config.**

RTL-SDRs are addressed by serial (or by that unstable index) — readsb has no
"select by `/dev` path" option, so pinning a serial is the reliable way to bind
a specific dongle.

**Find a dongle's serial** on any Linux machine with the `rtl-sdr` tools
installed:

- `lsusb` — confirms the dongle is seen (`Realtek … RTL2838 DVB-T`).
- `rtl_test` — lists each attached device with its index and serial, e.g.
  `0: Realtek, RTL2838UHIDIR, SN: 00000001`.
- `rtl_eeprom -d 0` — dumps device 0's EEPROM, including its serial.

The add-on also prints the devices readsb detects to the add-on **Log** at
startup, so you can read the serials there after plugging the dongles in. Put
the serial you find into `readsb_rtlsdr_device` (1090) or
`dump978_rtlsdr_device` (978); a dongle straight from the factory often shares
the generic serial `00000001`, in which case pick which stick is which by
plugging one in at a time.

### Gain

**1090 MHz gain** (`readsb_gain`) and **978 MHz gain** (`dump978_gain`) accept
either the literal `autogain` or a fixed value in dB.

- **`autogain`** (default): readsb periodically adjusts the tuner gain on its
  own to keep the strongest signals from overloading the receiver while still
  hearing weak ones. This is the right choice for most setups and needs no
  tuning.
- **A fixed dB value** (e.g. `49.6`): pins the tuner to one of its discrete gain
  steps and disables the automatic adjustment. Pin a value when you have a
  known-good number for your antenna/filter/LNA chain, or when autogain keeps
  hunting in a noisy RF environment. Higher is not always better — too much gain
  overloads the front end and _reduces_ message rate.

### PPM frequency correction

**1090 MHz frequency correction** (`readsb_ppm`) and the UAT equivalent
(`dump978_ppm`) apply a parts-per-million correction for the offset of the
dongle's reference oscillator.

- **`0` is fine for most modern dongles** — those with a temperature-compensated
  oscillator (TCXO), which most ADS-B-oriented sticks are.
- If yours drifts, **measure the error** with a tool such as `rtl_test -p` (it
  estimates the ppm offset over a few minutes) or `kalibrate-rtl`, then enter
  the measured value. The correction is a small integer, typically in the range
  of a few tens of ppm at most.

## Aggregators

Turn on the networks you want to feed. Two kinds:

- **Account-based** — you register with the site and paste a credential.
- **Community** — anonymous; each is identified by your **Station UUID**
  (`uuid`), with an optional per-aggregator override.

All community aggregators, plus RadarBox, also have a per-aggregator **MLAT
toggle** (`*_mlat`, on by default) — turn one off to feed that network's ADS-B
but skip its MLAT (handy when two networks share a MLAT backend, e.g. adsb.one
rides airplanes.live's, so feeding both from one receiver looks like a duplicate
and causes reconnect churn). The account feeders that do MLAT (FlightAware,
FlightRadar24, PlaneFinder, plane.watch, RadarVirtuel, sdrmap) do it as part of
the feeder with no separate toggle; OpenSky, ADSBHub, and 1090MHz UK are ADS-B
only.

| Aggregator                                                     | Enable                | Credential / identity                                                                                                                                 | MLAT toggle          |
| -------------------------------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| [FlightAware](https://flightaware.com/adsb/piaware/claim)      | `enable_piaware`      | `piaware_feeder_id` (blank → claim on the FA site)                                                                                                    | built-in             |
| [FlightRadar24](https://www.flightradar24.com/share-your-data) | `enable_fr24`         | `fr24_key` (+ `fr24_uat_key` for UAT)                                                                                                                 | built-in             |
| [PlaneFinder](https://planefinder.net/)                        | `enable_planefinder`  | `planefinder_sharecode`                                                                                                                               | built-in             |
| [OpenSky Network](https://opensky-network.org/)                | `enable_opensky`      | `opensky_username` + `opensky_serial` (serial blank first run, then set)                                                                              | —                    |
| [ADSBHub](https://www.adsbhub.org/)                            | `enable_adsbhub`      | `adsbhub_clientkey`                                                                                                                                   | —                    |
| [AirNav RadarBox](https://www.radarbox.com/)                   | `enable_radarbox`     | `radarbox_sharing_key` (blank → claim)                                                                                                                | `radarbox_mlat`      |
| [plane.watch](https://atc.plane.watch/)                        | `enable_planewatch`   | `planewatch_api_key`                                                                                                                                  | built-in             |
| [RadarVirtuel](https://www.radarvirtuel.com/)                  | `enable_radarvirtuel` | `radarvirtuel_contrib_name` + `radarvirtuel_contrib_email` (+ optional `radarvirtuel_station_uid` — see [Feeder identity](#feeder-identity-and-data)) | built-in             |
| [sdrmap](https://sdrmap.org/)                                  | `enable_sdrmap`       | `sdrmap_username` + `sdrmap_password`                                                                                                                 | built-in             |
| [1090MHz UK](https://www.1090mhz.uk/)                          | `enable_uk1090`       | `uk1090_key`                                                                                                                                          | — (ADS-B only)       |
| [adsb.lol](https://adsb.lol/)                                  | `feed_adsblol`        | `uuid` (or `adsblol_uuid`)                                                                                                                            | `adsblol_mlat`       |
| [adsb.fi](https://adsb.fi/)                                    | `feed_adsbfi`         | `uuid` (or `adsbfi_uuid`)                                                                                                                             | `adsbfi_mlat`        |
| [airplanes.live](https://airplanes.live/)                      | `feed_airplaneslive`  | `uuid` (or `airplaneslive_uuid`)                                                                                                                      | `airplaneslive_mlat` |
| [Planespotters](https://www.planespotters.net/)                | `feed_planespotters`  | `uuid` (or `planespotters_uuid`)                                                                                                                      | `planespotters_mlat` |
| [TheAirTraffic](https://theairtraffic.com/)                    | `feed_theairtraffic`  | `uuid` (or `theairtraffic_uuid`)                                                                                                                      | `theairtraffic_mlat` |
| [AVDelphi](https://www.avdelphi.com/)                          | `feed_avdelphi`       | `uuid` (or `avdelphi_uuid`)                                                                                                                           | — (no MLAT)          |
| [Fly Italy ADSB](https://flyitalyadsb.com/)                    | `feed_flyitaly`       | `uuid` (or `flyitaly_uuid`)                                                                                                                           | `flyitaly_mlat`      |
| [ADSBItalia](https://www.adsbitalia.it/)                       | `feed_adsbitalia`     | `uuid` (or `adsbitalia_uuid`)                                                                                                                         | `adsbitalia_mlat`    |
| [ADS-B Exchange](https://www.adsbexchange.com/)                | `feed_adsbexchange`   | `uuid` (or `adsbexchange_uuid`)                                                                                                                       | `adsbexchange_mlat`  |
| [adsb.one](https://adsb.one/)                                  | `feed_adsbone`        | `uuid` (or `adsbone_uuid`)                                                                                                                            | `adsbone_mlat`       |
| [HpRadar](https://hpradar.com/)                                | `feed_hpradar`        | `uuid` (or `hpradar_uuid`)                                                                                                                            | `hpradar_mlat`       |

> **ADS-B Exchange** shares your receiver statistics back to adsbexchange.com by
> default when enabled. Add `ADSBX_STATS=disabled` via **Extra env**
> (`extra_env`) to opt out.

> **ADSBItalia** auto-registers your station when `feed_adsbitalia` is on: at
> startup it detects your public IP and POSTs your station details to
> adsbitalia.it's registration API (a token is stored under `/data`). The
> registered name follows `adsbitalia_name` if set, otherwise your site name
> (`mlat_user` / `site_name`) — the same name your MLAT connector uses.

## Home Assistant sensors

Turn on **Home Assistant sensors** (`ha_sensors`) to publish Aviation Feeder
entities to HA via MQTT discovery — no `configuration.yaml` edits needed. This
needs the **Mosquitto broker** add-on: leave `mqtt_host` blank and the publisher
auto-detects it (host, port, username, and password come from the Supervisor
MQTT service), falling back to anonymous `core-mosquitto`. Set `mqtt_host` /
`mqtt_port` / `mqtt_username` / `mqtt_password` only to point at an external
broker; `mqtt_discovery_prefix` (default `homeassistant`) and `mqtt_base_topic`
(default `aviation_feeder`) control the topic namespaces.

These categories are published, toggled independently:

- **Feeder health** (`ha_feeder_health`) → the **Aviation Feeder** device:
  aircraft tracked, ADS-B / Mode-S / MLAT counts, aircraft with position,
  message rate, max range, session tracks; **Network Message Rate** and
  **Network Mode A/C Rate** (how much traffic the receiver takes in over its
  network connectors rather than the local dongle — a station-wide figure, not
  attributable to any one feeder; Mode A/C stays near zero unless a peer is
  sending it); a pair of MQTT broker-link diagnostic sensors (link uptime and
  reconnect count); and — when you run a local RTL-SDR — the dongle's receiver
  stats (gain, frequency error, signal / noise, samples dropped / lost, strong
  signals, peak signal).

  It also carries readsb's own performance, which nothing else in Home Assistant
  exposes: **readsb CPU (reader)**, **(demod)** and **(background)**, each as a
  percentage of one core. They are reported separately because they mean
  different things — `reader` is USB/SDR input pressure and is the early warning
  that samples are about to start dropping, `demod` is signal-processing load,
  `background` is housekeeping. **Bad Position Decodes** counts positions readsb
  decoded and then rejected as impossible; a flat line is healthy, a climbing
  one suggests interference or multipath.

  **SDR Strong Signals** is worth watching next to **SDR Gain** — it counts
  messages above the strong-signal threshold, so a large number means the gain
  is set too high and the front end is being overloaded.

  readsb splits its work across eleven workers and all of them are published,
  but only the three above are shown. The other eight — `aircraft.json`,
  `globe.json`, `binCraft`, `traces`, `heatmap/state`, `API workers`,
  `API update` and `remove stale` — cover its JSON writers, API threads and
  housekeeping, and each runs at roughly 0.03% of a core. They are hidden so
  eight near-zero tiles don't drown the three that carry signal; enable them if
  you are profiling something specific.

  **Bad Position Decodes** and **SDR Samples Lost** are hidden for the opposite
  reason: they read 0 on a healthy station, so they would be permanent noise.
  Enable them in Home Assistant if you are chasing a problem.

- **Planes near me** (`ha_planes_near_me`) → the **Aviation Feeder — Nearby**
  device: how many aircraft are within the **Nearby radius**
  (`ha_near_me_radius`, default 50 nmi), and the nearest aircraft (callsign,
  with distance / altitude / bearing / speed as attributes). Requires your
  station latitude/longitude to be set (inherited HA location counts).
- **Emergency squawk** (`ha_emergency_squawk`) → an **Emergency Squawk** safety
  binary sensor on the **Aviation Feeder** device: on whenever any tracked
  aircraft is squawking 7500 (hijack), 7600 (radio failure) or 7700 (general
  emergency), with the offending aircraft (hex, callsign, code, altitude) as
  attributes. Position is not required, so it catches an emergency anywhere in
  your coverage — a natural trigger for a notification automation.
- **Unique aircraft today** (`ha_unique_today`) → a **Unique Aircraft Today**
  counter on the **Aviation Feeder** device: how many distinct aircraft you have
  seen since local midnight. It resets each day (and on an add-on restart, which
  Home Assistant treats as a normal counter reset).
- **Message types** (`ha_message_types`) → an **Aviation Feeder — Message
  Types** device breaking your 1090 MHz traffic down by **Downlink Format**, the
  field at the start of every Mode S message that says what kind of message it
  is. All values are messages/second:

  | Sensor                                               | What it is                                                |
  | ---------------------------------------------------- | --------------------------------------------------------- |
  | **ADS-B (DF17)**                                     | aircraft broadcasting position, velocity and callsign     |
  | **All-Call Reply (DF11)**                            | replies announcing an aircraft's ICAO address             |
  | **TCAS Short / Long (DF0, DF16)**                    | aircraft interrogating each other for collision avoidance |
  | **Altitude Reply (DF4)**, **Comm-B Altitude (DF20)** | replies to a ground radar asking for altitude             |
  | **Identity Reply (DF5)**, **Comm-B Identity (DF21)** | replies to a ground radar asking for the squawk           |
  | **TIS-B / ADS-R (DF18)**                             | ground services rebroadcasting traffic                    |

  The mix tells you about your RF environment, not just your antenna: DF17 and
  DF11 are aircraft transmitting on their own, while DF4/5/20/21 only exist
  because a ground radar interrogated something nearby — a high count means you
  are near an interrogating radar.

  readsb does not report this itself, so the add-on counts it directly from
  readsb's Beast output on a background thread. **DF5**, **DF21** and **DF18**
  ship hidden, since they are typically a fraction of a message per second.

- **Per-feeder status** (`ha_feeder_status`) → **one device per enabled feeder**
  (each grouped under the main Aviation Feeder device). Every feeder device has
  a **Connection** sensor showing whether it is actually feeding, plus an
  **Uptime** sensor. Where the data is available a feeder also gets a **Send /
  Receive Rate** (bytes/sec — or a **Message Rate** for FlightRadar24, which
  feeds over UDP with no byte count), **MLAT peers / MLAT sync** for the feeders
  whose MLAT server reports it, and **MLAT Positions / Aircraft Used** (the
  client's own resolve rate
  - aircraft it contributes). RadarBox and sdrmap do not send sync statistics,
    so for those two the positions/aircraft pair is their only MLAT signal. The
    cumulative Data Sent/Received and Messages counters are also published but
    disabled by default (enable them in HA if you want totals). FlightAware
    additionally gets **MLAT** and **Radio** health binary sensors from its own
    status; the full self-report also rides along as attributes on the
    Connection sensor.

  Feeders whose client reports the aggregator's _own_ view of your station also
  get **Aircraft Tracked**, **Aircraft ADS-B** and **Aircraft non-ADS-B**
  (FlightRadar24 today). The main device reports what your receiver decoded;
  each portal reports what _its_ tracker made of that same feed.

  Expect the portal's **total** to sit close to the main device's — every feeder
  reads the same in-container readsb, so they are watching the same aircraft.
  What **is** meant to differ is the ADS-B/non-ADS-B **split**, because each
  portal classifies for itself. A simultaneous sample: FlightRadar24 reported 59
  aircraft as 41 ADS-B + 18 non-ADS-B, while readsb reported 58 as 52 ADS-B + 6
  Mode-S. Same aircraft, different classification — that split is the
  informative part.

  So do not read **Aircraft non-ADS-B** as an equivalent of the main device's
  **Aircraft MLAT**: they are not measuring the same thing, and FlightRadar24
  does not document how it classifies. Small differences in the totals (a few
  aircraft, from tracker timing and timeouts) are normal and not a fault.

  ADS-B Exchange gets the same three sensors, counted from the stats its own
  feeder writes. PlaneFinder instead reports per-second decode rates from its
  client: **Message Rate**, **Receiver Data Rate**, and **Mode A/C Rate**
  (disabled by default — it reads 0 unless Mode A/C is being decoded, and some
  PlaneFinder client versions report a nonsense value here, which the add-on
  discards rather than publishes).

  Only feeders whose client actually reports something get these. OpenSky,
  ADSBHub, plane.watch, RadarVirtuel, sdrmap and 1090MHz UK run clients that
  expose no status endpoint or file, and the community aggregators (adsb.lol,
  adsb.fi, airplanes.live, …) are network connectors with no client process at
  all — so for those there is no per-feeder view to publish, only the Connection
  and Uptime sensors every feeder gets.

  "Feeding" is measured: community aggregators (adsb.lol, adsb.fi, ADS-B
  Exchange, …) report readsb's own connection state; feeders that hold an open
  connection are checked for a live socket to the aggregator (so a feeder that
  is running but silently not sending shows as off); and the couple of feeders
  that report their own status expose it directly.

If you decode from a local RTL-SDR (receiver mode `rtlsdr` or `uat`), the main
**Aviation Feeder** device also gains SDR receiver stats: dongle gain, frequency
error (ppm), signal / noise levels, and dropped-sample count. In `remote` mode
there is no local dongle, so these are omitted.

If you decode **978 MHz UAT** locally (receiver mode `uat`, or `rtlsdr` with
**Enable 978 MHz UAT** on), a separate **Aviation Feeder — UAT** device appears
with the 978-band equivalents of the 1090 stats: **UAT Aircraft**, **UAT Message
Rate**, **UAT Max Range**, and **UAT Signal Level** (from dump978's own
decoder). The device is absent when 978 is not decoded locally.

Every device carries an availability (online/offline) state via MQTT Last-Will,
and its entities are registry-managed (grouped into devices, restored across HA
restarts). Publisher settings: **MQTT publish interval**
(`mqtt_interval_seconds`, default 30 s) and **MQTT publisher log level**
(`mqtt_log_level`).

## Map & advanced escape hatches

- **Update tar1090 on boot** (`update_tar1090`): off by default — the add-on
  ships a fixed tar1090 map + aircraft database (updated when the add-on
  updates), avoiding a GitHub download on every start. Turn on to fetch the
  latest at each boot instead.
- **HeyWhatsThat panorama** (`heywhatsthat_panorama_id`, `heywhatsthat_alts`):
  optional theoretical-range ring on the map. Create a panorama at
  <https://www.heywhatsthat.com>, then paste the `?view=CODE` id into
  `heywhatsthat_panorama_id`; `heywhatsthat_alts` sets the ring altitudes (blank
  = tar1090's default).
- **Prefer IPv4 (skip IPv6 DNS)** (`prefer_ipv4`): on by default — the resolver
  skips AAAA lookups so feeders don't stall on a container IPv6 address that has
  no working route. Turn off only if your container has real IPv6 connectivity.
- **Map history retention** (`max_globe_history_days`, default 7): how many days
  of globe_history / heatmap to keep on `/data`.
- **Extra ULTRAFEEDER_CONFIG** (`ultrafeeder_config`): extra readsb net
  connectors, appended verbatim (semicolon-separated), for aggregators not
  listed above.
- **Extra environment variables** (`extra_env`): `KEY=value` lines injected into
  the container, for any ultrafeeder/feeder setting not surfaced as an option.

## Networking

- The web UI is available via ingress (sidebar). A direct host port (default
  8504 → container 80) is set under the add-on **Network** tab.
- Raw data ports are unpublished by default: SBS `30003`, Beast `30005`, raw UAT
  `30978`. Publish any you need under **Network**.
- The PlaneFinder client's status/map UI is published on `30053` by default
  (`http://<host>:30053`); it serves nothing until PlaneFinder is enabled.

## How it works

Aviation Feeder is **one Docker image** running many services in a single
container under [s6-overlay](https://github.com/just-containers/s6-overlay): the
`docker-adsb-ultrafeeder` base (readsb, tar1090, graphs1090, mlat-client,
collectd) with the account-based feeder binaries layered on top. Every feeder
points at the in-container readsb (Beast on `localhost:30005`) and, for UAT,
dump978 (`localhost:30978`).

The heart of the add-on is a config bridge, `00-haos-options`: Home Assistant
stores your options in `/data/options.json`, but readsb and the feeders read
**environment variables**, so the bridge translates each option into the env
vars (and the `ULTRAFEEDER_CONFIG` connector list) that every downstream service
expects, before those services start.

For the full architecture — the multi-stage image build, the s6 service model
and feeder gating, the complete ports map, and the MQTT/HA-sensor publisher
internals — see [DEVELOPING.md](DEVELOPING.md).

## Troubleshooting

- Check the add-on **Log**. Each feeder logs under its own tag (`[piaware]`,
  `[fr24feed]`, `[rbfeeder]`, …). A disabled or unconfigured feeder logs
  "disabled … idling" and does nothing — that is expected.
- "no ADS-B data on port 30005/30978" means readsb/dump978 have no aircraft yet
  (no signal or no dongle) — not a feeder error.
- `state directory didn't exist, created it` from readsb on first start is
  normal. Flight history/heatmap (`/var/globe_history`), the graphs1090 database
  (`/var/lib/collectd`), and the piaware identity (`/var/cache/piaware`) are
  kept on the persistent `/data` volume, so they survive rebuilds and updates
  and rebuild over time on a fresh install. The live readsb/collectd write dirs
  run on a RAM tmpfs to spare the SD card; only an hourly graphs snapshot is
  written back to `/data`.

## Feeder identity and `/data`

Two feeders generate an identity the first time they run and keep it on `/data`.
If `/data` is ever lost — a wiped volume, or a reinstall (Home Assistant gives a
new install a fresh `/data`) — they silently register again as a **brand-new
site**, with no error to notice. Pin them in options instead, and `/data` then
holds no identity at all: only history, which simply rebuilds.

| feeder       | option                     | generated value on `/data` |
| ------------ | -------------------------- | -------------------------- |
| piaware      | `piaware_feeder_id`        | `/data/piaware/feeder_id`  |
| RadarVirtuel | `radarvirtuel_station_uid` | `/data/station_uid.txt`    |

RadarVirtuel keys the station off the **UID**: the feeder re-registers on every
start, and its entrypoint takes `RV_STATION_UID` **above** the persisted file —
so a pinned UID brings the same station back
(`Registration: EXISTING — station <id>`) even on a fresh volume. While the
option is empty the add-on logs the UID on every start, so you do not have to
know to go looking for it.

The UID must be at least 8 characters: the feeder silently ignores anything
shorter and generates a new identity instead. The add-on warns if you set one
that short.
