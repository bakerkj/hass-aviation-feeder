# Aviation Feeder — Developer / Architecture Guide

This is the **maintainer-facing** companion to the user-facing
[`DOCS.md`](DOCS.md). It explains how the container is plumbed: how raw 1090/978
gets into readsb, how each feeder consumes that data and how each feeder's
binary is built into the image, the s6 service model and the shared gating
helper, the full ports map, and the MQTT/HA-sensor publisher. Every port, option
name, host, env var, and Dockerfile detail below is grounded in the actual
source (paths are relative to `aviation_feeder/` unless noted).

For the end-user option reference (SDR serials, gain/ppm, per-aggregator keys,
etc.) read `DOCS.md` instead — this document deliberately does not repeat it.

## 1. Big-picture data flow

Aviation Feeder is **one image** running **many s6 services in one container**,
built `FROM`
[`docker-adsb-ultrafeeder`](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder).
The base already provides readsb, dump978, tar1090, graphs1090, the community
`mlat-client`, `mlathub`, and collectd; the add-on layers the account-based
client-binary feeders and a config bridge on top.

### The build pattern (shared)

The `Dockerfile` is a **multi-stage** build. The final image is
`FROM ${BUILD_FROM}`, where `BUILD_FROM` defaults to the pinned ultrafeeder base
`ghcr.io/sdr-enthusiasts/docker-adsb-ultrafeeder:latest@sha256:38b6e1e355c0…bc02832`
(the digest changes on every Renovate bump — read it from the top of the
Dockerfile, don't trust a copy here). Each account-based feeder is its own
`FROM …@sha256:… AS <stage>` pulling a prebuilt upstream image, and the final
image `COPY --from=<stage>`s just the binaries + s6 service files it needs
(never the upstream's competing decoder). All images are pinned `tag@digest` for
reproducibility; **Renovate**'s dockerfile manager (grouped "sdr-enthusiasts
base images") tracks the bumps. Each feeder's own section below documents its
stage, its `COPY --from` set, and any apt packages / staged files it needs. Two
recurring build conventions:

- **`legacy-cont-init` dep.** Copied-in config oneshots (`01-*`, `02-rbfeeder`,
  dump978) get a `dependencies.d/legacy-cont-init` marker so they run **after**
  our `00-haos-options` env bridge (otherwise they'd read an empty environment).
- **Gated wrappers.** Upstream config oneshots that hard-exit on missing
  credentials are copied to a `-real` sidecar and shadowed by a gated wrapper in
  `rootfs/` (see §2).

Runtime packages the base lacks are installed in **one apt layer**, each
version-pinned with a `# renovate:` annotation; the per-feeder sections say
which package belongs to which feeder.

### How the data moves

Everything hangs off **readsb**, the one decoder and one data hub:

- In `rtlsdr` mode readsb decodes 1090 MHz from the local RTL-SDR, and `dump978`
  decodes 978 MHz UAT from a second stick. dump978's UAT stream is fed **into**
  readsb as a net connector (`adsb,localhost,30978,uat_in`) so readsb holds the
  merged 1090+978 picture.
- In `uat` mode readsb also runs net-only, but the local `dump978` is its
  **sole** input (`adsb,localhost,30978,uat_in`) — for a receiver with only a 978
  stick and no 1090. `00-haos-options` forces `ENABLE_UAT=true` in this mode.
- In `remote` mode readsb runs net-only (no SDR): it ingests Beast from a remote
  extractor via `adsb,<host>,<port>,beast_in`, and dump978 idles.

readsb then exposes that picture several ways; every consumer reads one:

- **Beast** on `localhost:30005` — the primary feed source for almost every
  feeder and for the community `mlat-client`.
- **SBS/BaseStation** on `localhost:30003` — consumed by ADSBHub (via socat).
- **`aircraft.json`** at `/run/readsb/aircraft.json` — polled by tar1090, by the
  RadarVirtuel and sdrmap feeders, and by the MQTT publisher's planes-near-me.
- **`stats.json`** at `/run/readsb/stats.json` — the MQTT publisher's
  feeder-health source (`--write-json`).
- **`stats.prom`** at `/run/readsb/stats.prom` — Prometheus text export; the
  MQTT publisher reads `readsb_net_connector_status` from here to know which
  community aggregator connectors are up.

`/run/{readsb,collectd,mlat-client,piaware,adsbexchange-stats,graphs1090}` are
redirected onto a RAM tmpfs under `/tmp` by `00-tmpfs-readsb` / `link-tmpfs-dir`
so their high-frequency writes (readsb's ~1 Hz JSON, each mlat-client's
`--stats-json`, piaware's status.json, the ADSBx upload payload, graphs1090's
PNGs) never hit the SD/eMMC. `/run` itself is the overlay, not tmpfs — only
`/tmp` is (config.json `tmpfs: true`), so each dir is symlinked individually.

**MLAT** has two independent paths:

- **Community aggregators** use the base's `mlat-client` + `mlathub`: each
  `mlat,host,port` entry in `ULTRAFEEDER_CONFIG` spawns a base mlat-client that
  reads Beast from readsb (`localhost:30005`) and returns results into `mlathub`
  (`beast,connect,localhost:31004`).
- **Client-binary feeders** that do MLAT (piaware, rbfeeder, plane.watch,
  RadarVirtuel, sdrmap) each run their **own** mlat-client on a **distinct**
  results-listen port so their output tags don't collide (see the ports map).

```
   rtlsdr mode:
     1090 MHz RTL-SDR ─► readsb ──────────────────────────┐
     978  MHz RTL-SDR ─► dump978 ─► localhost:30978 (uat_in)┘ (fed into readsb)

   uat mode:
     978  MHz RTL-SDR ─► dump978 ─► localhost:30978 (uat_in) ─► readsb (net-only)

   remote mode:
     remote extractor Beast ─► readsb (adsb,<host>,<port>,beast_in)

                        ┌──────────────── readsb ────────────────┐
                        │ Beast   localhost:30005                 │
                        │ SBS     localhost:30003                 │
                        │ /run/readsb/{aircraft,stats}.json       │
                        │ /run/readsb/stats.prom                  │
                        └───┬───────┬──────────┬─────────────┬────┘
                            │       │          │             │
     tar1090 / graphs1090 ◄─┘       │          │             │  (reads JSON)
                                    │          │             │
   client-binary feeders ──────────┘          │             │
   (piaware, fr24feed, fr24uat, pfclient,     │             │
    opensky, rbfeeder, pw-feeder,             │             │
    radarvirtuel, sdrmap)  ◄── Beast :30005 / SBS :30003 / aircraft.json
                                               │             │
   community aggregators ◄── readsb net-connectors (ULTRAFEEDER_CONFIG)
   (adsb.lol, adsb.fi, ADSB Exchange, …)       │             │
                                               │             │
   base mlat-client(s) ─► mlathub (:31004) ◄───┘             │
   per-feeder mlat-client(s) ─► results beast,listen:3010x ──┘
```

### The config bridge (`rootfs/etc/cont-init.d/00-haos-options`)

Home Assistant writes the add-on options to `/data/options.json`. Neither
ultrafeeder nor the account-based feeders read that file — they read **environment
variables**. `00-haos-options` is the translator: a `cont-init.d` oneshot that
runs once at boot (before any longrun) and writes each option into the s6
container environment (`/run/s6/container_environment/<VAR>`), inherited by
every service started afterwards via `with-contenv`.

Highlights (all verified in the script):

- Resolves `lat`/`long`/`alt` into `LAT`/`LONG`/`ALT`. Blank **or** the
  `HOMEASSISTANT_*` sentinel defaults both mean "inherit HA's location", fetched
  from the Supervisor core API
  (`${SUPERVISOR_CORE_API:-http://supervisor/core/api}/config`, needs
  `SUPERVISOR_TOKEN`). `ALT` is normalized to **bare metres** — a trailing `m`
  silently breaks rbfeeder's MLAT and makes openskyd log "Garbage after number".
- Sets `BEASTHOST=localhost` / `BEASTPORT=30005` **globally** so the account-based
  feeders can reach readsb. readsb itself must **not** consume these (it would
  net-connect to its own Beast output — a CPU-pegging feedback loop), so the
  `readsb/run` override `unset`s them before launching readsb.
- Sets each feeder's enable flag + credential env var, plus SBS wiring for
  ADSBHub (`SBSHOST`/`SBSPORT=localhost:30003`) and UAT wiring
  (`UATHOST`/`UATPORT=localhost:30978`, `UAT_RECEIVER_*` for piaware).
- **Compiles `ULTRAFEEDER_CONFIG`**: the `uat_in` (or remote `beast_in`) feed
  source plus one `beast_reduce_plus_out` (and, where offered, `mlat`) connector
  per enabled community aggregator, each tagged with the station `UUID` or its
  per-aggregator override.
- Applies any `extra_env` `KEY=value` lines last.

### Local build + test loop

```sh
# Plain image build (BUILD_VERSION sets ADDON_VERSION / the image label):
docker build --build-arg BUILD_VERSION=dev -t aviation_feeder:dev aviation_feeder
```

`tests/e2e/run.sh` is the fastest way to validate the s6 / config-bridge /
gating / decode wiring without a real SDR: it builds the image
(`docker build --build-arg BUILD_VERSION=e2e -t aviation_feeder:e2e …`), injects
a canonical DF17 frame and asserts readsb decodes it, checks the compiled
container environment and every aggregator connector, that disabled feeders idle
and enabled ones start, the rbfeeder `.ini` fixes, the readsb self-loop guard,
the tmpfs redirect, and (against a throwaway Mosquitto + a mocked HA core API)
the MQTT publisher's discovery/state/availability.

```sh
tests/e2e/run.sh                 # build + run the full suite
SKIP_BUILD=1 tests/e2e/run.sh    # reuse an existing image
POLL_TIMEOUT=60 tests/e2e/run.sh # allow longer for slow/cold (or qemu) starts
```

It builds/uses `aviation_feeder:e2e` by default; override the tag with
`AVIATION_FEEDER_IMAGE` (e.g.
`AVIATION_FEEDER_IMAGE=aviation_feeder:dev SKIP_BUILD=1 tests/e2e/run.sh` to
test an image you already built). Format/lint hooks (prettier, shellcheck,
shfmt, hadolint, codespell) are in `.pre-commit-config.yaml`; run
`prek run --all-files`.

The add-on targets **`amd64` and `aarch64`** (`config.json` `arch`). The one
arch-sensitive feeder is `rbfeeder` (see its section); when verifying arm64,
prefer a **native build or downloading/inspecting the image** over slow qemu
buildx.

> Base defaults (e.g. the readsb/mlathub ports in §4) come from the upstream
> [`docker-adsb-ultrafeeder`](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder)
> image and each feeder's own `sdr-enthusiasts` image — confirm against those.

## 2. The s6-rc service model

Services live under `rootfs/etc/s6-overlay/s6-rc.d/<name>/` in standard s6-rc
layout:

- `type` — `oneshot` or `longrun` (feeders are longruns; `wait-readsb` is a
  oneshot).
- `run` — the longrun's script (our feeders' run scripts live here).
- `up` — a oneshot's command (`wait-readsb/up` execs `wait-for-readsb`).
- `dependencies.d/<other>` — ordering: an empty file named after another service
  means "start after it". Most feeders carry `dependencies.d/wait-readsb`.
- `.../user/contents.d/<name>` — an empty marker enrolling a service into the
  `user` bundle so s6 actually starts it.

`rootfs/etc/s6-overlay/scripts/` holds the heavier shell logic that `run`
scripts `exec` into (some copied from upstream images, some ours — e.g.
`link-tmpfs-dir`), and `rootfs/usr/local/bin/` holds the shared helpers
(`feeder-gate`, `wait-for-readsb`, `rbfeeder-mlat`, `s6wrap-color`).

### Startup ordering: `wait-readsb`

`wait-readsb` is a oneshot whose `up` runs `wait-for-readsb`: it blocks (bash
`/dev/tcp`) until readsb's Beast port `:30005` accepts a TCP connection, then
releases the feeders that depend on it — so net-input feeders don't race readsb
at boot and spew "connection refused". It **fails open after ~30 s** so a readsb
problem can never wedge everything queued behind it. Most feeder longruns (and
the base `mlathub`/`mlat-client`) carry `dependencies.d/wait-readsb`; the
exception is `fr24uat-feed`, whose service dir is copied from the fr24 image and
ships no such marker.

### Run-script convention + the gating helper (`feeder-gate`)

Every account-based-feeder `run` script follows the same shape: source the shared
gate, gate on the toggle (and required config), then `exec` the real feeder.

```sh
#!/command/with-contenv sh
. /usr/local/bin/feeder-gate
gate "[fr24feed]" ENABLE_FR24 FR24KEY
exec /etc/s6-overlay/scripts/fr24feed
```

`feeder-gate` (`rootfs/usr/local/bin/feeder-gate`) defines a `gate` function you
**source** (not exec):

```
gate "[tag]" ENABLE_VAR [REQUIRED_VAR ...]
```

- Arguments are env-var **names**, resolved by `eval` indirection so it works
  sourced from both `sh` (dash) and `bash`. Names are hardcoded literals in the
  run scripts (never user input), so the eval is safe.
- If `ENABLE_VAR` isn't exactly `true`, or any `REQUIRED_VAR` is empty, it
  prints one `"[tag] disabled (…); idling"` line and `exec sleep infinity` — the
  service stays "up" but does nothing instead of crash-looping.
- Otherwise it returns and the run script `exec`s the real binary.

The same discipline is mirrored in the config oneshots (`01-fr24feed`,
`01-opensky-network.sh`, `02-rbfeeder`): the upstream `-real` scripts hard-exit
(or `sleep infinity`) on missing credentials, which would fail s6 init on every
install where the feeder is off, so our gated wrappers skip cleanly.

### readsb self-loop guard (`readsb/run`)

`readsb/run` overrides the base longrun. Because `BEASTHOST`/`BEASTPORT` are
global (for the feeders), the stock `scripts/readsb` would append
`--net-connector=localhost,30005,beast_in` — connecting readsb to its **own**
Beast output (observed ~7M msg/s, a pegged core). The override imports the
environment via `with-contenv`, `unset`s
`BEASTHOST BEASTPORT MLATHOST MLATPORT`, then runs the upstream script as a
**bash argument** (`exec bash /etc/s6-overlay/scripts/readsb`) so a second
`with-contenv` doesn't re-import them. It also re-establishes the `/run/readsb`
tmpfs symlink first (the finish script `rm -rf`s it on every exit).
`collectd/run` does the equivalent for `/run/collectd`.

### Log tagging

The base pipes engines through `s6wrap`; the Dockerfile swaps in `s6wrap-color`
(renaming the real one to `s6wrap.real`) so each `[tag]` gets a stable
per-engine colour from a six-colour palette (31-36; grey/37 is avoided — HA's
log renderer shows it ~identical to default text, i.e. "uncolored"). Services
whose upstream binaries self-log (adsbhub, opensky, the rbfeeder mlat-client)
are wrapped explicitly. Two engines also emit a **redundant own timestamp** that
their `run` scripts strip so it doesn't double up with s6wrap's: `pw-feeder`
(zerolog `[pw-feeder] <RFC3339> …`, routed through s6wrap + sed) and `rbfeeder`
(its binary's `[YYYY-MM-DD HH:MM:SS]`, post-filtered after the base longrun).

## 3. The feeders

### 3a. Community aggregators (readsb net-connectors — no build artifact)

These are **config-only**: there is **no binary and no build stage**. Each
enabled one is compiled by `00-haos-options`'s `add_aggregator` into
`ULTRAFEEDER_CONFIG` as a `beast_reduce_plus_out` adsb connector plus (where the
aggregator offers MLAT) an `mlat` connector, both tagged with the station UUID
(or its per-aggregator override). readsb (from the base image) makes the
outbound connections and reports each connector's state via
`readsb_net_connector_status{host=…,port=…}` in `stats.prom` — which is what the
MQTT publisher reads for per-aggregator status. Ports are **not** uniform; each
row is verified against the `add_aggregator` calls.

| Option (`feed_*`)    | Aggregator                                      | ADS-B host:port                | MLAT host:port                 |
| -------------------- | ----------------------------------------------- | ------------------------------ | ------------------------------ |
| `feed_adsblol`       | [adsb.lol](https://adsb.lol/)                   | `in.adsb.lol:30004`            | `in.adsb.lol:31090`            |
| `feed_adsbfi`        | [adsb.fi](https://adsb.fi/)                     | `feed.adsb.fi:30004`           | `feed.adsb.fi:31090`           |
| `feed_airplaneslive` | [airplanes.live](https://airplanes.live/)       | `feed.airplanes.live:30004`    | `feed.airplanes.live:31090`    |
| `feed_planespotters` | [Planespotters](https://www.planespotters.net/) | `feed.planespotters.net:30004` | `mlat.planespotters.net:31090` |
| `feed_theairtraffic` | [TheAirTraffic](https://theairtraffic.com/)     | `feed.theairtraffic.com:30004` | `feed.theairtraffic.com:31090` |
| `feed_avdelphi`      | [AVDelphi](https://www.avdelphi.com/)           | `data.avdelphi.com:24999`      | _(no MLAT)_                    |
| `feed_flyitaly`      | [Fly Italy ADSB](https://flyitalyadsb.com/)     | `dati.flyitalyadsb.com:4905`   | `dati.flyitalyadsb.com:30100`  |
| `feed_adsbitalia`    | [ADSBItalia](https://www.adsbitalia.it/)        | `feed.adsbitalia.it:31108`     | `mlat.adsbitalia.it:41113`     |
| `feed_adsbexchange`  | [ADS-B Exchange](https://www.adsbexchange.com/) | `feed1.adsbexchange.com:30004` | `feed.adsbexchange.com:31090`  |
| `feed_adsbone`       | [adsb.one](https://adsb.one/)                   | `feed.adsb.one:64004`          | `feed.adsb.one:64006`          |
| `feed_hpradar`       | [HpRadar](https://hpradar.com/)                 | `skyfeed.hpradar.com:30004`    | `skyfeed.hpradar.com:31090`    |

Each aggregator has a matching `<name>_uuid` override option; blank falls back
to the shared `UUID`. The MLAT column becomes a base `mlat-client` (returning
into mlathub). ADS-B Exchange additionally shares receiver stats unless
`ADSBX_STATS=disabled` is set via `extra_env`.

### 3b. Client-binary feeders

Every one is an s6 longrun gated via `feeder-gate`; when disabled/unconfigured
it idles on `sleep infinity`. ADS-B input is readsb Beast `localhost:30005`
unless noted. The digests below are the current pins (Renovate-managed — read
the Dockerfile for live values).

#### piaware (FlightAware)

- **What / how it runs.** FlightAware's `piaware` (a Tcl program via
  `tcllauncher`) configured as a **relay** pointing at our readsb (`BEASTHOST`
  reaches its `01-piaware` config oneshot; the e2e suite asserts
  `receiver-type "relay"` in `/etc/piaware.conf`). Its own
  dump1090/skyaware/beast-splitter are **not** used — readsb + dump978 own those
  roles.
- **Gated on.** `ENABLE_PIAWARE` (needs no key — FlightAware issues a feeder ID
  on first connect).
- **Input / out.** Beast :30005 → FlightAware.
- **MLAT.** `fa-mlat-client`, **API-isolated**: the base ships mlat-client 0.4.2
  for the community clients, but FlightAware's client targets the incompatible
  0.2.13 `mlat` API, so it lives in a private `/opt/fa-mlat` and runs via a
  `PYTHONPATH` wrapper. The Dockerfile smoke-tests **both** APIs at build time
  so drift fails the build. Its results port is managed by the piaware base's own
  s6 service (which we copy unchanged), not set by us; code comments mention
  `30105`/`30106`, but that is the base's concern, not authoritative here.
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-piaware@sha256:8d3298…143015 AS piaware`.
  `COPY --from=piaware` brings `/usr/bin/piaware`, its Tcl library trees
  (`/usr/lib/piaware*`, `fa_adept_codec`), `tcllauncher` + `Tcllauncher1.10`,
  the s6 `piaware`/`01-piaware` services + scripts, and `fa-mlat-client` +
  `flightaware`/`mlat`/`_modes*.so` into `/opt/fa-mlat`. apt: the Tcl stack
  (`itcl3`, `tcl`, `tcl-tls`, `tcllib`, `tclx8.4`).

#### FlightRadar24 (fr24feed + fr24uat-feed)

- **What / how it runs.** One native `fr24feed` binary drives **both** bands as
  two longruns: `fr24feed` (1090) and `fr24uat-feed` (978, reading dump978 UAT
  on `UATHOST/UATPORT=localhost:30978`).
- **Gated on.** `ENABLE_FR24` + `FR24KEY` (1090); `ENABLE_FR24` + `FR24KEY_UAT`
  (978).
- **Input / out.** Beast :30005 (and dump978 :30978 for UAT) → FlightRadar24.
- **MLAT.** Handled internally by fr24feed.
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-flightradar24@sha256:f32968…0630f1 AS fr24`.
  `COPY --from=fr24` brings `/usr/bin/fr24feed` (re-symlinked to
  `/usr/local/bin/fr24feed`), the `fr24feed`/`fr24uat-feed` services + scripts,
  and `01-fr24feed` → `01-fr24feed-real` (shadowed by our gated wrapper). No
  extra apt.

#### PlaneFinder (pfclient)

- **What / how it runs.** `pfclient` reads Beast :30005 and serves its own
  status/map UI on **:30053** (declared in `config.json` but unpublished/`null`
  by default, like the other raw ports — publish it under the Network tab).
- **Gated on.** `ENABLE_PLANEFINDER` + `SHARECODE`.
- **MLAT.** Handled internally by pfclient.
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-planefinder@sha256:974f64…9799d38 AS planefinder`.
  `COPY --from=planefinder` brings `/usr/local/bin/pfclient`, its s6 service,
  and `01-pfclient` → `01-pfclient-real` (gated wrapper). No extra apt.

#### OpenSky (opensky-feeder)

- **What / how it runs.** `openskyd-dump1090` in **net-input** mode (connects to
  readsb, never touches the SDR). Our `scripts/opensky-feeder` override re-tags
  its self-logged `awk` output to match the other services.
- **Gated on.** `ENABLE_OPENSKY` + `OPENSKY_USERNAME` + `LAT`/`LONG`/`ALT`
  (openskyd hard-exits without a position).
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-opensky-network@sha256:d0a0aa…a564644 AS opensky`.
  `COPY --from=opensky` brings `/usr/bin/openskyd-dump1090`, its s6 service, and
  `01-opensky-network` → `01-opensky-network-real.sh` (gated wrapper); the build
  sets `ENV OPENSKY_DEVICE_TYPE=default` and creates its conf dirs. No extra
  apt.

#### ADSBHub (adsbhubclient)

- **What / how it runs.** `adsbhub.sh` pipes readsb's **SBS/BaseStation** stream
  (`SBSHOST/SBSPORT=localhost:30003`) to ADSBHub via `socat`. Routed through
  `s6wrap --prepend=adsbhubclient` because the upstream script echoes bare
  lines.
- **Gated on.** `ENABLE_ADSBHUB` + `CLIENTKEY`. No MLAT (ADSBHub has none).
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-adsbhub@sha256:7f896c…ffcb14 AS adsbhub`.
  `COPY --from=adsbhub` brings `/usr/bin/adsbhub.sh`, its s6 service, and
  `01-adsbhubclient` → `01-adsbhubclient-real`. apt: `socat`.

#### AirNav RadarBox (rbfeeder + rbfeeder-mlat)

- **What / how it runs.** `rbfeeder` is **ARM-only**: native on `aarch64`,
  armhf-under-qemu on `amd64` (`rbfeeder_wrapper.sh` picks at run time).
- **Gated on.** `ENABLE_RADARBOX` + `SHARING_KEY`.
- **Config fixes.** The gate-wrapped `02-rbfeeder` oneshot (bounded by
  `timeout 30` because the upstream script `sleep infinity`s on hosts with no
  thermal sensor) rewrites `/etc/rbfeeder.ini`:
  - **Alt normalization** — rbfeeder silently refuses to autostart MLAT unless
    `alt=` is a bare number; a `sed` strips any unit suffix.
  - **`intern_port=32208`** — moved off the default `32008`, which collides with
    readsb's SBS-input block (`--net-sbs-in-port=32006` opens 32006–32009) and
    crash-loops readsb.
  - **`mlat_cmd=/usr/local/bin/rbfeeder-mlat --results beast,listen,30107`** —
    points rbfeeder's autostarted mlat-client at our shim in **listen** mode on
    **30107**. `beast_input_port` stays at its default `32004` (the client
    auto-appends `--results beast,connect,127.0.0.1:32004`), so it must not
    move.
- **MLAT.** `rbfeeder-mlat` (`rootfs/usr/local/bin/`) `exec`s the real
  `mlat-client` but re-prefixes its inherited stdout with a yellow `[mlat]` (the
  same colour s6wrap-color gives `[rbfeeder]`, so the prefix reads as one). We
  deliberately do **not** set `MLAT_RESULTS_BEASTHOST/PORT` (force connect-mode,
  breaks autostart) or the global `VERBOSE_LOGGING` (a shared flag piaware/fr24
  also read).
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-radarbox@sha256:835abb…878b20 AS radarbox`.
  In the stage, a `RUN` stages _exactly this arch's_ runtime into `/rbx`: native
  `rbfeeder_arm` + `rbfeeder_wrapper.sh` always, plus (on amd64 only)
  `qemu-arm-static` + the `arm-linux-gnueabihf` tree. The final image does one
  arch-agnostic `COPY --from=radarbox /rbx/ /` (a plain COPY of the qemu/armhf
  files would fail to build on arm64 where they don't exist), symlinks
  `rbfeeder`, and copies the s6 service + its oneshot chain
  (`01-show-rbfeeder-changelog` → `02-rbfeeder` → `03-show-architecture`), with
  `02-rbfeeder` → `02-rbfeeder-real` (gated wrapper) and
  `ENV RBFEEDER_LOG_FILE=/var/log/rbfeeder.log`. apt (native arm64 deps,
  harmless on amd64): `libjansson4`, `librtlsdr0`, `libbladerf2` (its other deps
  `libcurl4t64`/`libglib2.0-0t64`/`libprotobuf-c1` are already in the base).

#### plane.watch (pw-feeder + planewatch-mlat)

- **What / how it runs.** `pw-feeder` is a native multi-arch **Go** binary
  (glibc-only). It reads Beast :30005, feeds plane.watch, and opens a local MLAT
  relay on `MLATSERVERHOST:MLATSERVERPORT` (default `127.0.0.1:12346`).
- **Gated on.** `ENABLE_PLANEWATCH` + `PLANEWATCH_API_KEY` (both `pw-feeder` and
  `planewatch-mlat`; the mlat also requires `LAT`/`LONG`/`ALT`).
- **MLAT.** The separate `planewatch-mlat` longrun runs a `mlat-client` that
  reads Beast :30005, syncs to plane.watch **through** the relay
  (`--server 127.0.0.1:12346`, authed with the API key), and republishes results
  on **30108**; it depends on `pw-feeder` (+ 5 s sleep) so the relay binds
  first.
- **Special handling.** `pw-feeder/run` exports a **scoped**
  `SSL_CERT_FILE=/usr/local/share/pw-feeder-ca.crt` — plane.watch's TLS uses
  Let's Encrypt Gen-Y roots that Go's `crypto/x509` won't bridge against the
  stock trixie store, so only this process trusts plane.watch's own CA bundle;
  the global store stays stock.
- **How it's built.** Stage
  `FROM ghcr.io/plane-watch/docker-plane-watch@sha256:f98637…e8874f AS planewatch`.
  Two copies: `COPY --from=planewatch /usr/local/sbin/pw-feeder …` (the Go
  binary, no extra shared libs) and
  `COPY --from=planewatch /etc/ssl/certs/ca-certificates.crt /usr/local/share/pw-feeder-ca.crt`
  (the scoped CA bundle, versioned with this digest). The gated s6 services
  (`pw-feeder`, `planewatch-mlat`) live in `rootfs/`. No extra apt.

#### RadarVirtuel / adsbnetwork (radarvirtuel + radarvirtuel-mlat)

- **What / how it runs.** `docker-entrypoint.py` (pure Python) reads readsb's
  `aircraft.json` directly (its built-in `AIRCRAFT_SOURCES`; we do **not** set
  `RV_AIRCRAFT_URL` because the entrypoint ignores localhost URLs) and POSTs to
  RadarVirtuel, auto-registering the station from `RV_CONTRIB_NAME`/`EMAIL` and
  persisting `station_id`/`station_uid` to `/data`.
- **Gated on.** `ENABLE_RADARVIRTUEL` + `RV_CONTRIB_NAME` + `RV_CONTRIB_EMAIL`
  (mlat also requires `LAT`/`LONG`/`ALT`). Note the bridge var mismatch handled
  in the run scripts: the feeder wants `RV_LON`, our global is `LONG`.
- **MLAT.** `radarvirtuel-mlat` waits for the `station_id`/`station_uid` files,
  then runs a `mlat-client` to `${RV_MLAT_SERVER:-mlat.adsbnetwork.com:50000}`,
  reading Beast :30005 and republishing results on **30109**, authed with the
  persisted station id/uid.
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-radarvirtuel@sha256:1f6543…57bc34 AS radarvirtuel`.
  `COPY --from=radarvirtuel` brings `/docker-entrypoint.py` and `/opt/feeder_rv`
  (we do **not** copy its bundled readsb or s6 scripts — our `rootfs/` gates
  it). apt: `python3-requests` (the feeder hard-exits without it).

#### sdrmap (sdrmap + sdrmap-stunnel + sdrmap-mlat)

- **What / how it runs.** Three cooperating longruns. `sdrmap` runs
  `sdrmapfeeder.sh` (self-contained shell), which scrapes readsb's
  `aircraft.json` and HTTPS-POSTs to sdrmap. (Exports `LON` from `LONG`.)
- **Gated on.** `ENABLE_SDRMAP` + `SMUSERNAME` + `SMPASSWORD` (the stunnel +
  mlat also require `LAT`/`LONG`/`ALT`).
- **MLAT (TLS-tunnelled).** `sdrmap-stunnel` runs apt `stunnel` with
  `rootfs/etc/stunnel/mlat.conf`: listens on `127.0.0.1:3333`, forwards
  TLS-wrapped to `mlat.feed.sdrmap.org:3334`. `sdrmap-mlat` runs a `mlat-client`
  reading Beast :30005, connecting to the **local** stunnel endpoint
  (`--server 127.0.0.1:3333`), authed as `${SMUSERNAME}:${SMPASSWORD}`, and
  republishing results on **30110**; it depends on `sdrmap-stunnel` (+ 10 s
  sleep) so the tunnel binds first.
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-sdrmap@sha256:57c87f…bb8c40 AS sdrmap`.
  `COPY --from=sdrmap /usr/lib/sdrmapfeeder/sdrmapfeeder.sh …` (arch-independent
  shell). The gated s6 services + `/etc/stunnel/mlat.conf` live in `rootfs/`.
  apt: `stunnel4` (installed rather than porting their hand-built binary — apt
  also pulls its OpenSSL dep `libssl3t64`).

#### 1090MHz UK (uk1090)

- **What / how it runs.** One longrun. `uk1090` runs the closed-source `radar`
  binary (radar1090 v2.x) foregrounded (`-f`), reading our readsb Beast output
  (`-l localhost`, radar's default `:30005`) and forwarding to 1090MHz UK's
  aggregator (radar's compiled-in default host). ADS-B only — **no MLAT**.
  Wrapped in `s6wrap` for the coloured `[uk1090]` tag (radar prints no tag).
- **Gated on.** `ENABLE_UK1090` + `RADAR1090_KEY`. Depends on `wait-readsb`.
- **How it's built.** Stage
  `FROM ghcr.io/sdr-enthusiasts/docker-radar1090@sha256:45ebf7…fb8f5 AS radar1090`.
  `COPY --from=radar1090 /usr/sbin/radar …` — a self-contained glibc binary
  (needs only libc; verified by `ldd`), so no extra apt deps. We ship our own
  gated s6 service rather than radar1090's (its s6 scripts depend on
  image-specific files like `/.CONTAINER_VERSION`).
- **Feeding-state.** `conn` mode in `feeders.py` (persistent outbound Beast
  connection; token `sbin/radar`); byte throughput via the kernel per-socket
  counters (`THROUGHPUT_KERNEL`).

### dump978 (978 MHz UAT decoder — infrastructure, not an aggregator)

Not a feeder but built the same way. `dump978-fa` decodes 978 MHz UAT and its
stream is fed **into** readsb (`uat_in`). Its `run` gate idles it unless
`ENABLE_UAT=true` **and** `RECEIVER_MODE` is not `remote` — so it runs in both
`rtlsdr` mode (a second 978 stick) and `uat`-only mode; in `remote` mode there is
no local SDR and UAT arrives via the remote Beast. **How it's built:** stage
`FROM ghcr.io/sdr-enthusiasts/docker-dump978@sha256:d223d9…3da2ae AS dump978`;
`COPY --from=dump978` brings `/usr/local/bin/dump978-fa` and the `dump978`
service

- script (its upstream deps are replaced with a `legacy-cont-init` dep). apt:
  `libboost-program-options1.83.0`.

## 4. Ports map

readsb/mlathub defaults are from the base `scripts/readsb`/`scripts/mlathub`;
per-feeder results ports are from our run scripts / `02-rbfeeder`.

| Port            | Owner / role                                              | Source                               |
| --------------- | --------------------------------------------------------- | ------------------------------------ |
| 80              | tar1090 web UI (ingress; host-mapped to 8504)             | `config.json`                        |
| 30001           | readsb raw input (`--net-ri-port`)                        | base `scripts/readsb`                |
| 30002           | readsb raw output (`--net-ro-port`)                       | base `scripts/readsb`                |
| 30003           | readsb SBS/BaseStation output (`--net-sbs-port`)          | base `scripts/readsb`                |
| 30005           | readsb Beast output — primary feed source                 | base `scripts/readsb`                |
| 30053           | pfclient status/map UI (`null`/unpublished by default)    | `config.json`                        |
| 30107           | rbfeeder mlat-client results (listen)                     | `scripts/02-rbfeeder`                |
| 30108           | planewatch-mlat results (listen)                          | `planewatch-mlat/run`                |
| 30109           | radarvirtuel-mlat results (listen)                        | `radarvirtuel-mlat/run`              |
| 30110           | sdrmap-mlat results (listen)                              | `sdrmap-mlat/run`                    |
| 30978           | dump978 raw UAT output (fed into readsb via `uat_in`)     | `config.json` / bridge               |
| 31003–31006     | mlathub SBS/beast-in/beast-out/beast-reduce-out           | base `scripts/mlathub`               |
| 32004           | rbfeeder `beast_input_port` (mlat-client connect target)  | `scripts/02-rbfeeder` (left default) |
| 32006–32009     | readsb SBS input block (`--net-sbs-in-port=32006`)        | base `scripts/readsb`                |
| 32208           | rbfeeder `intern_port` (moved off 32008 to dodge 32006–9) | `scripts/02-rbfeeder`                |
| 127.0.0.1:3333  | sdrmap stunnel accept → `mlat.feed.sdrmap.org:3334`       | `rootfs/etc/stunnel/mlat.conf`       |
| 127.0.0.1:12346 | pw-feeder local MLAT relay (`MLATSERVERHOST/PORT`)        | `pw-feeder/run` (default)            |

`config.json` maps container `80/tcp → 8504`; the raw data ports
`30003`/`30005`/`30978` **and** the pfclient UI port `30053` are all declared but
unpublished (`null`) by default. piaware's `fa-mlat-client` results port is
managed by the piaware base's own s6 service (copied unchanged), not set by us;
code comments mention `30105`/`30106`, but that is the base's concern, not
authoritative here.

## 5. The MQTT / HA-sensor publisher (`aviation_feeder_mqtt/`)

An optional paho-mqtt Python package run as the **`ha-mqtt`** longrun. Its run
script gates on `HA_SENSORS` (from the `ha_sensors` option), sets
`PYTHONPATH=/opt`, and execs
`python3 -m aviation_feeder_mqtt --options /data/options.json`. **How it's
built:** no stage — the `aviation_feeder_mqtt` package is `COPY`d to
`/opt/aviation_feeder_mqtt` and `compileall`'d; apt: `python3-paho-mqtt`.

Everything the publisher reports is read **locally** — readsb's JSON/Prometheus
files, `/proc`, `NETLINK_INET_DIAG`, the mlat-client stats files, and a couple
of localhost status endpoints. No outbound network calls to the aggregators.

### Module layout

- **`app.py`** — the orchestrator. Parses `/data/options.json`; resolves the
  MQTT broker (blank host → Supervisor `mqtt` service via `supervisor.py`, else
  `core-mosquitto`); builds a paho 2.x client with an LWT
  (`<base_topic>/availability` retained `offline`); connects with retry; then
  loops every `mqtt_interval_seconds`. Each cycle: read `stats.json`;
  (re)publish HA **discovery** when needed; publish state for the enabled
  categories; publish a diagnostic heartbeat. It subscribes to
  `<discovery_prefix>/status` and re-sends discovery on HA's `online` birth
  message. Three watchdogs exit non-zero for s6 to restart it:
  `EXIT_DISCONNECTED` (11, MQTT down > 300 s), `EXIT_PUBLISH_STALL` (12,
  connected but state publishes stopped landing within the expire window), and
  `EXIT_LOOP_ERROR` (14, unhandled exception in the loop); a `finally` publishes
  retained `offline` on shutdown.
- **`mqtt.py`** — `MqttHealth` (connection/publish timestamps +
  `connect_count`), `mqtt_publish` (marks `last_state_publish_ok` for the stall
  watchdog), `connect_mqtt_with_retry` (exponential backoff), and the
  **discovery builders**: `build_discovery_payloads` (feeder-health sensors),
  `build_broker_discovery` (the MQTT broker-link diagnostics on the main
  device), `build_sdr_discovery` (local-SDR sensors), `build_nearby_discovery`
  (nearby numeric sensors + the "Nearest Aircraft" text entity with
  `json_attributes_topic`), `build_feeders_discovery` (per-feeder connectivity
  `binary_sensor`, `device_class: connectivity`), and
  `build_feeder_metrics_discovery` (per-feeder numeric sensors from a
  `FeederMetric` list). `_feeder_device` makes each feeder its **own** HA device
  (`aviation_feeder_feeders_<key>`) nested under the main device via
  `via_device`; per-feeder entities use `has_entity_name` + a function name
  ("Connection", "Uptime", …) so HA renders them as `<feeder> Connection`. All
  entities carry `availability_topic` + `expire_after`.
- **`metadata.py`** — the single source of truth for the metric set and the HA
  **device** identities. `METRICS` (feeder-health), `BROKER_METRICS` (MQTT
  link), `SDR_METRICS`, and `NEARBY_METRICS` each carry HA sensor metadata plus
  an `extract` lambda (feeder-health/SDR read `stats.json`: aircraft totals,
  `messages_per_sec` from `last1min`, `max_range_nm`/SDR levels; nearby reads
  the `compute_nearby()` dict). The per-feeder `FeederMetric` groups —
  `THROUGHPUT_METRICS` (Data Sent/Received), `MESSAGES_METRICS` (Messages),
  `UPTIME_METRICS` (Uptime), `MLAT_SYNC_METRICS` (MLAT Peers/Sync — from the mlat
  server's `stats` push, every MLAT feeder but RadarBox), and `MLAT_RESULT_METRICS`
  (MLAT Positions/min + Aircraft Used — written client-side by our mlat-client
  patch, so _every_ MLAT feeder incl. RadarBox; enabled by default like the sync
  metrics) — are attached selectively per
  feeder by `app.py`. The client-side write is added by `patch-mlat-client.py`, a
  build-time patch of the base image's vendored mlat-client (asserts its anchors,
  fails the build on upstream drift). Device ids: `aviation_feeder` (main; also
  carries the SDR receiver stats in rtlsdr mode), `aviation_feeder_nearby`,
  `aviation_feeder_feeders` (the per-feeder device-id prefix).
- **`feeders.py`** — per-feeder **feeding-state** (not just "running"), the crux
  of the design. Two feeder classes; the client class now has **three modes**
  (the `mode` column of `PROPRIETARY_FEEDERS`):
  - **Community aggregators** (`COMMUNITY_FEEDERS`, host:port matching the
    `add_aggregator` table) → read `readsb_net_connector_status{host,port}` from
    `/run/readsb/stats.prom`; `0` = disconnected, non-zero = connected.
  - **`"conn"`** (piaware, opensky, radarbox, adsbhub, plane.watch, uk1090) → feeding =
    the feeder process owns an **ESTABLISHED TCP socket to a non-loopback
    remote** (matched inode `/proc/<pid>/fd` ↔ `/proc/net/tcp{,6}`). Running but
    not feeding (e.g. pw-feeder up but failing TLS, shipping zero bytes) reads
    as **off**. This is also the socket the kernel throughput counters ride.
  - **`"report"`** (fr24, pfclient) → the feed is TCP-invisible (fr24 feeds over
    **UDP**, pfclient over its own path), so the feeder's own status endpoint is
    **authoritative** for connected-state (and throughput); falls back to
    "process running" only if the endpoint is unreachable.
  - **`"proc"`** (radarvirtuel, sdrmap) → periodic-POST feeders that hold no
    socket to probe, so the signal is "the binary is running".

    `running_cmdlines_by_pid()` **skips `s6-supervise …` processes** so an idled
    feeder (whose supervisor cmdline still embeds the service name) doesn't
    false-positive. Only feeders the user enabled are reported.
    `compute_feeder_status()` returns per-feeder connected-state;
    `compute_feeder_uptime()` returns per-feeder uptime — aggregator
    connect-seconds (the `net_connector_status` value) or the client process's
    own age (resets on reconnect/restart, so it's a measurement, not a monotonic
    total). `THROUGHPUT_KERNEL` is the frozenset of `"conn"` feeders whose byte
    throughput comes from the kernel per-socket counters.

- **`netdiag.py`** — per-socket byte counters via **`NETLINK_INET_DIAG`**
  (`SOCK_DIAG`), pure-Python (no `ss` fork, no extra capability). For each
  ESTABLISHED socket it returns the remote address, the socket inode, and the
  `tcp_info` `tcpi_bytes_acked`/`tcpi_bytes_received` u64s (length-guarded at
  the stable offsets 120/128). Returns `[]` on any error so a cycle never
  crashes on enumeration.
- **`throughput.py`** — `ThroughputAccumulator`: folds the netdiag counters into
  **stable, monotonic per-feeder totals**. The kernel counters are
  per-**socket** and reset to 0 on reconnect (new inode), so it tracks each
  inode's last-seen value and adds only the **non-negative delta** into a
  per-feeder running total (correct for an HA `total_increasing` sensor);
  loopback is excluded. Only the `THROUGHPUT_KERNEL` feeders are measured this
  way.
- **`mlat_stats.py`** — reads the mlat-client `--stats-json` files under
  `/run/mlat-client/*.json` and maps each back to its feeder key
  (`MLAT_STATS_BASENAMES`): the community clients (ultrafeeder names them
  `<mlat_host>:<mlat_port>`), the three commercial feeders (planewatch / sdrmap
  / radarvirtuel, explicit basenames set in their run scripts), and **radarbox**
  (written by the `rbfeeder-mlat` shim's own `--stats-json`). Exposes
  `mlat_peers` (`peer_count`) and `mlat_sync`
  (`good_sync_percentage_last_hour`). `MLAT_CAPABLE` is the set of keys with any
  MLAT (avdelphi and adsbhub have none). piaware is **not** here — it uses
  `fa-mlat-client`, so its MLAT health comes from its own `status.json`
  (`app_reports.py`).
- **`app_reports.py`** — per-feeder application self-reports, for the feeders
  the kernel socket path can't see or that carry semantic health: **piaware**
  `/run/piaware/status.json` (FlightAware/MLAT/radio `green|yellow|red` +
  `cpu_temp_c`, attributes only — piaware still feeds over TCP); **fr24**
  `http://localhost:8754/monitor.json` (`feed_status` → authoritative
  `connected`, plus `num_messages` — a count, UDP has no byte counter); and
  **pfclient** `http://localhost:30053/ajax/stats.php`
  (`master_server_bytes_out/in` → real cumulative throughput, authoritative
  `connected`). `gather_reports()` returns them for the enabled report-mode
  feeders; any error yields no report (fall back to the process/kernel signal).
- **`nearby.py`** — reads `aircraft.json`, haversine-filters aircraft within
  `ha_near_me_radius`, returns the in-range count plus the nearest aircraft's
  distance/altitude/bearing/speed.
- **`stats.py`** — defensive `stats.json` reader (tolerates a partial read
  racing readsb's writer).
- **`supervisor.py`** — resolves broker host/port/credentials from the
  Supervisor `mqtt` service (the add-on declares `services: [mqtt:want]`).

### HA device model

Discovery publishes several HA devices, gated by three independent toggles
(`ha_feeder_health`, `ha_planes_near_me`, `ha_feeder_status`); toggling a
category off publishes **retained-empty** configs so HA removes its entities.

- **Main "Aviation Feeder" device** (`ha_feeder_health`) — the `METRICS`
  feeder-health sensors **plus** the `BROKER_METRICS` MQTT broker-link
  diagnostics (link uptime, reconnect count — surfaces _how_ the link has
  behaved beyond the LWT online/offline).
- **"Aviation Feeder — Nearby"** (`ha_planes_near_me`) — the numeric nearby
  sensors + the "Nearest Aircraft" text entity. Station lat/long falls back to
  the bridge-resolved `LAT`/`LONG` env, so an inherited HA location still works
  when the options are blank; if neither resolves, planes-near-me self-disables.
- **Per-feeder devices** (`ha_feeder_status`) — each **enabled** feeder is its
  own device (`aviation_feeder_feeders_<key>`) nested under the main device via
  `via_device`. Every one carries a **Connection** connectivity `binary_sensor`
  (with a `json_attributes_topic` for the report-mode/piaware semantic health).
  Numeric metrics are attached **per applicability** (see below).
- **Local-SDR health** (`SDR_METRICS`: gain, ppm, signal/noise dBFS, samples
  dropped) is attached to the **main Aviation Feeder device** — it's receiver
  stats, not a separate thing. Only published when `receiver_mode` is a local
  SDR (not `remote`); in net-only mode readsb owns no SDR, so these are skipped
  (decided from config at startup, gated by `feeder_health and sdr_present`).

### Per-feeder metric applicability

Not every feeder gets every metric — the publisher attaches only the ones that
have an honest source, and **removes** the rest by publishing an empty retained
config for the non-applicable `(feeder, suffix)` topics (so an entity that no
longer applies — e.g. dropped from an older build — is deleted, not left
"unavailable"):

- **Uptime** — all enabled feeders.
- **Throughput** — the primary sensors are per-second **rates**
  (`Send/Receive Rate` B/s via `data_rate`, `THROUGHPUT_RATE_METRICS`), computed
  from the counter delta between cycles. The cumulative **Data Sent/Received**
  counters (`THROUGHPUT_METRICS`) are still published but
  `enabled_by_default: false`. Both apply only to the `THROUGHPUT_KERNEL`
  feeders (kernel per-socket counters) **plus pfclient** (its own
  `master_server_bytes_*`); fr24 (UDP), radarvirtuel/sdrmap (short-lived POSTs),
  and community aggregators (readsb doesn't split bytes per-connector) get none.
- **Messages** — fr24 only: a **Message Rate** (msg/s) primary sensor
  (`MESSAGES_RATE_METRICS`) plus a disabled-by-default cumulative `Messages`
  counter, since UDP has no byte counter.
- **MLAT Peers / MLAT Sync** — the `MLAT_SYNC_CAPABLE` feeders (every MLAT feeder
  _except_ RadarBox, whose server never pushes the sync stats): the community MLAT
  connectors + the commercial clients plane.watch, sdrmap, radarvirtuel. RadarBox
  reports no peers/sync, so **MLAT Positions / Aircraft Used** (client-side stats
  present for every MLAT feeder) are its only MLAT signal.
- **piaware MLAT / Radio** — piaware isn't `MLAT_CAPABLE` (it uses
  `fa-mlat-client`, no peer_count), so its MLAT + radio health from
  `status.json` are surfaced as `binary_sensor`s (`REPORT_BINARY_SENSORS`, on
  when the reported status is "green") — plus the full report as Connection
  attributes.

### The MLAT `--stats-json` wiring

The per-feeder MLAT sync numbers come from mlat-client's `--stats-json` output.
The three commercial MLAT run scripts — `planewatch-mlat/run`,
`sdrmap-mlat/run`, `radarvirtuel-mlat/run` — each pass
`--stats-json /run/mlat-client/<name>.json --stats-interval 30`, and the
`rbfeeder-mlat` shim appends the same flags onto rbfeeder's autostarted
mlat-client (writing `/run/mlat-client/radarbox.json`). The community clients
already write their `<mlat_host>:<mlat_port>.json` files from the base image, so
`mlat_stats.py` reads all of them uniformly. </content>
