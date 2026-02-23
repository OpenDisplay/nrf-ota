"""nrf-ota — Nordic Legacy DFU over BLE.

Typical usage::

    from bleak import BLEDevice
    from nrf_ota import perform_dfu, scan_for_devices

    devices = await scan_for_devices(timeout=5)
    await perform_dfu("firmware.zip", devices[0], on_progress=lambda p: print(f"{p:.0f}%"))
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import cast

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from ._const import (
    LEGACY_DFU_SERVICE_UUID,
    TYPE_APPLICATION,
    DeviceNotFoundError,
    DFUError,
    LogCallback,
    ProgressCallback,
)
from ._zip import parse_dfu_zip
from .dfu import LegacyDFU
from .scan import _CB_MACOS, find_dfu_target, scan_for_devices, trigger_bootloader

# macOS CoreBluetooth write-without-response flow control rejects firmware
# transfers at PRN≥10 (status 0x06).  PRN=8 is confirmed stable on macOS.
_DEFAULT_PRN: int = 8 if sys.platform == "darwin" else 10

__version__ = "0.2.0"

__all__ = [
    "perform_dfu",
    "scan_for_devices",
    "DFUError",
    "DeviceNotFoundError",
]


async def perform_dfu(
    zip_path: str,
    device: BLEDevice | str,
    *,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
    packets_per_notification: int = _DEFAULT_PRN,
) -> None:
    """Perform a Nordic Legacy DFU firmware update over BLE.

    Handles the full flow: bootloader triggering, DFU-target discovery,
    connection retries, and the protocol itself.

    Args:
        zip_path: Path to the Nordic DFU ZIP file (contains ``.bin`` + ``.dat``).
        device: Target device — either a :class:`bleak.BLEDevice` (as returned
            by :func:`scan_for_devices`) or a raw Bluetooth address string.
            Passing a string will trigger a scan to resolve the device first.
        on_progress: Optional callback invoked with ``float`` percentage (0–100)
            as firmware chunks are delivered.
        on_log: Optional callback for human-readable status messages.
        packets_per_notification: How many 20-byte BLE packets to send before
            waiting for a receipt notification from the bootloader.  Default: 8
            on macOS (CoreBluetooth flow-control limit), 10 elsewhere.  Lower
            values are more reliable; higher values speed up the transfer.

    Raises:
        DFUError: If any step of the DFU process fails.
        DeviceNotFoundError: If the DFU-mode bootloader cannot be found after
            triggering a reboot.
        FileNotFoundError: If *zip_path* does not exist.
    """
    log: Callable[[str], None] = on_log or (lambda _: None)

    # ── 1. Parse firmware ZIP ──────────────────────────────────────────────
    info = parse_dfu_zip(zip_path)
    init_packet, firmware = info.init_packet, info.firmware
    ver_str = f" — v{info.app_version}" if info.app_version is not None else ""
    crc_str = f" — CRC {info.crc16:#06x} ✓" if info.crc16 is not None else ""
    log(f"Firmware: {info.bin_file} ({len(firmware):,} bytes){ver_str}{crc_str}")

    # ── 2. Resolve address string → BLEDevice ─────────────────────────────
    ble_device: BLEDevice | None
    original_address: str

    if isinstance(device, str):
        original_address = device
        log(f"Address string given — scanning to resolve {device}…")
        ble_device = await _resolve_address(device, on_log=log)
    else:
        ble_device = device
        original_address = device.address

    # ── 3. Trigger bootloader if in application mode ───────────────────────
    needs_reboot = await trigger_bootloader(ble_device, on_log=log)

    dfu_device: BLEDevice
    if needs_reboot:
        log("Waiting for device to reboot into DFU mode…")
        await asyncio.sleep(1.5)
        dfu_device = await find_dfu_target(original_address, timeout=30.0, on_log=log)
        log(f"Found DFU target: {dfu_device.name} ({dfu_device.address})")
    else:
        dfu_device = ble_device

    # ── 4. Connect (with retries + fresh scan each attempt) ───────────────
    log(f"Connecting to {dfu_device.name or 'DFU target'} ({dfu_device.address})…")
    disconnected = False

    def _on_disconnect(c: BleakClient) -> None:
        nonlocal disconnected
        disconnected = True

    client = await _connect_with_retry(
        dfu_device.address,
        on_disconnect_cb=_on_disconnect,
        on_log=log,
    )

    # ── 5. Run DFU protocol ────────────────────────────────────────────────
    try:
        dfu_service_present = any(
            str(svc.uuid).lower() == LEGACY_DFU_SERVICE_UUID.lower()
            for svc in client.services
        )
        if not dfu_service_present:
            await _safe_disconnect(client)
            raise DFUError("Legacy DFU service not found on device")

        dfu = LegacyDFU(client, on_progress=on_progress, on_log=log)

        try:
            major, minor = await dfu.read_version()
            log(f"DFU bootloader version: {major}.{minor}")
        except Exception as exc:  # noqa: BLE001
            log(f"Warning: could not read DFU version: {exc}")

        await dfu.start()

        if disconnected or not client.is_connected:
            raise DFUError("Device disconnected before DFU could begin")

        await dfu.start_dfu(len(firmware), TYPE_APPLICATION)
        await dfu.init_dfu(init_packet)
        await dfu.send_firmware(firmware, packets_per_notification=packets_per_notification)
        await dfu.activate_and_reset()

        log("DFU complete — device is rebooting with new firmware.")
        await _safe_disconnect(client)

    except Exception:
        await _safe_disconnect(client)
        raise


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _resolve_address(address: str, on_log: LogCallback | None = None) -> BLEDevice:
    """Scan and return the first device matching *address*."""
    log: Callable[[str], None] = on_log or (lambda _: None)
    for attempt in range(5):
        if attempt > 0:
            await asyncio.sleep(1.0)
        devices = await BleakScanner.discover(timeout=3, **_CB_MACOS)
        for d in devices:
            if cast(BLEDevice, d).address.upper() == address.upper():
                return cast(BLEDevice, d)
        log(f"Device not found in scan (attempt {attempt + 1}/5)…")
    raise DeviceNotFoundError(f"Could not locate device with address {address}")


async def _connect_with_retry(
    address: str,
    *,
    max_attempts: int = 5,
    on_disconnect_cb: Callable[[BleakClient], None],
    on_log: LogCallback | None = None,
) -> BleakClient:
    """Scan for a fresh BLEDevice handle then connect, retrying on failure.

    Bleak caches BLEDevice objects internally; after a bootloader reboot the
    cached object is stale.  Re-scanning before each connect attempt ensures
    we always hand Bleak a live advertisement.
    """
    log: Callable[[str], None] = on_log or (lambda _: None)

    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(1.5)

        # Get a fresh BLEDevice from a new scan (return_adv=True gives live advertisement
        # data so the name check isn't fooled by macOS Core Bluetooth's cached GAP name).
        fresh: BLEDevice | None = None
        for scan_try in range(10):
            found = await BleakScanner.discover(timeout=2, return_adv=True, **_CB_MACOS)
            for d, adv_data in found.values():
                addr_match = d.address.upper() == address.upper()
                if addr_match:
                    fresh = d
                    break
            if fresh:
                break
            if scan_try < 9:
                await asyncio.sleep(0.5)

        if fresh is None:
            if attempt < max_attempts - 1:
                log(f"Device not visible in scan (attempt {attempt + 1}/{max_attempts}) — retrying…")
                continue
            raise DFUError("Device not found after multiple scan attempts")

        client: BleakClient | None = None
        disconnected_early = False

        def _disc(c: BleakClient) -> None:
            nonlocal disconnected_early
            disconnected_early = True
            on_disconnect_cb(c)

        try:
            client = BleakClient(fresh, disconnected_callback=_disc)
            await client.connect(timeout=30.0)

            if disconnected_early or not client.is_connected:
                await _safe_disconnect(client)
                if attempt < max_attempts - 1:
                    log(f"Disconnected immediately after connect (attempt {attempt + 1}/{max_attempts}) — retrying…")
                    continue
                raise DFUError("Device keeps disconnecting immediately after connecting")

            return client

        except (TimeoutError, BleakError) as exc:
            if client:
                await _safe_disconnect(client)
            if attempt < max_attempts - 1:
                log(f"Connection failed: {exc} (attempt {attempt + 1}/{max_attempts}) — retrying…")
                continue
            raise DFUError(f"Failed to connect after {max_attempts} attempts: {exc}") from exc

    raise DFUError("Exhausted connection attempts")  # unreachable, but satisfies mypy


async def _safe_disconnect(client: BleakClient) -> None:
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        pass
