[![Tests](https://github.com/OpenDisplay-org/nrf-ota/actions/workflows/test.yml/badge.svg)](https://github.com/OpenDisplay-org/nrf-ota/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/nrf-ota)](https://pypi.org/project/nrf-ota/)
[![Python Version](https://img.shields.io/pypi/pyversions/nrf-ota)](https://pypi.org/project/nrf-ota/)

# nrf-ota

Flash firmware to Nordic nRF5x devices over BLE from Python. Implements the **Nordic Legacy DFU** protocol (nRF5 SDK ≤ 15.x) and works on Linux, macOS, and Windows.

## Installation

```bash
pip install nrf-ota
```

## CLI

Run using [uvx](https://docs.astral.sh/uv/):

```bash
uvx nrf-ota firmware.zip                                  # interactive device picker
uvx nrf-ota https://example.com/firmware.zip              # download then flash
uvx nrf-ota firmware.zip --device OD216205                # select by name
uvx nrf-ota firmware.zip --device FC:06:1C:C8:DE:47       # select by address
```

Accepts a local ZIP path or an HTTP(S) URL. Scans for nearby BLE devices, lets you pick one, and flashes the firmware. If the device is running application firmware the bootloader is triggered automatically.

## Library

```python
import asyncio
from nrf_ota import perform_dfu, scan_for_devices

async def main():
    devices = await scan_for_devices(timeout=5.0)

    # local file
    await perform_dfu("firmware.zip", devices[0], on_progress=lambda pct: print(f"\r{pct:.0f}%", end=""))

    # or a URL — downloads and flashes in one call
    await perform_dfu("https://example.com/firmware.zip", devices[0])

asyncio.run(main())
```

## API

### `perform_dfu(zip_path, device, *, on_progress=None, on_log=None, packets_per_notification=...)`

Performs a full OTA update, triggers the bootloader if needed, waits for the device to reboot into DFU mode, transfers the firmware, and activates it.

| Parameter | Type | Description |
|-----------|------|-------------|
| `zip_path` | `str \| DFUZipInfo` | Local path, HTTP(S) URL, or pre-parsed `DFUZipInfo` |
| `device` | `BLEDevice \| str` | Device from `scan_for_devices`, or a raw Bluetooth address |
| `on_progress` | `Callable[[float], None]` | Called with percentage (0–100) as firmware is sent |
| `on_log` | `Callable[[str], None]` | Called with status messages |
| `packets_per_notification` | `int` | Packets sent per receipt notification. Default: **8 on macOS**, 10 elsewhere. |

Raises `DFUError` on failure, `DeviceNotFoundError` if the bootloader can't be found after reboot.

### `DFUZipInfo`

Named tuple returned by `parse_dfu_zip`. Can be passed directly to `perform_dfu` to skip re-parsing:

```python
from nrf_ota import perform_dfu, parse_dfu_zip

info = parse_dfu_zip("firmware.zip")
print(f"{info.bin_file}  {len(info.firmware):,} bytes")
await perform_dfu(info, device)
```

### `scan_for_devices(timeout=5.0) -> list[BLEDevice]`

Scans for nearby named BLE devices and returns a list of `bleak.BLEDevice` objects.

### Exceptions

| Exception | Description |
|-----------|-------------|
| `DFUError` | Base exception for all DFU failures |
| `DeviceNotFoundError` | Bootloader not found after reboot |

## Platform notes

Works on Linux, macOS, and Windows via [bleak](https://github.com/hbldh/bleak). On macOS, the default `packets_per_notification` is lowered to 8 (from 10) to stay within CoreBluetooth's write-without-response flow control limits.

## Development

```bash
git clone https://github.com/OpenDisplay-org/nrf-ota.git
cd nrf-ota
uv sync --all-extras

uv run pytest tests/ -v
uv run ruff check .
uv run mypy src/nrf_ota
```