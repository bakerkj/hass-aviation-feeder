#!/usr/bin/env bash
# End-to-end tests for the aviation_feeder add-on.
#
# Builds the image, runs it against option fixtures, and asserts on container
# state, the compiled s6 container environment, feeder service states, and an
# actual readsb decode. No real SDR is needed: this validates the s6 /
# config-bridge / feeder-gating / decode wiring, not RF reception. Feeders
# with dummy keys start and fail auth, which is expected.
#
# Assertions poll with a timeout so a slow/cold container start does not cause
# flaky failures.
#
# Env:
#   AVIATION_FEEDER_IMAGE  image tag to build/use (default aviation_feeder:e2e)
#   SKIP_BUILD=1           reuse an existing image instead of building
#   POLL_TIMEOUT           seconds to wait for a condition (default 30)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$(cd "${HERE}/../../aviation_feeder" && pwd)"
IMAGE="${AVIATION_FEEDER_IMAGE:-aviation_feeder:e2e}"
CONTAINER="aviation_feeder_e2e"
POLL_TIMEOUT="${POLL_TIMEOUT:-30}"

pass_count=0
fail_count=0

section() { printf '\n=== %s ===\n' "$*"; }
ok() {
  printf '  ok: %s\n' "$*"
  pass_count=$((pass_count + 1))
}
bad() {
  printf '  FAIL: %s\n' "$*"
  fail_count=$((fail_count + 1))
}

MQTT_BROKER="aviation_feeder_e2e_mqtt"
MQTT_NET="aviation_feeder_e2e_net"
API_MOCK="aviation_feeder_e2e_api"
# Per-case resource names. Every case runs in its own backgrounded worker with a
# unique container/sidecar/network set, so cases run concurrently without
# colliding. setup_case_names is called at the top of each case function; because
# each case fn declares `local CONTAINER MQTT_* API_MOCK`, these assignments are
# dynamically scoped to that case and the assertion helpers pick them up.
setup_case_names() { # $1 = short case id
  CONTAINER="aviation_feeder_e2e_$1"
  MQTT_BROKER="aviation_feeder_e2e_mqtt_$1"
  API_MOCK="aviation_feeder_e2e_api_$1"
  MQTT_NET="aviation_feeder_e2e_net_$1"
}
teardown_case() {
  docker rm -f "${CONTAINER}" "${MQTT_BROKER}" "${API_MOCK}" >/dev/null 2>&1 || true
  docker network rm "${MQTT_NET}" >/dev/null 2>&1 || true
}
RESULTS_DIR=""
cleanup() {
  # Sweep every per-case container/network by name prefix (covers anything a
  # crashed case left behind), then drop the results scratch dir.
  docker ps -aq --filter "name=aviation_feeder_e2e" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network ls -q --filter "name=aviation_feeder_e2e_net" | xargs -r docker network rm >/dev/null 2>&1 || true
  [ -n "${RESULTS_DIR}" ] && rm -rf "${RESULTS_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

status() { docker inspect -f '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || echo missing; }
env_val() { docker exec "${CONTAINER}" cat "/run/s6/container_environment/$1" 2>/dev/null || true; }
# Strip ANSI colour codes so log assertions match regardless of the per-engine
# tag colouring (the s6wrap shim wraps each [tag] in ANSI).
# sed's stderr is discarded: callers pipe into `grep -q`, which exits on the
# first match and closes the pipe, so sed reliably hits EPIPE on the unread
# remainder ("couldn't write N items to stdout: Broken pipe"). That is benign
# noise — the match already succeeded — so silence it.
logs() { docker logs "${CONTAINER}" 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' 2>/dev/null || true; }
aircraft_json() { docker exec "${CONTAINER}" cat /run/readsb/aircraft.json 2>/dev/null || true; }

# Poll a predicate until it succeeds or POLL_TIMEOUT elapses.
wait_for() {
  local deadline=$((SECONDS + POLL_TIMEOUT))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    if "$@"; then return 0; fi
    sleep 1
  done
  return 1
}

_running() { [ "$(status)" = "running" ]; }
_env_has() { case "$(env_val "$1")" in *"$2"*) return 0 ;; *) return 1 ;; esac }
_log_has() { logs | grep -qE "$1"; }
_decoded() { case "$(aircraft_json)" in *"$1"*) return 0 ;; *) return 1 ;; esac }
_file_exists() { docker exec "${CONTAINER}" test -f "$1" 2>/dev/null; }
_symlink_to() { [ "$(docker exec "${CONTAINER}" readlink "$1" 2>/dev/null)" = "$2" ]; }
_is_exec() { docker exec "${CONTAINER}" test -x "$1" 2>/dev/null; }
# Full argv of the running readsb process (NUL-delimited /proc/*/cmdline).
readsb_cmdline() {
  docker exec "${CONTAINER}" sh -c '
    for f in /proc/[0-9]*/cmdline; do
      c=$(tr "\0" " " <"$f" 2>/dev/null) || continue
      case "$c" in *"readsb --net"*) printf "%s\n" "$c" ;; esac
    done' 2>/dev/null || true
}
# Passes once readsb is up AND is NOT connected to its own 30005 Beast output.
# Guards the BEASTHOST self-loop bug (readsb --net-connector=localhost,30005 ->
# feedback loop -> 100% CPU). Keeps waiting while readsb has not started yet.
_readsb_no_selfloop() {
  local cmd
  cmd="$(readsb_cmdline)"
  [ -n "${cmd}" ] || return 1
  case "${cmd}" in *"localhost,30005,beast_in"*) return 1 ;; *) return 0 ;; esac
}

start_container() {
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  docker run -d --name "${CONTAINER}" \
    -v "$1:/data/options.json:ro" "${IMAGE}" >/dev/null
  # Wait for the config bridge to finish (or the container to exit early).
  local deadline=$((SECONDS + POLL_TIMEOUT))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    if logs | grep -q 'container environment prepared'; then break; fi
    if [ "$(status)" = "exited" ]; then break; fi
    sleep 1
  done
}

assert_running() { if wait_for _running; then ok "container running"; else bad "container not running (status=$(status))"; fi; }
assert_env_contains() {
  if wait_for _env_has "$1" "$2"; then ok "env $1 contains '$2'"; else bad "env $1 missing '$2' (got: '$(env_val "$1")')"; fi
}
# Single-shot negative: the env file is fully written by the time earlier
# positive assertions in the same case have passed, so no wait is needed.
assert_env_not_contains() {
  if _env_has "$1" "$2"; then bad "env $1 unexpectedly contains '$2'"; else ok "env $1 excludes '$2'"; fi
}
assert_log() { if wait_for _log_has "$1"; then ok "log matches /$1/"; else bad "log missing /$1/"; fi; }
# Single-shot negative log check. Unlike assert_log it does NOT poll: use it only
# AFTER a positive assertion has confirmed the relevant startup phase is already in
# the logs, so an absence means "never printed", not merely "not printed yet".
assert_log_not() { if _log_has "$1"; then bad "log unexpectedly matches /$1/"; else ok "log excludes /$1/"; fi; }
# Like assert_log but with an explicit longer timeout, for the qemu-emulated
# feeder: rbfeeder is an armhf binary run under qemu-arm-static, so it can take
# well over the default poll to reach "started" on a loaded CI host.
assert_log_within() {
  local t="$1" pat="$2"
  local deadline=$((SECONDS + t))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    if logs | grep -qE "${pat}"; then
      ok "log matches /${pat}/ (<=${t}s)"
      return
    fi
    sleep 1
  done
  bad "log missing /${pat}/ (waited ${t}s)"
}
# Prove readsb decodes an injected frame. Robust against a slow or emulated
# (qemu/aarch64) start: inject the frame on EVERY poll tick until the ICAO shows
# up in aircraft.json, instead of injecting once up front. If readsb's raw-input
# port (30001) isn't listening yet when we first inject, those frames are lost
# and polling alone never recovers — re-injecting each tick removes that race.
assert_readsb_decodes() {
  local hex="$1" frame="$2"
  local deadline=$((SECONDS + POLL_TIMEOUT))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    docker exec "${CONTAINER}" sh -c \
      "printf '%s\r\n' '${frame}' | socat -t1 - TCP:127.0.0.1:30001" >/dev/null 2>&1 || true
    if _decoded "${hex}"; then
      ok "readsb decoded injected aircraft '${hex}'"
      return
    fi
    sleep 1
  done
  bad "readsb aircraft.json missing '${hex}' after injecting for ${POLL_TIMEOUT}s"
}
assert_symlink() {
  if wait_for _symlink_to "$1" "$2"; then ok "symlink $1 -> $2"; else bad "symlink $1 not -> $2 (got: '$(docker exec "${CONTAINER}" readlink "$1" 2>/dev/null)')"; fi
}
# s6 silently never starts a longrun whose run script isn't executable (a
# non-executable collectd/run override once left graphs1090/collectd down while
# the tmpfs symlink still looked fine). Guard the overridden run scripts.
assert_executable() {
  if wait_for _is_exec "$1"; then ok "executable $1"; else bad "$1 is not executable (s6 won't start it)"; fi
}
assert_readsb_no_selfloop() {
  if wait_for _readsb_no_selfloop; then ok "readsb has no localhost:30005 self-connector"; else bad "readsb self-connects to localhost:30005 (BEASTHOST leaked into readsb: $(readsb_cmdline))"; fi
}

# Point checks (no polling): the container environment is final once the bridge
# has run, which start_container already waited for.
assert_env_unset() {
  if docker exec "${CONTAINER}" test -f "/run/s6/container_environment/$1" 2>/dev/null; then
    bad "env $1 is set but should be unset"
  else
    ok "env $1 unset"
  fi
}
# Absence-in-log check: call only after the relevant services have had time to
# start (i.e. after the service-started assertions in the same case).
assert_no_log() {
  if logs | grep -qE "$1"; then bad "log unexpectedly matches /$1/"; else ok "log clean of /$1/"; fi
}

# NB: the Python unit tests (aviation_feeder_mqtt) run in their own CI job via
# uv/pytest (.github/workflows/tests.yaml -> "Unit tests"), not here — this
# script is the container-level e2e harness only.

if [ "${SKIP_BUILD:-}" != "1" ]; then
  section "Building ${IMAGE}"
  docker build --build-arg BUILD_VERSION=e2e -t "${IMAGE}" "${ADDON_DIR}"
fi

case_default() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names default
  section "CASE default — all feeders off must NOT crash s6 init"
  start_container "${HERE}/fixtures/default.json"
  assert_running
  assert_log 'container environment prepared'
  # 02-rbfeeder must skip (not run its upstream script) when RadarBox is off,
  # else it can sleep-infinity on thermal-less hosts and stall s6 init.
  assert_log 'radarbox disabled/unconfigured; skipping setup'
  # HA sensor publisher must idle cleanly when ha_sensors is unset/off.
  assert_log '\[ha-mqtt\] disabled .*idling'
  # tar1090 must default to the baked version (no per-boot GitHub download).
  assert_env_contains UPDATE_TAR1090 false
  # globe_history retention defaults to 7 days (bounds the persisted /data growth).
  assert_env_contains MAX_GLOBE_HISTORY 7
  # ADSBItalia registration is opt-in-by-feeding: with feed_adsbitalia off it must
  # stay unset so the base's 52-adsbitalia-register hook self-noops (no public-IP
  # detection, no POST to adsbitalia.it).
  assert_env_unset ADSBITALIA_REGISTRATION
  # Persistent data must live on /data (survives rebuilds) and the high-frequency
  # collectd RRDs must be RAM-backed with hourly write-back to /data.
  assert_symlink /var/globe_history /data/globe_history
  assert_symlink /var/lib/collectd /data/collectd
  assert_symlink /var/cache/piaware /data/piaware
  assert_symlink /run/collectd /tmp/collectd
  assert_env_contains GRAPHS1090_REDUCE_IO true
  assert_env_contains GRAPHS1090_REDUCE_IO_FLUSH_IVAL 1h
  assert_executable /etc/s6-overlay/s6-rc.d/readsb/run
  assert_executable /etc/s6-overlay/s6-rc.d/collectd/run

  teardown_case
}

case_rtlsdr() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names rtlsdr
  section "CASE rtlsdr + UAT + aggregators (shared + override UUID)"
  start_container "${HERE}/fixtures/rtlsdr-uat.json"
  assert_running
  assert_env_contains READSB_DEVICE_TYPE rtlsdr
  assert_env_contains UUID '01234567-89ab-cdef'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,localhost,30978,uat_in'
  # Assert every aggregator connector so a host/port typo in 00-haos-options
  # ships loudly, not silently. Distinctive host+port per aggregator; shared vs
  # override UUID checked on adsblol/airplaneslive; mlat lines and the no-mlat
  # aggregator (avdelphi) checked too.
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,in.adsb.lol,30004,beast_reduce_plus_out,uuid=01234567-89ab-cdef'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,feed.adsb.fi,30004,beast_reduce_plus_out'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,feed.airplanes.live,30004,beast_reduce_plus_out,uuid=aaaaaaaa-bbbb'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,feed.planespotters.net,30004,beast_reduce_plus_out'
  assert_env_contains ULTRAFEEDER_CONFIG 'mlat,mlat.planespotters.net,31090'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,feed.theairtraffic.com,30004,beast_reduce_plus_out'
  assert_env_contains ULTRAFEEDER_CONFIG 'mlat,feed.theairtraffic.com,31090'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,data.avdelphi.com,24999,beast_reduce_plus_out'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,dati.flyitalyadsb.com,4905,beast_reduce_plus_out'
  assert_env_contains ULTRAFEEDER_CONFIG 'mlat,dati.flyitalyadsb.com,30100'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,feed.adsbitalia.it,31108,beast_reduce_plus_out'
  assert_env_contains ULTRAFEEDER_CONFIG 'mlat,mlat.adsbitalia.it,41113'
  # adsbitalia_name rides the MLAT connector as name= (overrides MLAT_USER).
  assert_env_contains ULTRAFEEDER_CONFIG 'mlat,mlat.adsbitalia.it,41113,uuid=01234567-89ab-cdef-0123-456789abcdef,name=Motown_ADSB'
  # Feeding ADSBItalia auto-enables its registration, and the registration name
  # honors adsbitalia_name via ADSBITALIA_NAME (the patched hook prefers it).
  assert_env_contains ADSBITALIA_REGISTRATION 'true'
  assert_env_contains ADSBITALIA_NAME 'Motown_ADSB'
  if docker exec "${CONTAINER}" grep -q 'FEEDER_NAME="${ADSBITALIA_NAME:-${MLAT_USER:-$FEEDER_ID}}"' /etc/s6-overlay/startup.d/52-adsbitalia-register 2>/dev/null; then
    ok "52-adsbitalia-register patched to prefer ADSBITALIA_NAME"
  else
    bad "52-adsbitalia-register not patched (registration name would fall back to MLAT_USER only)"
  fi
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,feed1.adsbexchange.com,30004,beast_reduce_plus_out'
  assert_env_contains ULTRAFEEDER_CONFIG 'mlat,feed.adsbexchange.com,31090'
  # adsb.one / HpRadar: non-uniform ports (64004/64006, 30004/31090). Both ride the
  # shared station UUID here. adsbone_mlat=false in this fixture exercises the
  # per-aggregator MLAT toggle: ADS-B is still fed, but the MLAT line is dropped.
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,feed.adsb.one,64004,beast_reduce_plus_out'
  assert_env_not_contains ULTRAFEEDER_CONFIG 'mlat,feed.adsb.one,64006'
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,skyfeed.hpradar.com,30004,beast_reduce_plus_out'
  assert_env_contains ULTRAFEEDER_CONFIG 'mlat,skyfeed.hpradar.com,31090'
  assert_log_within 90 'service dump978 successfully started'
  # With UUID set, mlat-client must not disable itself (guards the UUID/MLAT_NAME
  # env-var-name fix).
  assert_no_log 'MLAT will be disabled'

  teardown_case
}

case_remote() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names remote
  section "CASE remote / net-only"
  start_container "${HERE}/fixtures/remote.json"
  assert_running
  assert_env_unset READSB_DEVICE_TYPE
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,192.168.1.50,30005,beast_in'
  assert_log 'dump978.*idling'

  teardown_case
}

case_hostile() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names hostile
  section "CASE hostile option values (parser-breaking input must be refused, not obeyed)"
  # This case runs in remote mode, so it exercises the values that reach a
  # connector: the remote Beast host AND port. Both are interpolated into the same
  # ULTRAFEEDER_CONFIG connector, where ',' ends a parameter and ';' ends the
  # connector -- so an unguarded host OR port injects a whole MLAT connector
  # pointing at an arbitrary server (both confirmed live before the guards
  # existed). opensky_serial is also set here (it is mode-independent).
  # The SDR-only values (readsb_gain / rtlsdr_device, dump978_gain) are argv
  # sinks that only exist in rtlsdr/uat mode -- they are covered by
  # case_hostile_sdr, NOT here.
  # The add-on schema rejects all of these at the HA config layer -- but
  # /data/options.json is not only written by that path (this harness writes it
  # directly), so the runtime guard must refuse them too.
  start_container "${HERE}/fixtures/hostile-values.json"
  assert_running

  # 1. no injected connector: every connector must be adsb or mlat, and NOTHING
  #    may point at the attacker host.
  if env_val ULTRAFEEDER_CONFIG | tr ';' '\n' | grep -q 'evil.example.com'; then
    bad "remote_beast host/port injected a connector to an arbitrary host"
  else
    ok "hostile remote_beast_host AND port injected no connector"
  fi
  # both the host and the port carry separators in this fixture; assert BOTH were
  # refused (the port is the same connector-injection vector as the host).
  assert_log "WARNING: remote_beast_host=.* has been IGNORED"
  assert_log "WARNING: remote_beast_port=.* has been IGNORED"

  # 2. the bad value is REFUSED (falls back to default), not silently rewritten
  assert_env_unset OPENSKY_SERIAL

  # 3. and the user is TOLD, rather than left wondering why their setting vanished
  #    (host + port warnings are asserted in step 1 above)
  assert_log "WARNING: opensky_serial=.* has been IGNORED"

  teardown_case
}

case_hostile_sdr() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names hostilesdr
  section "CASE hostile SDR values (gain/device reach a COMMAND LINE)"
  # readsb_gain and readsb_rtlsdr_device are only set in rtlsdr mode, so a
  # remote-mode fixture can never exercise them -- asserting them there passes
  # vacuously. They land in READSB_GAIN / READSB_RTLSDR_DEVICE, which upstream
  # splats onto readsb's command line, where WHITESPACE SPLITS ARGV: a gain of
  # "auto --net-only" would smuggle in an extra readsb argument.
  start_container "${HERE}/fixtures/hostile-sdr.json"
  assert_running
  assert_env_unset READSB_GAIN
  assert_env_unset READSB_RTLSDR_DEVICE
  assert_env_unset DUMP978_SDR_GAIN
  assert_log "WARNING: readsb_gain=.* has been IGNORED"
  assert_log "WARNING: readsb_rtlsdr_device=.* has been IGNORED"
  assert_log "WARNING: dump978_gain=.* has been IGNORED"

  teardown_case
}

case_remote_bad_port() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names remotebadport
  section "CASE remote_beast_port numerically out of range (the range-check branch)"
  # The hostile-values fixture's port fails the digits-only RE_PORT, so it exercises
  # checked()'s regex-rejection path. A digits-only-but-out-of-range port (99999)
  # instead reaches the SEPARATE 1-65535 range check -- a distinct branch of the
  # guard that would otherwise have no coverage.
  start_container "${HERE}/fixtures/remote-bad-port.json"
  assert_running
  # falls back to the default :30005 connector, not the injected/garbage port
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,h.example.com,30005,beast_in'
  assert_env_not_contains ULTRAFEEDER_CONFIG '99999'
  assert_log "WARNING: remote_beast_port='99999' is out of range"

  teardown_case
}

case_uat() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names uat
  section "CASE uat — 978-only (readsb net-only, dump978 the sole local SDR)"
  start_container "${HERE}/fixtures/uat-978.json"
  assert_running
  assert_env_contains RECEIVER_MODE uat
  # UAT is forced on in uat-only mode even though the fixture never sets enable_uat.
  assert_env_contains ENABLE_UAT true
  # readsb runs net-only -- no local 1090 SDR is bound.
  assert_env_unset READSB_DEVICE_TYPE
  # the 978 stick is claimed by dump978.
  assert_env_contains DUMP978_RTLSDR_DEVICE 00000978
  # readsb's only input is the local UAT stream -- no remote beast_in.
  assert_env_contains ULTRAFEEDER_CONFIG 'adsb,localhost,30978,uat_in'
  assert_env_not_contains ULTRAFEEDER_CONFIG 'beast_in'
  # dump978 must actually run here (contrast remote mode above, where it idles).
  assert_log_within 90 'service dump978 successfully started'

  teardown_case
}

case_decoder() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names decoder
  section "CASE decoder — readsb (dump1090 equivalent) decodes injected ADS-B"
  start_container "${HERE}/fixtures/decoder.json"
  assert_running
  # Canonical DF17 identification message: ICAO 4840D6, callsign KLM1023.
  assert_readsb_decodes '4840d6' '*8D4840D6202CC371C32CE0576098;'

  teardown_case
}

case_unconfig() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names unconfig
  section "CASE feeders enabled but unconfigured — must idle cleanly, not crash-loop"
  # The gates are `[enable != true] || [ -z "$KEY" ]`; the all-off and
  # all-configured fixtures never exercise the empty-key half. This asserts a
  # feeder enabled WITHOUT its credential idles cleanly (the exact path the
  # gating exists for) rather than restart-looping or failing s6 init.
  start_container "${HERE}/fixtures/unconfigured.json"
  assert_running
  # First idle assert uses a longer window: the unconfigured case now starts a
  # dozen gated feeders, so on a loaded host the first idle message can take >30s.
  # Once it appears the rest are already emitted, so they can use the short assert.
  assert_log_within 90 '\[fr24feed\] disabled .*idling'
  assert_log '\[pfclient\] disabled .*idling'
  assert_log '\[opensky-feeder\] disabled .*idling'
  assert_log '\[adsbhubclient\] disabled .*idling'
  assert_log '\[rbfeeder\] disabled .*idling'
  assert_log '\[pw-feeder\] disabled .*idling'
  assert_log '\[planewatch-mlat\] disabled.*idling'
  assert_log '\[radarvirtuel\] disabled .*idling'
  assert_log '\[radarvirtuel-mlat\] disabled.*idling'
  assert_log '\[sdrmap\] disabled .*idling'
  assert_log '\[sdrmap-stunnel\] disabled.*idling'
  assert_log '\[sdrmap-mlat\] disabled.*idling'
  assert_log '\[uk1090\] disabled .*idling'
  # The rbfeeder config oneshot must also skip when keyless (matches the longrun
  # gate; otherwise the real oneshot can sleep-infinity on thermal-less hosts).
  assert_log 'radarbox disabled/unconfigured; skipping setup'
  # The config oneshots must skip cleanly too (they hard-exit on missing creds,
  # which would fail s6 init if not gated).
  assert_log '\[01-fr24feed\] fr24 disabled/unconfigured; skipping setup'
  assert_log '\[01-opensky-network\] opensky disabled/unconfigured; skipping setup'

  teardown_case
}

case_allfeeders() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names allfeeders
  section "CASE all feeders enabled — every feeder service starts"
  start_container "${HERE}/fixtures/all-feeders.json"
  assert_running
  assert_log_within 90 'service dump978 successfully started'
  assert_log_within 90 'service piaware successfully started'
  assert_log_within 90 'service fr24feed successfully started'
  assert_log_within 90 'service fr24uat-feed successfully started'
  assert_log_within 90 'service pfclient successfully started'
  # fr24feed's binary demands TZ=GMT, and upstream enforces it by clobbering the
  # SHARED s6 container-environment TZ file -- forcing tar1090/graphs/logs/MQTT
  # onto GMT too. We patch that global write out (Dockerfile) and give GMT to
  # fr24's OWN processes instead. So with fr24 enabled AND a tz set (this fixture:
  # America/Detroit), the shared TZ file must NOT have been clobbered to GMT.
  # Before the fix this env file was literally "GMT". (This env file -- not a
  # per-process TZ -- is the thing fr24's clobber overwrote and every with-contenv
  # service reads.)
  assert_env_contains TZ 'America/Detroit'
  assert_env_not_contains TZ 'GMT'
  # We also strip the upstream oneshot's now-false warning that TZ "is ignored ...
  # because fr24feed requires ... GMT" -- we honour the container tz and scope GMT
  # to fr24's own processes. fr24feed has already started above, so had the warning
  # been going to print (its own if-block in 01-fr24feed-real), it would be logged
  # by now; a single-shot negative is therefore sound here.
  assert_log_not 'Setting timezone via TZ is ignored'
  # GMT is pinned to the fr24feed BINARY alone: /usr/local/bin/fr24feed is a wrapper
  # that flips TZ=GMT before exec'ing /usr/bin/fr24feed, so the run scripts and their
  # s6wrap logger stay on the container tz and stamp fr24's log lines in LOCAL time
  # rather than 4h-ahead GMT. Guard both halves of that mechanism.
  if docker exec "${CONTAINER}" grep -q 'TZ=GMT' /usr/local/bin/fr24feed 2>/dev/null; then
    ok "fr24feed wrapper pins TZ=GMT to the binary alone"
  else
    bad "fr24feed wrapper missing TZ=GMT (binary tz unpinned: GMT errors, or logs revert to GMT)"
  fi
  # Match the forcing CODE ('env TZ=GMT ...'), not the word "TZ=GMT" that the run
  # scripts' own comments legitimately mention.
  if docker exec "${CONTAINER}" grep -q 'env TZ=GMT' \
    /etc/s6-overlay/s6-rc.d/fr24feed/run /etc/s6-overlay/s6-rc.d/fr24uat-feed/run 2>/dev/null; then
    bad "fr24 run script still forces TZ=GMT -- s6wrap would stamp fr24 logs in GMT"
  else
    ok "fr24 run scripts leave s6wrap on the container tz (fr24 logs stamped local)"
  fi
  assert_log_within 90 'service opensky-feeder successfully started'
  assert_log_within 90 'service adsbhubclient successfully started'
  assert_log_within 90 'service rbfeeder successfully started'
  assert_log_within 90 'service pw-feeder successfully started'
  assert_log_within 90 'service planewatch-mlat successfully started'
  assert_log_within 90 'service radarvirtuel successfully started'
  assert_log_within 90 'service radarvirtuel-mlat successfully started'
  # The station identity must live in OPTIONS, not in /data: RV_STATION_UID takes
  # priority over the persisted /data/station_uid.txt in the feeder's entrypoint,
  # so pinning it means a wiped /data (or a new add-on slug) still comes back as
  # the SAME RadarVirtuel station instead of silently re-registering a new one.
  assert_env_contains RV_STATION_UID 'E2E-PINNED-UID-0001'
  # Per-aggregator station name: the name an aggregator DISPLAYS is the MLAT
  # --user, which normally comes from site_name for every network alike. A
  # receiver registered under a different name on one network cannot be expressed
  # by a single site_name -- so <aggregator>_name overrides it for that connector
  # only. Assert BOTH halves: the override reaches adsb.fi's mlat connector, and
  # an aggregator WITHOUT an override (adsb.lol) still falls back to site_name.
  assert_env_contains ULTRAFEEDER_CONFIG 'mlat,feed.adsb.fi,31090'
  # The fixture's name deliberately contains BOTH ULTRAFEEDER_CONFIG separators
  # (',' between a connector's params, ';' between connectors). Unsanitised, it
  # does not merely render oddly -- it CHANGES THE PARSE: the comma invents an
  # extra param and the semicolon STARTS A NEW CONNECTOR, so a station name can
  # inject arbitrary feeder config. Both must arrive neutralised.
  assert_env_contains ULTRAFEEDER_CONFIG 'name=E2E Name_ With_ Sep'
  if env_val ULTRAFEEDER_CONFIG | tr ';' '\n' | grep -qvE '^(adsb|mlat),'; then
    bad "ULTRAFEEDER_CONFIG has a connector that is neither adsb nor mlat (injection)"
  else
    ok "no injected connector: ',' and ';' in a station name are neutralised"
  fi
  # This must NOT pass vacuously: assert adsb.lol actually HAS an mlat connector
  # first, then that the connector carries no name=. Without the first half, the
  # check silently succeeds the moment adsb.lol stops emitting mlat at all.
  if ! env_val ULTRAFEEDER_CONFIG | grep -qE 'mlat,in\.adsb\.lol,[0-9]+'; then
    bad "adsb.lol has no mlat connector -- the no-leak assertion below would be vacuous"
  elif env_val ULTRAFEEDER_CONFIG | grep -qE 'mlat,in\.adsb\.lol,[0-9]+[^;]*name='; then
    bad "adsb.lol got a name= it was never given (override leaked across aggregators)"
  else
    ok "aggregator without a name override falls back to site_name (and does emit mlat)"
  fi
  # A station name IS the MLAT --user, so it can only take effect through an MLAT
  # connector. Set one on an aggregator whose MLAT is off (or which has none at
  # all, e.g. an ADS-B-only network) and it silently does nothing -- the exact
  # failure this option exists to remove. It must WARN instead.
  assert_log 'WARNING: hpradar_name is set, but feed_hpradar has no active MLAT'
  # pw-feeder is a native multi-arch Go binary (glibc-only); assert it was staged.
  if docker exec "${CONTAINER}" test -x /usr/local/sbin/pw-feeder 2>/dev/null; then
    ok "pw-feeder binary present + executable"
  else
    bad "pw-feeder binary missing"
  fi
  # RadarVirtuel is pure Python; assert the feeder + its requests dep are staged.
  if docker exec "${CONTAINER}" test -f /docker-entrypoint.py 2>/dev/null; then
    ok "radarvirtuel entrypoint staged"
  else
    bad "radarvirtuel /docker-entrypoint.py missing"
  fi
  if docker exec "${CONTAINER}" python3 -c 'import requests' 2>/dev/null; then
    ok "python3-requests available for radarvirtuel feeder"
  else
    bad "python3-requests missing (feeder would exit)"
  fi
  assert_log_within 90 'service sdrmap successfully started'
  assert_log_within 90 'service sdrmap-stunnel successfully started'
  assert_log_within 90 'service sdrmap-mlat successfully started'
  # sdrmap: shell feeder staged + stunnel apt-installed (with its OpenSSL libs).
  if docker exec "${CONTAINER}" test -x /usr/lib/sdrmapfeeder/sdrmapfeeder.sh 2>/dev/null; then
    ok "sdrmapfeeder.sh staged + executable"
  else
    bad "sdrmapfeeder.sh missing"
  fi
  if docker exec "${CONTAINER}" sh -c 'command -v stunnel >/dev/null && ldd "$(command -v stunnel)" | grep -q libssl' 2>/dev/null; then
    ok "stunnel present + linked to libssl"
  else
    bad "stunnel missing or unlinked (sdrmap MLAT would fail)"
  fi
  assert_log_within 90 'service uk1090 successfully started'
  # 1090MHz UK: the radar binary (glibc-only) must be staged + executable.
  if docker exec "${CONTAINER}" test -x /usr/sbin/radar 2>/dev/null; then
    ok "radar (1090MHz UK) binary present + executable"
  else
    bad "radar binary missing"
  fi
  # rbfeeder's internal intern_port (default 32008) must not collide with readsb's
  # SBS-input block (32006-32009); 02-rbfeeder pins it to 32208. Guard the readsb
  # crash-loop this caused (readsb can't bind 32008 -> "Address already in use").
  assert_no_log '32008.*Address already in use'
  # rbfeeder MLAT autostart hinges on a correctly-generated /etc/rbfeeder.ini --
  # regression guards for the alt-suffix bug that silently disabled MLAT (rbfeeder
  # skips MLAT unless alt is a bare number), plus the intern_port move and the
  # listen-mode mlat_cmd that let the community mlat-client actually launch.
  if docker exec "${CONTAINER}" sh -c 'grep -qE "^alt=-?[0-9]+$" /etc/rbfeeder.ini' 2>/dev/null; then
    ok "rbfeeder.ini alt is a bare number"
  else
    bad "rbfeeder.ini alt not bare (MLAT would silently not start)"
  fi
  if docker exec "${CONTAINER}" sh -c 'grep -qx "intern_port=32208" /etc/rbfeeder.ini' 2>/dev/null; then
    ok "rbfeeder.ini intern_port=32208"
  else
    bad "rbfeeder.ini intern_port not pinned off 32008"
  fi
  if docker exec "${CONTAINER}" sh -c 'grep -qE "^mlat_cmd=/usr/local/bin/rbfeeder-mlat --results beast,listen,30107$" /etc/rbfeeder.ini' 2>/dev/null; then
    ok "rbfeeder.ini mlat_cmd is listen-mode on 30107 via rbfeeder-mlat shim"
  else
    bad "rbfeeder.ini mlat_cmd not the listen-mode fix"
  fi
  # The rbfeeder-mlat shim (re-tags client output as [mlat]) must exist and be executable.
  if docker exec "${CONTAINER}" test -x /usr/local/bin/rbfeeder-mlat 2>/dev/null; then
    ok "rbfeeder-mlat shim is executable"
  else
    bad "rbfeeder-mlat shim missing or not executable"
  fi
  # The bridge strips a trailing unit from the altitude (all-feeders.json sets "250m").
  if [ "$(env_val ALT)" = "250" ]; then ok "bridge stripped ALT unit -> bare '250'"; else bad "bridge did not strip ALT unit (got: '$(env_val ALT)')"; fi
  # The feeders export BEASTHOST=localhost in their own wrappers to reach readsb,
  # but readsb itself must NOT self-connect to its own 30005 output — that is the
  # Beast feedback loop that pegged a CPU core. This is the real regression guard.
  assert_readsb_no_selfloop
  # And piaware must still be configured as a relay pointing at readsb, proving the
  # scoped BEASTHOST reached it (without it, piaware would fall back to rtlsdr and
  # start its own competing decoder).
  if docker exec "${CONTAINER}" grep -q 'receiver-type "relay"' /etc/piaware.conf 2>/dev/null; then
    ok "piaware configured as relay (scoped BEASTHOST reached it)"
  else
    bad "piaware not configured as relay (scoped BEASTHOST did not reach 01-piaware)"
  fi
  # After the feeders have started, no copied binary should be missing a shared
  # library (guards the class of bug where a binary is copied without its runtime
  # libs, e.g. dump978-fa needing libboost).
  assert_no_log 'error while loading shared libraries'

  teardown_case
}

case_hasensors() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names hasensors
  section "CASE ha_sensors=on — publishes discovery + state to a live broker (inherited HA location)"
  # Spin up a throwaway Mosquitto broker + a mock HA Core API on a private network,
  # point the add-on at them, and assert the paho publisher connects and publishes
  # HA discovery + state + availability. mqtt.json leaves lat/long on the
  # HOMEASSISTANT_* sentinels, so the nearby device is discovered/published only if
  # the inherited HA location (bridge /config fetch -> LAT/LONG env -> the
  # publisher's _coord fallback) reaches the publisher — this covers the
  # blank-location path too. Skips cleanly if the broker image can't be pulled.
  if docker pull -q eclipse-mosquitto:2 >/dev/null 2>&1; then
    docker network create "${MQTT_NET}" >/dev/null 2>&1 || true
    docker rm -f "${MQTT_BROKER}" "${API_MOCK}" >/dev/null 2>&1 || true
    docker run -d --name "${MQTT_BROKER}" --network "${MQTT_NET}" eclipse-mosquitto:2 \
      sh -c 'printf "listener 1883\nallow_anonymous true\n" >/mosquitto/config/mosquitto.conf && exec mosquitto -c /mosquitto/config/mosquitto.conf' >/dev/null
    # Mock HA Core API: GET /core/api/config feeds the bridge's location fetch.
    docker run -d --name "${API_MOCK}" --network "${MQTT_NET}" --entrypoint python3 "${IMAGE}" -c '
import http.server as h, json
class H(h.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith("/config"):
            b = json.dumps({"latitude": 42.3601, "longitude": -71.0589, "elevation": 43}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a):
        pass
h.HTTPServer(("0.0.0.0", 8099), H).serve_forever()
' >/dev/null
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
    # The fixture hard-codes mqtt_host as the broker's container name. Each case
    # now runs its own uniquely-named broker (parallel isolation), so rewrite
    # mqtt_host to this case's broker before mounting. (The bind-mounted inode
    # survives the rm below — Linux keeps it alive for the mount.)
    local optfile
    optfile="$(mktemp)"
    sed "s/\"mqtt_host\": \"aviation_feeder_e2e_mqtt\"/\"mqtt_host\": \"${MQTT_BROKER}\"/" \
      "${HERE}/fixtures/mqtt.json" >"${optfile}"
    docker run -d --name "${CONTAINER}" --network "${MQTT_NET}" \
      -e SUPERVISOR_TOKEN=test \
      -e SUPERVISOR_CORE_API="http://${API_MOCK}:8099/core/api" \
      -v "${optfile}:/data/options.json:ro" "${IMAGE}" >/dev/null
    rm -f "${optfile}"
    if wait_for _log_has 'MQTT connected to'; then
      ok "mqtt publisher connected to broker"
    else
      bad "mqtt publisher did not connect to broker"
    fi
    # Capture broker traffic in short windows until the feeder-health state (which
    # waits on readsb's slower stats.json) shows up; retained discovery/availability
    # and the frequent nearby state are present in every window.
    CAP=""
    mq_deadline=$((SECONDS + 50))
    while [ "${SECONDS}" -lt "${mq_deadline}" ]; do
      CAP="$(docker exec "${MQTT_BROKER}" timeout 4 mosquitto_sub -t '#' -v 2>/dev/null || true)"
      case "${CAP}" in *"aviation_feeder/aircraft_total/state"*) break ;; *) ;; esac
      sleep 1
    done
    case "${CAP}" in
      *"homeassistant/sensor/aviation_feeder/aircraft_total/config"*) ok "mqtt feeder-health discovery published" ;;
      *) bad "mqtt feeder-health discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder/aircraft_total/state"*) ok "mqtt feeder-health state published" ;;
      *) bad "mqtt feeder-health state missing" ;;
    esac
    # MQTT broker-link diagnostic sensor (main device): uptime + reconnect count.
    case "${CAP}" in
      *"aviation_feeder/mqtt_reconnects/config {"*) ok "mqtt broker-link discovery published" ;;
      *) bad "mqtt broker-link discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder/mqtt_reconnects/state"*) ok "mqtt broker-link state published" ;;
      *) bad "mqtt broker-link state missing" ;;
    esac
    # SDR health device: this fixture is remote mode (no local SDR), so the SDR
    # device must be ABSENT — its config is published retained-empty (a removal),
    # never with a JSON body. Requiring "config {" (a real payload) exercises the
    # receiver_mode guard instead of just matching the topic string.
    case "${CAP}" in
      *"aviation_feeder/sdr_gain_db/config {"*) bad "SDR sensors published in remote mode (should be absent)" ;;
      *) ok "SDR device correctly absent in remote mode" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder/availability online"*) ok "mqtt availability online published" ;;
      *) bad "mqtt availability online missing" ;;
    esac
    # Planes-near-me device (aircraft_in_range is 0 with no live feed, but still
    # discovered + published).
    case "${CAP}" in
      *"homeassistant/sensor/aviation_feeder_nearby/aircraft_in_range/config"*) ok "mqtt nearby discovery published" ;;
      *) bad "mqtt nearby discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder/nearby/aircraft_in_range/state"*) ok "mqtt nearby state published" ;;
      *) bad "mqtt nearby state missing" ;;
    esac
    # Emergency-squawk safety binary_sensor (main device): discovered with a real
    # payload, and state published. The test feed has no 7500/7600/7700, so it
    # must be "off" (proves the whole read->compute->publish path end to end).
    case "${CAP}" in
      *"homeassistant/binary_sensor/aviation_feeder/emergency_squawk/config {"*) ok "mqtt emergency-squawk discovery published" ;;
      *) bad "mqtt emergency-squawk discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder/emergency_squawk/state off"*) ok "mqtt emergency-squawk state published (off)" ;;
      *) bad "mqtt emergency-squawk state missing" ;;
    esac
    # Unique-aircraft-today counter (main device): discovered with a real payload
    # and its state published (0 or more, whatever the test feed has seen today).
    case "${CAP}" in
      *"homeassistant/sensor/aviation_feeder/unique_today/config {"*) ok "mqtt unique-today discovery published" ;;
      *) bad "mqtt unique-today discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder/unique_today/state"*) ok "mqtt unique-today state published" ;;
      *) bad "mqtt unique-today state missing" ;;
    esac
    # Per-feeder status: piaware is enabled (needs no key) so its process runs.
    case "${CAP}" in
      *"homeassistant/binary_sensor/aviation_feeder_feeders/piaware/config"*) ok "mqtt feeder-status discovery published" ;;
      *) bad "mqtt feeder-status discovery missing" ;;
    esac
    # Per-feeder throughput: piaware is a kernel-TCP feeder, so it gets byte sensors.
    # "config {" requires a real payload, not just the topic.
    case "${CAP}" in
      *"aviation_feeder_feeders/piaware_bytes_sent/config {"*) ok "mqtt feeder-throughput discovery published" ;;
      *) bad "mqtt feeder-throughput discovery missing" ;;
    esac
    # primary per-second rate sensor (send/receive B/s).
    case "${CAP}" in
      *"aviation_feeder_feeders/piaware_bytes_sent_rate/config {"*) ok "mqtt feeder rate sensor published" ;;
      *) bad "mqtt feeder rate sensor missing" ;;
    esac
    # piaware self-report binary_sensors (MLAT/Radio from status.json).
    case "${CAP}" in
      *"binary_sensor/aviation_feeder_feeders/piaware_mlat_ok/config {"*) ok "mqtt piaware MLAT binary published" ;;
      *) bad "mqtt piaware MLAT binary missing" ;;
    esac
    # fr24 feeds UDP (no byte counter) -> a Messages sensor, and NO byte sensor.
    case "${CAP}" in
      *"aviation_feeder_feeders/fr24_messages/config {"*) ok "mqtt fr24 messages discovery published" ;;
      *) bad "mqtt fr24 messages discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder_feeders/fr24_bytes_sent/config {"*) bad "fr24 byte sensor present (should be dropped for UDP feed)" ;;
      *) ok "fr24 byte sensor correctly absent" ;;
    esac
    # fr24 reports the aggregator's own aircraft view (feed_num_ac_*_tracked).
    case "${CAP}" in
      *"aviation_feeder_feeders/fr24_portal_aircraft/config {"*) ok "mqtt fr24 portal-aircraft discovery published" ;;
      *) bad "mqtt fr24 portal-aircraft discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder_feeders/fr24_portal_aircraft_adsb/config {"*) ok "mqtt fr24 portal ADS-B discovery published" ;;
      *) bad "mqtt fr24 portal ADS-B discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder_feeders/fr24_portal_aircraft_other/config {"*) ok "mqtt fr24 portal non-ADS-B discovery published" ;;
      *) bad "mqtt fr24 portal non-ADS-B discovery missing" ;;
    esac
    # ...but not for a community aggregator, which has no client to report it.
    case "${CAP}" in
      *"aviation_feeder_feeders/adsblol_portal_aircraft/config {"*) bad "portal-aircraft sensor present for a community aggregator (no client to report it)" ;;
      *) ok "portal-aircraft correctly absent for community aggregator" ;;
    esac
    # PlaneFinder reports its own per-second decode rates (not aircraft counts).
    case "${CAP}" in
      *"aviation_feeder_feeders/planefinder_portal_message_rate/config {"*) ok "mqtt planefinder portal message-rate discovery published" ;;
      *) bad "mqtt planefinder portal message-rate discovery missing" ;;
    esac
    case "${CAP}" in
      *"aviation_feeder_feeders/planefinder_portal_receive_rate/config {"*) ok "mqtt planefinder portal receive-rate discovery published" ;;
      *) bad "mqtt planefinder portal receive-rate discovery missing" ;;
    esac
    # Network-ingest rates on the main device (readsb stats.json last1min.remote).
    case "${CAP}" in
      *"aviation_feeder/remote_message_rate/config {"*) ok "mqtt network message-rate discovery published" ;;
      *) bad "mqtt network message-rate discovery missing" ;;
    esac
    # community aggregators no longer get an (unreliable) per-connector byte sensor.
    case "${CAP}" in
      *"aviation_feeder_feeders/adsblol_bytes_sent/config {"*) bad "aggregator byte sensor present (should be dropped)" ;;
      *) ok "aggregator byte sensor correctly absent" ;;
    esac
    # Per-feeder MLAT sync discovery for a MLAT-capable feeder (adsb.lol, enabled
    # in mqtt.json). piaware/fr24 are NOT MLAT-capable via mlat-client, so they get
    # no MLAT sensors — adsb.lol is the deterministic positive here.
    case "${CAP}" in
      *"aviation_feeder_feeders/adsblol_mlat_peers/config {"*) ok "mqtt feeder-MLAT discovery published" ;;
      *) bad "mqtt feeder-MLAT discovery missing" ;;
    esac
    # Per-feeder uptime sensor (universal — every enabled feeder).
    case "${CAP}" in
      *"aviation_feeder_feeders/piaware_uptime/config {"*) ok "mqtt feeder-uptime discovery published" ;;
      *) bad "mqtt feeder-uptime discovery missing" ;;
    esac
    # Per-feeder app-report attributes wiring: piaware's connectivity sensor config
    # must carry its json_attributes_topic (live self-report data isn't hermetic,
    # so only the discovery wiring is asserted).
    case "${CAP}" in
      *"aviation_feeder/feeders/piaware/attributes"*) ok "mqtt feeder attributes topic wired" ;;
      *) bad "mqtt feeder attributes topic missing" ;;
    esac
    # piaware is a "conn"-mode feeder: its status now reflects *actually feeding*
    # (an ESTABLISHED connection to FlightAware), not merely "process running", so
    # its on/off value depends on real connectivity and isn't hermetic here — just
    # assert the state is published. The feeding-vs-running logic itself is covered
    # deterministically by tests/unit/test_feeders.py.
    case "${CAP}" in
      *"aviation_feeder/feeders/piaware/state on"* | *"aviation_feeder/feeders/piaware/state off"*) ok "mqtt feeder-status state (piaware published)" ;;
      *) bad "mqtt feeder-status state missing" ;;
    esac
    # A feeder enabled WITHOUT its key idles -> its status must publish "off". This
    # guards the s6-supervise false-positive fix through the full MQTT path (fr24 is
    # enabled with no fr24_key, so fr24feed is sleep-infinity, not the real binary).
    case "${CAP}" in
      *"aviation_feeder/feeders/fr24/state off"*) ok "mqtt feeder-status state (idled fr24 off)" ;;
      *) bad "mqtt feeder-status idled-fr24 not off" ;;
    esac
    docker rm -f "${MQTT_BROKER}" "${API_MOCK}" >/dev/null 2>&1 || true
    docker network rm "${MQTT_NET}" >/dev/null 2>&1 || true
  else
    printf '  SKIP: eclipse-mosquitto image unavailable (offline?)\n'
  fi

  teardown_case
}

case_autoloc() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names autoloc
  section "CASE auto lat/long/alt from Home Assistant location"
  # The fixture uses the HOMEASSISTANT_* sentinels (the option defaults); the bridge
  # maps them to the location fetched from HA's core API (GET /core/api/config).
  # Mock that endpoint and point the bridge at it via SUPERVISOR_CORE_API +
  # SUPERVISOR_TOKEN.
  docker network create "${MQTT_NET}" >/dev/null 2>&1 || true
  docker rm -f "${API_MOCK}" >/dev/null 2>&1 || true
  docker run -d --name "${API_MOCK}" --network "${MQTT_NET}" --entrypoint python3 "${IMAGE}" -c '
import http.server as h, json
class H(h.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith("/config"):
            b = json.dumps({"latitude": 42.3601, "longitude": -71.0589, "elevation": 43}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b)
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *a):
        pass
h.HTTPServer(("0.0.0.0", 8099), H).serve_forever()
' >/dev/null
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  docker run -d --name "${CONTAINER}" --network "${MQTT_NET}" \
    -e SUPERVISOR_TOKEN=test \
    -e SUPERVISOR_CORE_API="http://${API_MOCK}:8099/core/api" \
    -v "${HERE}/fixtures/auto-location.json:/data/options.json:ro" "${IMAGE}" >/dev/null
  # Wait for the config bridge to finish.
  deadline=$((SECONDS + POLL_TIMEOUT))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    logs | grep -q 'container environment prepared' && break
    sleep 1
  done
  assert_env_contains LAT '42.3601'
  assert_env_contains LONG '-71.0589'
  # Bare metres, NO unit suffix -- a trailing "m" silently breaks rbfeeder's MLAT
  # autostart and makes openskyd log "Garbage after number ignored". Exact match so
  # a returning "43m" fails.
  if [ "$(env_val ALT)" = "43" ]; then ok "env ALT is bare metres '43'"; else bad "env ALT not bare '43' (got: '$(env_val ALT)')"; fi
  assert_log 'inherited station location from Home Assistant'
  docker rm -f "${API_MOCK}" >/dev/null 2>&1 || true
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  docker network rm "${MQTT_NET}" >/dev/null 2>&1 || true

  teardown_case
}

case_tmpfs() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names tmpfs
  section "CASE readsb output redirected to tmpfs (SD-card wear mitigation)"
  # Supervisor sets tmpfs:true -> /tmp is tmpfs; a plain `docker run` doesn't, so
  # simulate it with --tmpfs. Assert /run/readsb is redirected into /tmp, resolves
  # to a tmpfs mount, and readsb still writes its JSON through the symlink.
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  docker run -d --name "${CONTAINER}" --tmpfs /tmp:exec \
    -v "${HERE}/fixtures/remote.json:/data/options.json:ro" "${IMAGE}" >/dev/null
  deadline=$((SECONDS + POLL_TIMEOUT))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    logs | grep -q 'container environment prepared' && break
    sleep 1
  done
  assert_log 'tmpfs.*readsb.*/tmp'
  if [ "$(docker exec "${CONTAINER}" readlink /run/readsb 2>/dev/null)" = "/tmp/readsb" ]; then
    ok "/run/readsb symlinked into /tmp"
  else
    bad "/run/readsb not symlinked into /tmp (got: '$(docker exec "${CONTAINER}" readlink /run/readsb 2>/dev/null)')"
  fi
  if docker exec "${CONTAINER}" sh -c 'df -T /run/readsb 2>/dev/null | tail -1 | grep -q tmpfs'; then
    ok "readsb output is tmpfs-backed"
  else
    bad "readsb output not tmpfs-backed"
  fi
  if wait_for _file_exists /run/readsb/aircraft.json; then
    ok "readsb writes aircraft.json through the tmpfs symlink"
  else
    bad "readsb aircraft.json missing on tmpfs"
  fi

  teardown_case
}

# Runtime mirror of the build-time allowlist guard (aviation_feeder/assert-units.py):
# assert the shipped image exposes exactly the approved s6 boot surface -- the
# enrolled services AND the startup.d hooks -- and that the two pruned upstream
# wrappers (telegraf, timelapse1090) are fully gone. Catches a broken prune or an
# out-of-sync allowlist at runtime, in addition to the build-time check.
assert_ls_equals() { # $1 label, $2 dir, $3 = newline-separated expected names
  local want got
  want="$(printf '%s\n' "$3" | sed '/^[[:space:]]*$/d' | sort)"
  # -A (not plain -1) so hidden entries are listed too: the build-time guard uses
  # Python os.listdir() which includes dotfiles, so this runtime mirror must as well.
  got="$(docker exec "${CONTAINER}" sh -c "ls -1A '$2' 2>/dev/null" | sort)"
  if [ "${got}" = "${want}" ]; then
    ok "$1 matches allowlist"
  else
    bad "$1 drift in $2:"
    diff <(printf '%s\n' "${want}") <(printf '%s\n' "${got}") | sed 's/^/      /' || true
  fi
}
case_units() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names units
  section "CASE s6 unit allowlist — only approved services + startup hooks ship"
  start_container "${HERE}/fixtures/default.json"
  assert_running

  assert_ls_equals "enrolled services" /etc/s6-overlay/s6-rc.d/user/contents.d "\
adsbx-stats
aggregator-urls
autogain
cleanup_globe_history
collectd
graphs1090
graphs1090-writeback
libseccomp2
mlat-client
mlathub
nginx
readsb
startup
tar1090
tar1090-update
01-adsbhubclient
01-fr24feed
01-opensky-network
01-pfclient
01-piaware
01-show-rbfeeder-changelog
02-rbfeeder
02-show-architecture
03-show-architecture
adsbhubclient
dump978
fr24feed
fr24uat-feed
ha-mqtt
opensky-feeder
pfclient
piaware
planewatch-mlat
pw-feeder
radarvirtuel
radarvirtuel-mlat
rbfeeder
sdrmap
sdrmap-mlat
sdrmap-stunnel
uat-stats
uk1090
wait-dump978
wait-readsb"

  assert_ls_equals "startup hooks" /etc/s6-overlay/startup.d "\
01-print-container-version
01-sanity-check
04-tar1090-configure
06-range-outline
07-nginx-configure
08-graphs1090-init
50-store-uuid
52-adsbitalia-register
99-prometheus-conf"

  # Pruned units must be gone entirely: marker, unit dir, and startup hook.
  local p
  for p in \
    /etc/s6-overlay/s6-rc.d/user/contents.d/telegraf \
    /etc/s6-overlay/s6-rc.d/telegraf \
    /etc/s6-overlay/s6-rc.d/user/contents.d/timelapse1090 \
    /etc/s6-overlay/s6-rc.d/timelapse1090 \
    /etc/s6-overlay/startup.d/10-telegraf-conf \
    /etc/s6-overlay/startup.d/11-timelapse1090; do
    if docker exec "${CONTAINER}" sh -c "test -e '${p}'" 2>/dev/null; then
      bad "pruned path still present: ${p}"
    else
      ok "pruned: ${p}"
    fi
  done

  teardown_case
}

case_adsbitalia_nomlat() {
  local CONTAINER MQTT_BROKER API_MOCK MQTT_NET
  setup_case_names adsbitalia_nomlat
  section "CASE ADSBItalia name with MLAT off — name still reaches registration, no false 'no effect' warning"
  start_container "${HERE}/fixtures/adsbitalia-nomlat.json"
  assert_running
  assert_log 'container environment prepared'
  # adsbitalia_mlat=false drops the MLAT connector, so the name can't ride it...
  assert_env_not_contains ULTRAFEEDER_CONFIG 'mlat,mlat.adsbitalia.it,41113'
  # ...but registration is still on and the name still applies via ADSBITALIA_NAME.
  assert_env_contains ADSBITALIA_REGISTRATION 'true'
  assert_env_contains ADSBITALIA_NAME 'NoMlatItalia'
  # So the generic add_aggregator "name has no effect" warning must NOT fire for
  # ADSBItalia here (the name works through the registration record).
  assert_log_not 'adsbitalia_name is set, but feed_adsbitalia has no active MLAT connector'

  teardown_case
}

# --- Runner: launch every case in a bounded worker pool ---------------------
# Each case runs in its own backgrounded subshell (unique container/sidecar
# names via setup_case_names) writing to a per-case log. Concurrency is capped
# at ${E2E_JOBS:-nproc} so a 4-vCPU CI runner isn't oversubscribed by ~10
# add-on containers at once. The cases are independent (own container, own
# sidecars, own network), so wall-clock collapses toward the slowest single
# case instead of the sum. Results are tallied from the per-case logs after all
# workers finish, and the logs are replayed in case order so output stays
# readable and deterministic.
CASES=(
  case_default
  case_rtlsdr
  case_remote
  case_hostile
  case_hostile_sdr
  case_remote_bad_port
  case_uat
  case_decoder
  case_unconfig
  case_allfeeders
  case_hasensors
  case_autoloc
  case_tmpfs
  case_units
  case_adsbitalia_nomlat
)
RESULTS_DIR="$(mktemp -d)"
JOBS="${E2E_JOBS:-$(nproc 2>/dev/null || echo 4)}"
section "Running ${#CASES[@]} e2e cases (up to ${JOBS} in parallel)"
for idx in "${!CASES[@]}"; do
  # Throttle: block until a worker slot frees up before launching the next.
  while [ "$(jobs -rp | wc -l)" -ge "${JOBS}" ]; do wait -n 2>/dev/null || true; done
  (
    # Insurance: bash already resets the EXIT trap in a `( … ) &` subshell
    # (verified), but drop it explicitly so a worker exiting can never run the
    # top-level cleanup() — which would nuke sibling containers + the shared
    # RESULTS_DIR mid-run.
    trap - EXIT
    "${CASES[${idx}]}"
    echo "$?" >"${RESULTS_DIR}/${idx}.status"
  ) \
    >"${RESULTS_DIR}/${idx}.log" 2>&1 &
done
wait

total_pass=0
total_fail=0
total_crash=0
for idx in "${!CASES[@]}"; do
  cat "${RESULTS_DIR}/${idx}.log"
  p=$(grep -c '^  ok:' "${RESULTS_DIR}/${idx}.log" 2>/dev/null || true)
  f=$(grep -c '^  FAIL:' "${RESULTS_DIR}/${idx}.log" 2>/dev/null || true)
  st=$(cat "${RESULTS_DIR}/${idx}.status" 2>/dev/null || echo 1)
  total_pass=$((total_pass + ${p:-0}))
  total_fail=$((total_fail + ${f:-0}))
  if [ "${st}" -ne 0 ]; then
    total_crash=$((total_crash + 1))
    printf '  CRASH: %s exited %s (no clean completion)\n' "${CASES[${idx}]}" "${st}"
  fi
done

printf '\n==== %d passed, %d failed, %d crashed ====\n' \
  "${total_pass}" "${total_fail}" "${total_crash}"
[ "${total_fail}" -eq 0 ] && [ "${total_crash}" -eq 0 ]
