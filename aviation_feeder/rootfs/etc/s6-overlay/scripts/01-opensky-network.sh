#!/command/with-contenv bash
# shellcheck shell=bash
# Gated wrapper for the OpenSky config oneshot: skip cleanly when the feeder is
# off or unconfigured (the real script hard-exits on missing LAT/LONG/ALT/
# BEASTHOST/OPENSKY_USERNAME, which would fail s6 init).
if [ "${ENABLE_OPENSKY:-false}" != "true" ] || [ -z "${OPENSKY_USERNAME}" ] ||
  [ -z "${LAT}" ] || [ -z "${LONG}" ] || [ -z "${ALT}" ]; then
  echo "[01-opensky-network] opensky disabled/unconfigured; skipping setup"
  exit 0
fi
exec /etc/s6-overlay/scripts/01-opensky-network-real.sh
