# Changelog

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
