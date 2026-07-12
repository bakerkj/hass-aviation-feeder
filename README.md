# Ken's Aviation Feeder Add-on

Home Assistant OS add-ons for ADS-B / UAT / MLAT flight tracking, built on the
[sdr-enthusiasts](https://github.com/sdr-enthusiasts) container stack.

## Add-on

- **[Aviation Feeder](aviation_feeder/)** — merged ADS-B (1090) + UAT (978) +
  MLAT feeder with a tar1090 map, feeding FlightAware, FlightRadar24,
  PlaneFinder, OpenSky, ADSBHub, RadarBox, and the adsb.lol / adsb.fi and other
  community aggregators.

## What it is

Aviation Feeder is a single Home Assistant add-on that **layers the account-based
per-network feeders onto the
[`docker-adsb-ultrafeeder`](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder)
base image**. Ultrafeeder supplies readsb (1090 decode), dump978 (978/UAT), the
tar1090 map, graphs1090, mlat-client, and the merged-Beast community
aggregators; this add-on adds piaware (FlightAware), fr24feed (FlightRadar24),
pfclient (PlaneFinder), the OpenSky feeder, adsbhub, and rbfeeder (AirNav
RadarBox — an ARM-only binary that AirNav ships, run natively on ARM and under
`qemu-arm-static` emulation on amd64, so RadarBox works on both), plus a config
bridge that maps Home Assistant add-on options onto the environment variables
all of those services expect. Everything runs in
one container under s6-overlay.

For the architecture in detail (image build, init scripts, services, config
bridge, and the optional Home Assistant sensors), see
[**How it works** in the add-on docs](aviation_feeder/DOCS.md#how-it-works).

## Install

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories** and add this URL:

   ```
   https://github.com/bakerkj/hass-aviation-feeder
   ```

2. The **Aviation Feeder** add-on now appears in the store — install it.
3. Open its **Configuration** tab, set your location and the aggregators you
   want, then start it. Full option reference:
   [add-on Documentation](aviation_feeder/DOCS.md).

## Development / Build / Test

The add-on image is built by `aviation_feeder/Dockerfile`. It uses multi-stage
builds to pull each per-network feeder from its upstream `sdr-enthusiasts` image
and copy just the binaries + s6 service files onto the ultrafeeder base. The
base image is digest-pinned in the Dockerfile via `ARG BUILD_FROM`; builds
(local and CI) use that pinned default rather than an externally supplied
`BUILD_FROM`.

### Build the image locally

```sh
docker build --build-arg BUILD_VERSION=dev -t aviation_feeder:dev aviation_feeder
```

The add-on is public and multi-arch (`amd64`, `aarch64`); `rbfeeder` is
ARM-only. To build/test `aarch64` on an `amd64` host, use Docker Buildx with
QEMU (`docker run --privileged --rm tonistiigi/binfmt --install arm64`).

### End-to-end tests

`tests/e2e/run.sh` builds the image and runs it against the option fixtures in
`tests/e2e/fixtures/`, asserting on container state, the compiled s6 container
environment, feeder service states, an actual readsb decode (an ADS-B frame is
injected — no real SDR needed), and the MQTT sensor publisher (against a live
broker with the station location inherited from a mocked Home Assistant).

```sh
tests/e2e/run.sh                 # build + run the full suite
SKIP_BUILD=1 tests/e2e/run.sh    # reuse an existing image
POLL_TIMEOUT=60 tests/e2e/run.sh # allow longer for slow/cold container starts
```

### Formatting / linting

Formatting and lint hooks are configured in `.pre-commit-config.yaml` (prettier,
shellcheck, shfmt, hadolint, codespell). Run them with
[`prek`](https://github.com/j178/prek) or `pre-commit`:

```sh
prek run --all-files
```

## License

MIT — see [LICENSE.md](LICENSE.md).
