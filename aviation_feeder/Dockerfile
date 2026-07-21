# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Aviation Feeder — merged ADS-B (1090) + UAT (978) + MLAT add-on.
#
# Layers the sdr-enthusiasts proprietary-feeder components onto the
# docker-adsb-ultrafeeder base (readsb + tar1090 + graphs1090 + mlat-client
# + the merged-Beast aggregator net_connectors). Each feeder is pulled from
# its own upstream image as a build stage and its binaries + s6 service are
# selectively copied in. See PLAN.md section 11 for the full manifest.
#
# All feeders are wired in: 978/UAT decode (dump978), the client-binary feeders
# (piaware, flightradar24, planefinder, opensky, adsbhub, plane.watch,
# radarvirtuel, sdrmap, rbfeeder) staged from their upstream images, plus the
# community aggregators configured at runtime (no build artifact).
# ---------------------------------------------------------------------------

# Base image is digest-pinned here via ARG BUILD_FROM. Declared before the first
# FROM so it is usable as a global build arg in the final-stage FROM. Builds
# (local and CI) use this pinned default -- we intentionally do NOT ship a
# build.yaml / let the HA builder override BUILD_FROM, so the digest stays fixed.
ARG BUILD_FROM=ghcr.io/sdr-enthusiasts/docker-adsb-ultrafeeder:latest-build-946@sha256:964ef12ff8651200cb713777dcb469a9e5c278b5df080cf59b2a68e69c096bcd

# --- Feeder source stages -------------------------------------------------
# Images pinned to tag@digest so Renovate's dockerfile manager (grouped as
# "sdr-enthusiasts base images") tracks and bumps them.
FROM ghcr.io/sdr-enthusiasts/docker-dump978:latest-build-801@sha256:9c04879d1b4003b2586ad63476612b0ec3f80c486a0bdf0861dba86c369f32cc AS dump978
FROM ghcr.io/sdr-enthusiasts/docker-piaware:latest-build-665@sha256:912921ca1800ca1cce4058aa04380f62d9214ad09be20f7addf67d31c119d1ec AS piaware
FROM ghcr.io/sdr-enthusiasts/docker-flightradar24:latest-build-858@sha256:488a0b3018daa11581daf3bd53ca6b6d596a615e8fae4031f54b3e4e51963ced AS fr24
FROM ghcr.io/sdr-enthusiasts/docker-planefinder:latest-build-540@sha256:b0521c923127bc9722a114a4bdab7ddf2b642f316bcc4a2503de6fdc125f2c7a AS planefinder
FROM ghcr.io/sdr-enthusiasts/docker-opensky-network:latest-build-843@sha256:dddfeb06f6723ec99d5d646cfc97973dbad577ab97ef5208440ce8d142e12398 AS opensky
FROM ghcr.io/sdr-enthusiasts/docker-adsbhub:latest-build-529@sha256:8d9953dc6b759539b2f7b236d31b2c143d934d6280591458526dc2c6d1363345 AS adsbhub
FROM ghcr.io/plane-watch/docker-plane-watch:v0.0.9@sha256:54531eb196367e03a44bd36cde58f408c9984c275f6b28e1b7cb9adf816eb520 AS planewatch
FROM ghcr.io/sdr-enthusiasts/docker-radarvirtuel:latest-build-799@sha256:b5b4236e0aed6b06d4112a3669d775e8cd2be1bde19e9e6225d7aab8ea4f4ec0 AS radarvirtuel
FROM ghcr.io/sdr-enthusiasts/docker-sdrmap:latest-build-98@sha256:8aab9e1c416f01f955a10d330016e1fc4b2d979e91249a9d7d15a062d3735212 AS sdrmap
FROM ghcr.io/sdr-enthusiasts/docker-radarbox:latest-build-882@sha256:248ce194ba48cbfb0686cfb5b73c991c94a812594f6b6e3007c38fdad6df5146 AS radarbox
# AirNav ships rbfeeder only for ARM. On arm64 the base image has a NATIVE
# rbfeeder (and no qemu); on amd64 it has the armhf binary + qemu-arm-static.
# Stage exactly the runtime this arch needs into /rbx so the final image can copy
# it arch-agnostically (a plain COPY of the qemu/armhf files would fail to build
# on arm64, where they don't exist). The native arm64 rbfeeder's shared-library
# deps are provided by apt in the final image (trixie-native, see the apt layer)
# rather than copied out of this radarbox image (also trixie), so they stay
# apt-managed and updatable instead of being pinned to whatever the feeder image
# happened to ship.
# rbfeeder_wrapper.sh picks native-vs-qemu at run time. On amd64 the rbfeeder
# binary is armhf-under-qemu, so the qemu branch below brings qemu + the armhf
# runtime tree; everything goes under /usr (a real dir) because /lib is a usrmerge
# symlink and a /rbx/lib directory would collide with it on COPY.
# (Comments are kept OUT of the `\`-continued RUN so a stray backslash can't
# silently break the arch copy.)
RUN set -eu; mkdir -p /rbx/usr/bin; \
    cp -a /usr/bin/rbfeeder_arm /usr/bin/rbfeeder_wrapper.sh /rbx/usr/bin/; \
    if [ -e /usr/bin/qemu-arm-static ]; then \
      cp -aL /usr/bin/qemu-arm-static /rbx/usr/bin/qemu-arm-static; \
      mkdir -p /rbx/usr/lib; \
      cp -a /usr/lib/arm-linux-gnueabihf /rbx/usr/lib/; \
      cp -aL /lib/ld-linux-armhf.so.3 /rbx/usr/lib/; \
    fi

# 1090MHz UK feeder source: just the self-contained `radar` binary (glibc-only).
FROM ghcr.io/sdr-enthusiasts/docker-radar1090:latest-build-315@sha256:ac5260a9a73dab64e0bccfb30723d59e7792a3493916f1a20ec3b32af2702df7 AS radar1090

# --- Final image ----------------------------------------------------------
FROM ${BUILD_FROM}

ARG BUILD_VERSION
ENV ADDON_VERSION=${BUILD_VERSION}

LABEL \
  org.opencontainers.image.title="Aviation Feeder" \
  org.opencontainers.image.description="Merged ADS-B / UAT / MLAT feeder built on docker-adsb-ultrafeeder." \
  org.opencontainers.image.source="https://github.com/bakerkj/hass-aviation-feeder" \
  org.opencontainers.image.licenses="MIT"

# --- Runtime packages the ultrafeeder base lacks --------------------------
# The Tcl stack for piaware and socat for adsbhub, in one layer. Versions are
# pinned and tracked by a Renovate custom manager (deb datasource) via the
# `# renovate:` annotations below.
RUN apt-get update \
  && apt-get install --no-install-recommends -y \
    # renovate: datasource=deb depName=itcl3
    itcl3=3.4.4-3 \
    # renovate: datasource=deb depName=tcl
    tcl=8.6.16 \
    # renovate: datasource=deb depName=tcl-tls
    tcl-tls=1.8.0-2 \
    # renovate: datasource=deb depName=tcllib
    tcllib=2.0+dfsg-4 \
    # renovate: datasource=deb depName=tclx8.4
    tclx8.4=8.4.1-5 \
    # renovate: datasource=deb depName=socat
    socat=1.8.0.3-1 \
    # renovate: datasource=deb depName=libboost-program-options1.83.0
    libboost-program-options1.83.0=1.83.0-4.2 \
    # paho-mqtt for the Home Assistant sensor publisher (aviation_feeder_mqtt).
    # renovate: datasource=deb depName=python3-paho-mqtt
    python3-paho-mqtt=2.1.0-1 \
    # requests for the RadarVirtuel Python feeder (feeder_radarvirtuel.py hard
    # exits without it). renovate: datasource=deb depName=python3-requests
    python3-requests=2.32.3+dfsg-5+deb13u1 \
    # stunnel for sdrmap's MLAT TLS tunnel (apt-installed rather than porting
    # their hand-built binary -- apt pulls its OpenSSL dep libssl3t64, the trixie
    # t64-transition name, too). renovate: datasource=deb depName=stunnel4
    stunnel4=3:5.74-2 \
    # Native runtime deps for rbfeeder on arm64 (its other deps -- libcurl4t64,
    # libglib2.0-0t64, libprotobuf-c1 -- are already in the base). Unused by the
    # amd64 qemu/armhf path but harmless there.
    # renovate: datasource=deb depName=libjansson4
    libjansson4=2.14-2+b3 \
    # renovate: datasource=deb depName=librtlsdr0
    librtlsdr0=2.0.2-2+b1 \
    # renovate: datasource=deb depName=libbladerf2
    libbladerf2=0.2024.05-1 \
  && rm -rf /var/lib/apt/lists/*

# --- dump978 (978 MHz UAT decoder) ----------------------------------------
# Binary lives in the dump978-full base at /usr/local/bin/dump978-fa.
COPY --from=dump978 /usr/local/bin/dump978-fa /usr/local/bin/dump978-fa
# The dump978 longrun service + its wrapper script (run execs this).
COPY --from=dump978 /etc/s6-overlay/s6-rc.d/dump978 /etc/s6-overlay/s6-rc.d/dump978
COPY --from=dump978 /etc/s6-overlay/scripts/dump978 /etc/s6-overlay/scripts/dump978
# dump978's stats aggregator: reads dump978's decoded-UAT stream on :30979 (+ raw
# :30978) and writes /run/stats/stats.json, which our ha-mqtt publisher turns into
# the HA "UAT" sensor device. Stdlib-only, so the single file is all we need; we
# run it from our OWN uat-stats s6 service (rootfs) rather than the upstream
# telegraf-gated launcher, which drags in services this base lacks.
COPY --from=dump978 /scripts/stats.py /scripts/stats.py
# The upstream dump978 service depends on optional oneshots (01-timezone,
# 02-sanity-check, 04-telegraf, 05-rtlsdr-biastee) that the ultrafeeder base
# does NOT provide. Replace those deps with `legacy-cont-init` so the service
# starts AFTER our /etc/cont-init.d env bridge has populated the container
# environment (otherwise it races ahead and reads an empty environment).
RUN rm -rf /etc/s6-overlay/s6-rc.d/dump978/dependencies.d \
  && mkdir -p /etc/s6-overlay/s6-rc.d/dump978/dependencies.d \
  && touch /etc/s6-overlay/s6-rc.d/dump978/dependencies.d/legacy-cont-init

# --- piaware (FlightAware feeder) -----------------------------------------
# (piaware's Tcl runtime is installed in the consolidated apt layer above.)
# piaware program + its Tcl library tree (includes helpers/faup1090), the real
# fa-mlat-client binary, and the tcllauncher prefix. Explicitly do NOT copy
# piaware's own dump1090/dump978/skyaware/beast-splitter (readsb + our dump978
# own those roles).
# piaware and its Tcl library tree (includes helpers/faup1090). piaware is a
# tcllauncher script, so it also needs the tcllauncher binary and the
# Tcllauncher package (registered at /usr/lib/Tcllauncher<ver>, NOT /opt/tcl).
COPY --from=piaware /usr/bin/piaware /usr/bin/piaware
COPY --from=piaware /usr/lib/piaware /usr/lib/piaware
# The piaware Tcl packages, on the auto_path via /usr/lib. `package require`
# pulls in several: the piaware package (piaware_packages), the ADEPT codecs
# (fa_adept_codec), and the config/status packages.
COPY --from=piaware /usr/lib/piaware_packages /usr/lib/piaware_packages
COPY --from=piaware /usr/lib/fa_adept_codec /usr/lib/fa_adept_codec
COPY --from=piaware /usr/lib/piaware-config /usr/lib/piaware-config
COPY --from=piaware /usr/lib/piaware-status /usr/lib/piaware-status
# piaware's fa-mlat-client is FlightAware's mlat-client wrapper, and its flightaware
# package targets the mlat-client 0.2.13 API. The ultrafeeder base ships mlat-client
# 0.4.2 for the community mlat-clients, and the two APIs are incompatible
# (Coordinator/JsonServerConnection signatures differ), so they cannot share one
# importable `mlat`. Isolate piaware's client: install it + its 0.2.13 deps in a
# private /opt/fa-mlat and run it via a PYTHONPATH wrapper, leaving the base's 0.4.2
# as the default for everyone else. Sharing one `mlat` breaks one side or the other
# (ModuleNotFoundError, or a Coordinator "missing stats_interval" TypeError).
COPY --from=piaware /usr/local/bin/fa-mlat-client /opt/fa-mlat/fa-mlat-client.py
# NB: the `python3.13` / `-313-` below are pinned to the piaware base's Python
# minor version. When the piaware base bumps to a new Python minor these COPY
# steps fail the build (missing source path) -- bump 3.13/313 here to match. (The .so
# is already globbed; the dist-packages dir path is the part that needs it.)
COPY --from=piaware /usr/local/lib/python3.13/dist-packages/flightaware /opt/fa-mlat/flightaware
COPY --from=piaware /usr/local/lib/python3.13/dist-packages/mlat /opt/fa-mlat/mlat
COPY --from=piaware /usr/local/lib/python3.13/dist-packages/_modes.cpython-313-*.so /opt/fa-mlat/
# Wrapper that runs fa-mlat-client with its private deps, plus build-time smoke
# tests: the isolated client resolves mlat from /opt/fa-mlat (0.2.13), and the base
# community mlat stays 0.4.2 (its Coordinator takes stats_interval). Either drift
# fails the build here instead of shipping broken MLAT.
# hadolint ignore=SC2016  # $PYTHONPATH is meant to stay literal in the wrapper
RUN printf '#!/bin/sh\nexport PYTHONPATH=/opt/fa-mlat${PYTHONPATH:+:$PYTHONPATH}\nexec /usr/bin/python3 /opt/fa-mlat/fa-mlat-client.py "$@"\n' > /usr/local/bin/fa-mlat-client \
  && chmod +x /usr/local/bin/fa-mlat-client \
  && PYTHONPATH=/opt/fa-mlat python3 -c "import flightaware.client.cli, mlat; assert mlat.__file__.startswith('/opt/fa-mlat'), mlat.__file__" \
  && python3 -c "import inspect; from mlat.client.coordinator import Coordinator; assert 'stats_interval' in inspect.signature(Coordinator.__init__).parameters"
COPY --from=piaware /usr/bin/tcllauncher /usr/bin/tcllauncher
COPY --from=piaware /usr/lib/Tcllauncher1.10 /usr/lib/Tcllauncher1.10
# s6 longrun + its config-writing oneshot + their wrapper scripts.
COPY --from=piaware /etc/s6-overlay/s6-rc.d/piaware /etc/s6-overlay/s6-rc.d/piaware
COPY --from=piaware /etc/s6-overlay/s6-rc.d/01-piaware /etc/s6-overlay/s6-rc.d/01-piaware
COPY --from=piaware /etc/s6-overlay/scripts/piaware /etc/s6-overlay/scripts/piaware
COPY --from=piaware /etc/s6-overlay/scripts/01-piaware /etc/s6-overlay/scripts/01-piaware
# 01-piaware writes piaware.conf from env, so it must run after our env bridge.
# (The piaware longrun already depends only on 01-piaware.)
RUN mkdir -p /etc/s6-overlay/s6-rc.d/01-piaware/dependencies.d \
  && touch /etc/s6-overlay/s6-rc.d/01-piaware/dependencies.d/legacy-cont-init

# --- flightradar24 (fr24feed: 1090 + UAT) ---------------------------------
# Single native binary serves both 1090 (fr24feed) and 978 (fr24uat-feed).
# Upstream's scripts always launch the binary as /usr/local/bin/fr24feed -- both the
# native-vs-qemu `--version` probe and the real `exec s6wrap ... fr24feed`. Install a
# thin wrapper there (not the stock symlink) that flips TZ=GMT for the binary ALONE.
# The binary demands GMT+0; pinning that at this single chokepoint lets the s6 run
# scripts -- and the s6wrap logger that timestamps fr24's output -- stay on the
# user's timezone, so fr24's log lines read in LOCAL time instead of 4h-ahead GMT.
# The binary gets GMT no matter who launches it; the `--version` probe still succeeds
# natively through the wrapper, so the qemu branch is never taken on our native arch.
COPY --from=fr24 /usr/bin/fr24feed /usr/bin/fr24feed
RUN printf '#!/bin/sh\nexec env TZ=GMT /usr/bin/fr24feed "$@"\n' > /usr/local/bin/fr24feed \
  && chmod +x /usr/local/bin/fr24feed \
  && grep -q 'TZ=GMT' /usr/local/bin/fr24feed
# s6 longruns (fr24feed, fr24uat-feed) + their shared oneshots + scripts.
COPY --from=fr24 /etc/s6-overlay/s6-rc.d/fr24feed /etc/s6-overlay/s6-rc.d/fr24feed
COPY --from=fr24 /etc/s6-overlay/s6-rc.d/fr24uat-feed /etc/s6-overlay/s6-rc.d/fr24uat-feed
COPY --from=fr24 /etc/s6-overlay/s6-rc.d/01-fr24feed /etc/s6-overlay/s6-rc.d/01-fr24feed
COPY --from=fr24 /etc/s6-overlay/s6-rc.d/02-show-architecture /etc/s6-overlay/s6-rc.d/02-show-architecture
COPY --from=fr24 /etc/s6-overlay/scripts/fr24feed /etc/s6-overlay/scripts/fr24feed
COPY --from=fr24 /etc/s6-overlay/scripts/fr24uat-feed /etc/s6-overlay/scripts/fr24uat-feed
# 01-fr24feed hard-exits on missing FR24KEY; route it through a gated wrapper
# (our rootfs) that skips cleanly when fr24 is disabled/unconfigured.
COPY --from=fr24 /etc/s6-overlay/scripts/01-fr24feed /etc/s6-overlay/scripts/01-fr24feed-real
COPY --from=fr24 /etc/s6-overlay/scripts/02-show-architecture /etc/s6-overlay/scripts/02-show-architecture
# 01-fr24feed writes fr24feed.ini from env, so it must run after our env bridge.
RUN mkdir -p /etc/s6-overlay/s6-rc.d/01-fr24feed/dependencies.d \
  && touch /etc/s6-overlay/s6-rc.d/01-fr24feed/dependencies.d/legacy-cont-init

# --- planefinder (pfclient) -----------------------------------------------
# Native per-arch binary at a fixed path. The config oneshot hard-exits on
# missing SHARECODE, so we copy it to a -real sidecar and our rootfs provides a
# gated wrapper at the original path.
COPY --from=planefinder /usr/local/bin/pfclient /usr/local/bin/pfclient
COPY --from=planefinder /etc/s6-overlay/s6-rc.d/pfclient /etc/s6-overlay/s6-rc.d/pfclient
COPY --from=planefinder /etc/s6-overlay/s6-rc.d/01-pfclient /etc/s6-overlay/s6-rc.d/01-pfclient
COPY --from=planefinder /etc/s6-overlay/scripts/pfclient /etc/s6-overlay/scripts/pfclient
COPY --from=planefinder /etc/s6-overlay/scripts/01-pfclient /etc/s6-overlay/scripts/01-pfclient-real
RUN mkdir -p /etc/s6-overlay/s6-rc.d/01-pfclient/dependencies.d \
  && touch /etc/s6-overlay/s6-rc.d/01-pfclient/dependencies.d/legacy-cont-init

# --- opensky-network (opensky-feeder) -------------------------------------
# The feeder binary is openskyd-dump1090 run in NET-INPUT mode (it connects to
# readsb, never touches the SDR). Config oneshot hard-exits on missing config,
# so same -real sidecar + gated wrapper treatment.
COPY --from=opensky /usr/bin/openskyd-dump1090 /usr/bin/openskyd-dump1090
COPY --from=opensky /etc/s6-overlay/s6-rc.d/opensky-feeder /etc/s6-overlay/s6-rc.d/opensky-feeder
COPY --from=opensky /etc/s6-overlay/s6-rc.d/01-opensky-network /etc/s6-overlay/s6-rc.d/01-opensky-network
COPY --from=opensky /etc/s6-overlay/scripts/opensky-feeder /etc/s6-overlay/scripts/opensky-feeder
COPY --from=opensky /etc/s6-overlay/scripts/01-opensky-network.sh /etc/s6-overlay/scripts/01-opensky-network-real.sh
RUN mkdir -p /etc/s6-overlay/s6-rc.d/01-opensky-network/dependencies.d \
  && touch /etc/s6-overlay/s6-rc.d/01-opensky-network/dependencies.d/legacy-cont-init \
  && mkdir -p /var/lib/openskyd/conf.d /etc/openskyd/conf.d
# opensky ENV default we don't inherit. 01-opensky-network.sh writes
# Type=${OPENSKY_DEVICE_TYPE} into the config; unset yields Type= (openskyd
# treats empty as "default", so functionally identical) — restore the upstream
# value for config fidelity. opensky-only, safe.
ENV OPENSKY_DEVICE_TYPE=default

# --- plane.watch (pw-feeder) ----------------------------------------------
# pw-feeder is a native multi-arch Go binary (amd64+arm64), dynamically linked
# against glibc only -- no extra shared libs to stage. It reads BEASTHOST/
# BEASTPORT (our global localhost:30005) for ADS-B and opens a local MLAT relay
# on MLATSERVERHOST:MLATSERVERPORT (127.0.0.1:12346) that our planewatch-mlat
# service tunnels a mlat-client through. The gated s6 services live in rootfs.
COPY --from=planewatch /usr/local/sbin/pw-feeder /usr/local/sbin/pw-feeder
# NB: pw-feeder used to need plane.watch's own CA bundle staged here (and pointed
# at via SSL_CERT_FILE in its s6 run script): its TLS verify put the SYSTEM cert
# pool in x509.VerifyOptions.Intermediates, so Let's Encrypt's Gen-Y cross-sign
# chain -- which the server SENDS but which is not in the system store -- could
# never be built, and it failed "x509: certificate signed by unknown authority".
# pw-feeder v0.0.9 (plane-watch/pw-feeder 2dcd20d, "verify TLS chains using
# server-provided intermediates") now builds the intermediates pool from the certs
# the server presents and anchors in the system roots -- where ISRG Root X1 already
# lives -- so the stock trust store validates and the workaround is gone.

# --- RadarVirtuel / adsbnetwork (pure-Python feeder) ----------------------
# No compiled binary: feeder_radarvirtuel.py reads our readsb's
# /run/readsb/aircraft.json directly (its built-in AIRCRAFT_SOURCES; we do NOT set
# RV_AIRCRAFT_URL -- the upstream entrypoint ignores a localhost URL anyway) and POSTs
# to radarvirtuel, auto-registering the station from RV_CONTRIB_NAME/EMAIL
# (station id/uid persisted to /data). We do NOT copy its bundled readsb (we have
# our own) or its s6 scripts (our rootfs gates it). Needs python3-requests (apt).
COPY --from=radarvirtuel /docker-entrypoint.py /docker-entrypoint.py
COPY --from=radarvirtuel /opt/feeder_rv /opt/feeder_rv

# --- sdrmap (shell feeder + stunnel MLAT) ---------------------------------
# sdrmapfeeder.sh is a self-contained shell script (arch-independent): it scrapes
# our readsb /run/readsb/aircraft.json and HTTPS-POSTs to sdrmap. MLAT goes
# mlat-client -> local stunnel (127.0.0.1:3333) -> mlat.feed.sdrmap.org; stunnel
# is apt-installed (see the apt layer) rather than porting their hand-built
# binary, so libssl3 is resolved by apt. Our rootfs provides the gated s6
# services + /etc/stunnel/mlat.conf.
COPY --from=sdrmap /usr/lib/sdrmapfeeder/sdrmapfeeder.sh /usr/lib/sdrmapfeeder/sdrmapfeeder.sh

# --- 1090MHz UK (radar1090) -----------------------------------------------
# `radar` is a self-contained glibc binary (needs only libc -- verified by ldd):
# it reads our readsb Beast output (localhost:30005) and forwards to 1090MHz UK's
# aggregator (radar's compiled-in default host), keyed by the sharing key. ADS-B
# only (no MLAT). We copy just the binary; our rootfs provides the gated s6
# service (radar's own s6 scripts depend on image-specific files we don't ship).
COPY --from=radar1090 /usr/sbin/radar /usr/sbin/radar

# --- adsbhub (adsbhubclient) ----------------------------------------------
# Shell + socat script consuming SBS (30003). socat is in the consolidated
# apt layer above.
COPY --from=adsbhub /usr/bin/adsbhub.sh /usr/bin/adsbhub.sh
COPY --from=adsbhub /etc/s6-overlay/s6-rc.d/adsbhubclient /etc/s6-overlay/s6-rc.d/adsbhubclient
COPY --from=adsbhub /etc/s6-overlay/s6-rc.d/01-adsbhubclient /etc/s6-overlay/s6-rc.d/01-adsbhubclient
COPY --from=adsbhub /etc/s6-overlay/scripts/adsbhubclient /etc/s6-overlay/scripts/adsbhubclient
COPY --from=adsbhub /etc/s6-overlay/scripts/01-adsbhubclient /etc/s6-overlay/scripts/01-adsbhubclient-real
RUN mkdir -p /etc/s6-overlay/s6-rc.d/01-adsbhubclient/dependencies.d \
  && touch /etc/s6-overlay/s6-rc.d/01-adsbhubclient/dependencies.d/legacy-cont-init

# --- radarbox (rbfeeder) --------------------------------------------------
# AirNav ships rbfeeder only for ARM: native on arm64, armhf-under-qemu on amd64.
# The radarbox stage staged exactly this arch's runtime into /rbx (rbfeeder_arm +
# wrapper always; on amd64 also qemu-arm-static + the armhf tree; on arm64 the
# native binary's shared libs). One arch-agnostic copy avoids referencing files
# that don't exist on the other arch. Do NOT copy dump1090-rb (radarbox's decoder).
COPY --from=radarbox /rbx/ /
RUN ln -sf /usr/bin/rbfeeder_wrapper.sh /usr/bin/rbfeeder
# s6 longrun + its oneshot chain (01-changelog -> 02-rbfeeder -> 03-arch).
COPY --from=radarbox /etc/s6-overlay/s6-rc.d/rbfeeder /etc/s6-overlay/s6-rc.d/rbfeeder
COPY --from=radarbox /etc/s6-overlay/s6-rc.d/01-show-rbfeeder-changelog /etc/s6-overlay/s6-rc.d/01-show-rbfeeder-changelog
COPY --from=radarbox /etc/s6-overlay/s6-rc.d/02-rbfeeder /etc/s6-overlay/s6-rc.d/02-rbfeeder
COPY --from=radarbox /etc/s6-overlay/s6-rc.d/03-show-architecture /etc/s6-overlay/s6-rc.d/03-show-architecture
COPY --from=radarbox /etc/s6-overlay/scripts/rbfeeder /etc/s6-overlay/scripts/rbfeeder
# radarbox ENV default we don't inherit (we build FROM ultrafeeder, not radarbox):
# rbfeeder + its stats/watchdog scripts read RBFEEDER_LOG_FILE (unset -> repeated
# "Can't create log file" + broken stats).
#
# We intentionally do NOT set MLAT_RESULTS_BEASTHOST/PORT: those force the
# mlat-client into connect mode, which makes rbfeeder's MLAT autostart fail to
# wire up the client (MLAT never starts). Instead the 02-rbfeeder wrapper rewrites
# mlat_cmd to LISTEN mode on a free port, matching AirNav's own working setup.
# MLAT_SERVER is not read by the radarbox config script (rbfeeder learns its mlat
# server from the RB24 handshake), so it's omitted too.
# We deliberately do NOT set the global VERBOSE_LOGGING=true. It would make
# rbfeeder run unfiltered, but it is a shared flag: piaware reads it to add -debug
# (unbounded /var/log/piaware/current) and fr24 reads it to drop its stats filter.
# The RB stack state we actually want in the log -- the autostarted mlat-client's
# sync/positions -- inherits rbfeeder's stdout and is NOT matched by rbfeeder's
# default stats filter, so it already reaches the HA add-on log without the global
# flag. The rbfeeder-mlat shim re-tags those lines [mlat] so they're
# distinguishable from rbfeeder's own feed lines.
ENV RBFEEDER_LOG_FILE=/var/log/rbfeeder.log
COPY --from=radarbox /etc/s6-overlay/scripts/01-show-rbfeeder-changelog /etc/s6-overlay/scripts/01-show-rbfeeder-changelog
# 02-rbfeeder is ungated upstream and `sleep infinity`s on hosts without a
# thermal sensor (VMs, CI, some x86); route it through a gated wrapper so it
# only runs when RadarBox is enabled.
COPY --from=radarbox /etc/s6-overlay/scripts/02-rbfeeder /etc/s6-overlay/scripts/02-rbfeeder-real
COPY --from=radarbox /etc/s6-overlay/scripts/03-show-architecture /etc/s6-overlay/scripts/03-show-architecture
# 02-rbfeeder writes /etc/rbfeeder.ini from env, so it must run after the bridge.
RUN mkdir -p /etc/s6-overlay/s6-rc.d/02-rbfeeder/dependencies.d \
  && touch /etc/s6-overlay/s6-rc.d/02-rbfeeder/dependencies.d/legacy-cont-init

# --- Home Assistant sensor publisher --------------------------------------
# Python package (paho-mqtt) that publishes readsb feeder-health metrics to HA.
# Run as the ha-mqtt s6 longrun (see rootfs) via `python3 -m aviation_feeder_mqtt`.
COPY aviation_feeder_mqtt /opt/aviation_feeder_mqtt
# compileall precompiles every module (and so fails the build on a syntax error),
# but it never IMPORTS one -- a missing runtime dep (paho) would still sail
# through and only surface when the add-on starts. So also import each submodule
# against this image's python + apt-installed deps. __main__ is skipped: importing
# it would run the publisher. See .dockerignore for why no host __pycache__ is
# present to shadow this (compileall adds bytecode, it does not remove foreign
# bytecode that COPY brought in).
RUN python3 -m compileall -q /opt/aviation_feeder_mqtt \
  && PYTHONPATH=/opt python3 -c "\
import importlib, pkgutil, aviation_feeder_mqtt as pkg; \
[importlib.import_module('aviation_feeder_mqtt.' + m.name) \
 for m in pkgutil.iter_modules(pkg.__path__) if m.name != '__main__']"

# --- HAOS glue: config bridge + service enablement ------------------------
# 00-haos-options is committed with its executable bit set, which COPY
# preserves, so no chmod is needed here.
COPY rootfs/ /

# Colorize per-engine log tags: slip a thin shim in front of s6wrap (every
# service pipes through it) so each engine's [tag] gets a stable colour. The
# real binary is kept as s6wrap.real; the shim only rewrites --prepend.
RUN mv /usr/local/bin/s6wrap /usr/local/bin/s6wrap.real \
  && mv /usr/local/bin/s6wrap-color /usr/local/bin/s6wrap \
  && chmod +x /usr/local/bin/s6wrap

# Teach the base image's community mlat-client to also write its client-side
# stats (positions/minute, aircraft-used) into the --stats-json file, on its own
# timer -- so every mlat feeder (incl. RadarBox, whose server never pushes the
# `stats` message) reports them uniformly and our HA publisher reads one JSON per
# feeder. The patch script asserts the upstream anchors and FAILS THE BUILD if a
# base-image bump moved them (rather than silently losing stats at runtime).
COPY patch-mlat-client.py /tmp/patch-mlat-client.py
# Apply, then smoke-test the patched client imports and the helper is present, so
# a patch that produced broken Python fails the build here (not at runtime).
RUN python3 /tmp/patch-mlat-client.py \
  && python3 -c "import mlat.client.stats as s, mlat.client.jsonclient; assert hasattr(s, '_haf_write_client_stats'), 'stats patch missing'" \
  && rm -f /tmp/patch-mlat-client.py

# Neutralise the two container-level timezone side effects of fr24feed's config
# oneshot. Upstream 01-fr24feed-real (1) writes the SHARED s6 container-environment
# TZ file (`printf "GMT" > .../TZ`) because the fr24feed binary demands GMT+0, and
# (2) logs a WARNING that setting TZ "is ignored ... because fr24feed requires the
# container to use timezone GMT". Both are wrong for us: we keep the container on
# the user's tz and scope GMT to the fr24feed BINARY alone via the
# /usr/local/bin/fr24feed wrapper (above). So the global write -- which would clobber
# tar1090/graphs/logs/MQTT, nondeterministically racing sibling longruns -- is
# removed, AND the now-false warning is stripped so the log stops claiming the tz was
# ignored when it was honoured.
#
# Anchored + bounded + verified: both anchor strings must be present before the sed
# and both absent after, so the build FAILS loudly if upstream moves/renames either.
# The warning is its own fixed 3-line `if [ -n "$TZ" ]` block (a separate statement
# from the printf), deleted with a BOUNDED `addr,+2d` range rather than an open
# `addr,/^fi$/d`: were a future base to reshape the block so its closing `fi` no
# longer matched, an open range would delete to EOF -- and because both target
# strings would vanish as a side effect, the post-asserts could not catch it. The
# bounded delete can only ever remove 3 lines; a reshaped block then trips the
# post-assert instead.
# SC2016: the ${TZ} in the sed address is a LITERAL match against the script text,
# not a shell expansion -- single quotes are deliberate.
# hadolint ignore=SC2016
RUN F=/etc/s6-overlay/scripts/01-fr24feed-real \
  && grep -q 'printf "GMT" >/var/run/s6/container_environment/TZ' "$F" \
  && grep -q 'Setting timezone via TZ is ignored' "$F" \
  && sed -i -e '/printf "GMT" >\/var\/run\/s6\/container_environment\/TZ/d' -e '/^if \[ -n "${TZ}" \]; then$/,+2d' "$F" \
  && ! grep -q 'container_environment/TZ' "$F" \
  && ! grep -q 'Setting timezone via TZ is ignored' "$F"

# ADSBItalia registration name. The base's 52-adsbitalia-register startup hook
# derives the registered station name from MLAT_USER alone. Patch it to prefer
# ADSBITALIA_NAME (set by 00-haos-options from the adsbitalia_name option) so the
# per-aggregator name override reaches the registration record too -- matching how
# adsbitalia_name already names the MLAT connector. Blank => ADSBITALIA_NAME unset
# => falls back to MLAT_USER, exactly as before.
#
# Anchored + verified: the exact upstream line must be present before, and the
# patched form after, so a base bump that moves/renames it FAILS THE BUILD instead
# of silently reverting to the MLAT_USER-only name.
# hadolint ignore=SC2016
RUN F=/etc/s6-overlay/startup.d/52-adsbitalia-register \
  && grep -q 'FEEDER_NAME="${MLAT_USER:-$FEEDER_ID}"' "$F" \
  && sed -i 's/FEEDER_NAME="${MLAT_USER:-$FEEDER_ID}"/FEEDER_NAME="${ADSBITALIA_NAME:-${MLAT_USER:-$FEEDER_ID}}"/' "$F" \
  && grep -q 'FEEDER_NAME="${ADSBITALIA_NAME:-${MLAT_USER:-$FEEDER_ID}}"' "$F" \
  && ! grep -q 'FEEDER_NAME="${MLAT_USER:-$FEEDER_ID}"' "$F"

# Assert the two ENROLLED s6 boot surfaces match our explicit allowlist, so a base
# bump can't quietly enroll something new without our approval. The script prunes
# the two dead upstream wrappers we reject (telegraf, timelapse1090) and FAILS THE
# BUILD if a base-image bump enrolls a new service (user/contents.d) or adds a new
# startup hook (startup.d, e.g. an aggregator auto-register) we have not
# classified. (It polices enrollment, not transitive dependencies.d pulls -- see
# the script header.) Runs LAST so it validates the final tree, after
# `COPY rootfs/ /` and every s6 edit above.
COPY assert-units.py /tmp/assert-units.py
RUN python3 /tmp/assert-units.py && rm -f /tmp/assert-units.py
