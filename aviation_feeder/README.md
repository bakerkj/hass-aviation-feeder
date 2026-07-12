# Aviation Feeder

Merged ADS-B (1090 MHz) + UAT (978 MHz) + MLAT feeder for Home Assistant OS,
built on the [sdr-enthusiasts](https://github.com/sdr-enthusiasts)
[`docker-adsb-ultrafeeder`](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder)
stack. One add-on decodes your RTL-SDR dongle(s), shows aircraft on a built-in
[tar1090](https://github.com/wiedehopf/tar1090) map (via the Home Assistant
sidebar), and feeds as many of FlightAware, FlightRadar24, PlaneFinder, OpenSky,
ADSBHub, AirNav RadarBox, and the adsb.lol / adsb.fi / airplanes.live / ADS-B
Exchange community aggregators as you choose — with MLAT to every aggregator
that supports it, and optional Home Assistant sensors.

**See the [Documentation](DOCS.md) tab for the full configuration and usage
reference** — receiver setup, SDR dongle serials/gain/ppm, per-aggregator keys,
Home Assistant sensors, split-site mode, and an architecture overview.

## License

MIT — see [LICENSE.md](../LICENSE.md).
