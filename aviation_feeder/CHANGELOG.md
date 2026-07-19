# Changelog

## [0.0.12](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.11...aviation_feeder-v0.0.12) (2026-07-19)


### Bug Fixes

* **ha-sensors:** report 0 for MLAT that is not syncing, not unavailable ([#78](https://github.com/bakerkj/hass-aviation-feeder/issues/78)) ([793846d](https://github.com/bakerkj/hass-aviation-feeder/commit/793846d0835daf83e48ee23c6360a8523dae719c))
* **ha-sensors:** retract discovery for feeders the user has disabled ([#77](https://github.com/bakerkj/hass-aviation-feeder/issues/77)) ([ed46781](https://github.com/bakerkj/hass-aviation-feeder/commit/ed46781c17039880a0b25107b738092f0fc1c6be))
* **ha-sensors:** sdrmap has no MLAT sync stats either ([#75](https://github.com/bakerkj/hass-aviation-feeder/issues/75)) ([edd985e](https://github.com/bakerkj/hass-aviation-feeder/commit/edd985ee2e16f7d32561547958dbf9748a24bd1c))

## [0.0.11](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.10...aviation_feeder-v0.0.11) (2026-07-19)


### Features

* **ha-sensors:** break 1090 MHz traffic down by Mode S downlink format ([#74](https://github.com/bakerkj/hass-aviation-feeder/issues/74)) ([c31d7ed](https://github.com/bakerkj/hass-aviation-feeder/commit/c31d7ed360451787cf6fea679bc815ce54b4754f))
* **ha-sensors:** publish FlightRadar24's own aircraft view ([#68](https://github.com/bakerkj/hass-aviation-feeder/issues/68)) ([348c37d](https://github.com/bakerkj/hass-aviation-feeder/commit/348c37d70dea69a5fb6334f22368739c89b583dc))
* **ha-sensors:** publish readsb's own performance metrics ([#73](https://github.com/bakerkj/hass-aviation-feeder/issues/73)) ([2706bfb](https://github.com/bakerkj/hass-aviation-feeder/commit/2706bfbf4b671e8cbd62b1675d0d7512e6bd3165))
* **ha-sensors:** publish the remaining Multi-Portal dashboard metrics ([#71](https://github.com/bakerkj/hass-aviation-feeder/issues/71)) ([#72](https://github.com/bakerkj/hass-aviation-feeder/issues/72)) ([f6133dc](https://github.com/bakerkj/hass-aviation-feeder/commit/f6133dc5e69ebd8641f7c10894ff10342bca1659))


### Miscellaneous Chores

* **deps:** update ghcr.io/sdr-enthusiasts/docker-adsb-ultrafeeder docker tag to latest-build-946 ([#69](https://github.com/bakerkj/hass-aviation-feeder/issues/69)) ([7b344c9](https://github.com/bakerkj/hass-aviation-feeder/commit/7b344c90c9c76f0c0c0bc141a155511c24832e1b))
* **deps:** update ghcr.io/sdr-enthusiasts/docker-radarbox docker tag to latest-build-882 ([#66](https://github.com/bakerkj/hass-aviation-feeder/issues/66)) ([a3c78a0](https://github.com/bakerkj/hass-aviation-feeder/commit/a3c78a0d8aefeaf0fe20c55dcdb08e2afa952f4f))


### Documentation

* **ha-sensors:** correct the FR24 non-ADS-B explanation ([#70](https://github.com/bakerkj/hass-aviation-feeder/issues/70)) ([60f20c5](https://github.com/bakerkj/hass-aviation-feeder/commit/60f20c508f32d7cde26b44dd2b35f61fc789cfcb))

## [0.0.10](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.9...aviation_feeder-v0.0.10) (2026-07-18)


### Features

* **ha-sensors:** add "unique aircraft today" counter ([#62](https://github.com/bakerkj/hass-aviation-feeder/issues/62)) ([aa25360](https://github.com/bakerkj/hass-aviation-feeder/commit/aa25360823c21a023c5ae7d6cc82a37fa1b38e61))
* **ha-sensors:** add emergency-squawk safety binary_sensor ([#61](https://github.com/bakerkj/hass-aviation-feeder/issues/61)) ([95c7617](https://github.com/bakerkj/hass-aviation-feeder/commit/95c7617f65da9517102cb6a88774a65a0c7668f5))
* **ha-sensors:** add UAT/978 receiver-stats device ([#63](https://github.com/bakerkj/hass-aviation-feeder/issues/63)) ([d4d5c4a](https://github.com/bakerkj/hass-aviation-feeder/commit/d4d5c4acfad0faa1ce91415ee92329b8d93ffbd4))

## [0.0.9](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.8...aviation_feeder-v0.0.9) (2026-07-18)


### Features

* **adsbitalia:** auto-register the station and honor adsbitalia_name ([#58](https://github.com/bakerkj/hass-aviation-feeder/issues/58)) ([c3465f2](https://github.com/bakerkj/hass-aviation-feeder/commit/c3465f2b7f062c2192f05bdd6c86d4de42c1f5e3))
* **build:** guard the s6 boot surface with an allowlist ([#54](https://github.com/bakerkj/hass-aviation-feeder/issues/54)) ([41fc0ac](https://github.com/bakerkj/hass-aviation-feeder/commit/41fc0ac13f09d6a00123cf6391931b0b7f6dc756))


### Miscellaneous Chores

* **python:** adopt ruff + mypy pre-commit hooks ([#55](https://github.com/bakerkj/hass-aviation-feeder/issues/55)) ([6e3d100](https://github.com/bakerkj/hass-aviation-feeder/commit/6e3d10021c78880437f9b622420c87240f07c70c))

## [0.0.8](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.7...aviation_feeder-v0.0.8) (2026-07-17)


### Bug Fixes

* **fr24:** strip the stale "TZ ignored" warning from fr24's config oneshot ([#51](https://github.com/bakerkj/hass-aviation-feeder/issues/51)) ([70999dd](https://github.com/bakerkj/hass-aviation-feeder/commit/70999dd7b70cd2ec61f704e1c867356483cb77b7))

## [0.0.7](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.6...aviation_feeder-v0.0.7) (2026-07-17)


### Features

* **fr24:** scope fr24's GMT requirement to its own processes, not the container ([#49](https://github.com/bakerkj/hass-aviation-feeder/issues/49)) ([1b6a374](https://github.com/bakerkj/hass-aviation-feeder/commit/1b6a374f108fb3e2328db6fbc198b9fbf4d97bcc))


### Miscellaneous Chores

* **deps:** update ghcr.io/sdr-enthusiasts/docker-adsb-ultrafeeder docker tag to latest-build-945 ([#47](https://github.com/bakerkj/hass-aviation-feeder/issues/47)) ([f91385f](https://github.com/bakerkj/hass-aviation-feeder/commit/f91385f06618624685461171bff1033c46355f30))
* **deps:** update ghcr.io/sdr-enthusiasts/docker-flightradar24 docker tag to latest-build-858 ([#42](https://github.com/bakerkj/hass-aviation-feeder/issues/42)) ([1213910](https://github.com/bakerkj/hass-aviation-feeder/commit/12139104f6d5ea198747a49e9450a6de7d67b757))

## [0.0.6](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.5...aviation_feeder-v0.0.6) (2026-07-15)


### Features

* **aggregators:** per-aggregator station name; reject separators in it ([#34](https://github.com/bakerkj/hass-aviation-feeder/issues/34)) ([8ae9402](https://github.com/bakerkj/hass-aviation-feeder/commit/8ae940264382a1760919f6d2c9d2f8efd3f70a9a))


### Bug Fixes

* **options:** validate every user value that reaches a parser or command line ([#37](https://github.com/bakerkj/hass-aviation-feeder/issues/37)) ([e61f59c](https://github.com/bakerkj/hass-aviation-feeder/commit/e61f59cf9dbdd74d4e4380bdd859a9ebc36a1fa4))


### Miscellaneous Chores

* **deps:** update sdr-enthusiasts base images ([#36](https://github.com/bakerkj/hass-aviation-feeder/issues/36)) ([6d8370e](https://github.com/bakerkj/hass-aviation-feeder/commit/6d8370ea70974f0c4a8ffb5fbb1460c4243f29ef))

## [0.0.5](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.4...aviation_feeder-v0.0.5) (2026-07-14)


### Features

* **radarvirtuel:** pin the station identity in options, not in /data ([#29](https://github.com/bakerkj/hass-aviation-feeder/issues/29)) ([4521569](https://github.com/bakerkj/hass-aviation-feeder/commit/452156947f77f97c0f94a70977cb45179e772422))

## [0.0.4](https://github.com/bakerkj/hass-aviation-feeder/compare/aviation_feeder-v0.0.3...aviation_feeder-v0.0.4) (2026-07-13)


### Features

* Aviation Feeder — merged ADS-B / UAT / MLAT Home Assistant add-on ([723dc25](https://github.com/bakerkj/hass-aviation-feeder/commit/723dc25e38984514faecbffd3a191c6c30ca1544))
* **brand:** add add-on icon ([#22](https://github.com/bakerkj/hass-aviation-feeder/issues/22)) ([fb38383](https://github.com/bakerkj/hass-aviation-feeder/commit/fb38383fe50329dc3d71309227c1141bb0c53b6a))
* reference prebuilt ghcr image in config.json ([200a620](https://github.com/bakerkj/hass-aviation-feeder/commit/200a620cc480cc72044ee2bbfb9cd66bc5612e77))
* reference prebuilt ghcr image in config.json ([b45fb02](https://github.com/bakerkj/hass-aviation-feeder/commit/b45fb026e0e9b9da7a8e3137849461a125e96eff))


### Bug Fixes

* **build:** stop host bytecode leaking into the image; guard deps at build time ([#23](https://github.com/bakerkj/hass-aviation-feeder/issues/23)) ([227d1f4](https://github.com/bakerkj/hass-aviation-feeder/commit/227d1f40256f8d7fc444b995b4e61210271eedd5))
* drop the pw-feeder CA-bundle workaround (fixed upstream in v0.0.9) ([#19](https://github.com/bakerkj/hass-aviation-feeder/issues/19)) ([993e979](https://github.com/bakerkj/hass-aviation-feeder/commit/993e97997ec9fc29eded81ff5cd39713cdfd4a8b))
* **init:** abort container init on a failed persist/tmpfs step ([#26](https://github.com/bakerkj/hass-aviation-feeder/issues/26)) ([09ca2e7](https://github.com/bakerkj/hass-aviation-feeder/commit/09ca2e7d122da2db1b5f7604776e22c52926d3ac))


### Miscellaneous Chores

* **deps:** update ghcr.io/plane-watch/docker-plane-watch docker tag to v0.0.9 ([#18](https://github.com/bakerkj/hass-aviation-feeder/issues/18)) ([4710685](https://github.com/bakerkj/hass-aviation-feeder/commit/4710685627ab50d009eb4fbaac9f3d358e6f23e3))
* **deps:** update ghcr.io/sdr-enthusiasts/docker-adsb-ultrafeeder docker tag to latest-build-942 ([#20](https://github.com/bakerkj/hass-aviation-feeder/issues/20)) ([8abc538](https://github.com/bakerkj/hass-aviation-feeder/commit/8abc538c37f80cfb9cb252ac2b52099d11696117))
* **lint:** shellcheck every shell script, at a much stricter level ([#25](https://github.com/bakerkj/hass-aviation-feeder/issues/25)) ([e5c5c0a](https://github.com/bakerkj/hass-aviation-feeder/commit/e5c5c0a631dbef611a170bf8f7e8352d8f225946))
* satisfy prek hooks (end-of-file, exec bit, codespell, prettier) ([98173d0](https://github.com/bakerkj/hass-aviation-feeder/commit/98173d0826a2a2f6119892182b64b5e6dd8087e5))
* satisfy prek hooks (end-of-file, exec bit, codespell, prettier) ([53807ba](https://github.com/bakerkj/hass-aviation-feeder/commit/53807bab199e2b0ab7c81146802d2ef3053e6d58))


### Build System

* pin feeder images to versioned tags (pure retag, no image change) ([#16](https://github.com/bakerkj/hass-aviation-feeder/issues/16)) ([7f37be5](https://github.com/bakerkj/hass-aviation-feeder/commit/7f37be5bb5adae8733fde2b4b9b528bb6cd4a115))
