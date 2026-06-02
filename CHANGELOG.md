# Changelog

## [0.4.0](https://github.com/OpenDisplay/nrf-ota/compare/v0.3.0...v0.4.0) (2026-06-02)


### Features

* pace firmware packets for ESPHome proxy reliability ([c68b433](https://github.com/OpenDisplay/nrf-ota/commit/c68b4330a81b8360a7710effbd490e14c1e8b334))
* verify PRN receipt offset to detect dropped packets ([93d0c4d](https://github.com/OpenDisplay/nrf-ota/commit/93d0c4d1276621bb058233bccef4cae8c828c69d))


### Bug Fixes

* don't swallow the transfer-complete response on a PRN boundary ([0d8d978](https://github.com/OpenDisplay/nrf-ota/commit/0d8d97841505447e26c5e966c07ef143b72bb128))
* treat validate INVALID_STATE as an incomplete image, not success ([19ca56c](https://github.com/OpenDisplay/nrf-ota/commit/19ca56cc8e6d56bb35a0ea0b6a7c604d10f4d50c))

## [0.3.0](https://github.com/OpenDisplay-org/nrf-ota/compare/v0.2.0...v0.3.0) (2026-02-23)


### Features

* allow url for downloading zip ([6451594](https://github.com/OpenDisplay-org/nrf-ota/commit/64515941c37e5961d68fb83ecb2c944f982baebf))
* refactor ([f9d46f0](https://github.com/OpenDisplay-org/nrf-ota/commit/f9d46f056b00c1e10213f29967d28586f8eb6db4))
* visual improvements ([def1639](https://github.com/OpenDisplay-org/nrf-ota/commit/def1639e1a651d597d1a70aa4408f7c817a9e795))


### Documentation

* update usage ([c98230f](https://github.com/OpenDisplay-org/nrf-ota/commit/c98230f2f8a9dba829ecad9774f3540d66b237d9))

## [0.2.0](https://github.com/OpenDisplay-org/nrf-ota/compare/v0.1.0...v0.2.0) (2026-02-22)


### Features

* **cli:** add --device flag for non-interactive device selection ([5246a9e](https://github.com/OpenDisplay-org/nrf-ota/commit/5246a9e14882752f4fe939ad6de21b077e42eee4))
* **cli:** add --quiet flag to suppress all non-error output ([1c98563](https://github.com/OpenDisplay-org/nrf-ota/commit/1c9856353c4afa72c6942fe9f672bce1d4272033))
* **cli:** add actionable recovery hints to error messages ([521eca0](https://github.com/OpenDisplay-org/nrf-ota/commit/521eca015845d5b9fb5e2b0c24717cb167469083))
* **cli:** use live advertisement names in device list ([cb1da38](https://github.com/OpenDisplay-org/nrf-ota/commit/cb1da38f08e4ff68f63eaa9d1ec5691ce065a85c))
* **dfu:** surface firmware filename and CRC in log output ([04e9fb3](https://github.com/OpenDisplay-org/nrf-ota/commit/04e9fb3ef39072c4c0b240f3ba0fc84843a1bd00))


### Documentation

* **scan:** update find_dfu_target docstring to reflect use_bdaddr=True ([d1ccc9f](https://github.com/OpenDisplay-org/nrf-ota/commit/d1ccc9fc6e2837055aa334df356355b2bd271ee4))

## 0.1.0 (2026-02-22)


### ⚠ BREAKING CHANGES

* initial implementation

### Features

* initial implementation ([13a42f7](https://github.com/OpenDisplay-org/nrf-ota/commit/13a42f77a593acefd0a3f25a1cbb506082528015))
