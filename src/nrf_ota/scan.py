"""BLE device discovery and connection helpers for Nordic DFU."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Sequence
from typing import Any, cast

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from ._const import (
    BUTTONLESS_CP_UUID,
    LEGACY_DFU_CONTROL_POINT_UUID,
    LEGACY_DFU_SERVICE_UUID,
    OP_START_DFU,
    TYPE_APPLICATION,
    DeviceNotFoundError,
    DFUError,
    LogCallback,
)

# On macOS, retrieve real Bluetooth MAC addresses via the private IOBluetooth API so
# app-mode and DFU-mode devices are distinct CBPeripheral objects (no GATT cache clash).
_CB_MACOS: dict[str, Any] = {"cb": {"use_bdaddr": True}} if sys.platform == "darwin" else {}


def _is_dfu_advertisement(name: str = "", service_uuids: Sequence[str] = ()) -> bool:
    """Return True if *name* or *service_uuids* indicate DFU bootloader mode."""
    return (
        any(x in name.upper() for x in ("ADADFU", "DFUTARG", "DFU"))
        or any(LEGACY_DFU_SERVICE_UUID.lower() in s.lower() for s in service_uuids)
    )


async def scan_for_devices(timeout: float = 5.0) -> list[BLEDevice]:
    """Discover nearby BLE devices that have a name.

    Args:
        timeout: Scan duration in seconds.

    Returns:
        List of :class:`bleak.BLEDevice` objects, filtered to named devices.
    """
    devices = await BleakScanner.discover(timeout=timeout, **_CB_MACOS)
    return [d for d in devices if d.name]


async def _discover_with_adv(timeout: float) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
    """Run a BLE scan returning ``address → (device, adv_data)`` with platform workarounds."""
    result: dict[str, tuple[BLEDevice, AdvertisementData]] = await BleakScanner.discover(
        timeout=timeout, return_adv=True, **_CB_MACOS
    )
    return result


async def _safe_disconnect(client: BleakClient) -> None:
    """Disconnect *client*, swallowing any errors."""
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        pass


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
            for d, _adv in found.values():
                if d.address.upper() == address.upper():
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


async def trigger_bootloader(
    device: BLEDevice,
    on_log: LogCallback | None = None,
) -> bool:
    """Attempt to reboot *device* into Nordic DFU bootloader mode.

    Tries the Buttonless DFU characteristic first; falls back to sending a
    Legacy DFU reboot command.  If the device name already signals it is in
    DFU mode, returns ``False`` immediately (no reboot needed).

    Args:
        device: The application-mode BLE device to reboot.
        on_log: Optional callback for status messages.

    Returns:
        ``True`` if a reboot was triggered, ``False`` if already in DFU mode.
    """
    log: Callable[[str], None] = on_log or (lambda _: None)

    # Scan for live advertisement data, bypassing the macOS Core Bluetooth GAP-name cache
    # (device.name may reflect a stale name from a previous connection).  Retry up to 3×
    # so a brief advertising gap doesn't cause us to miss the device and fall through.
    fresh = None
    for _scan_attempt in range(3):
        scan_results = await BleakScanner.discover(timeout=2, return_adv=True, **_CB_MACOS)
        fresh = scan_results.get(device.address) or scan_results.get(device.address.upper())
        if fresh:
            break
        if _scan_attempt < 2:
            await asyncio.sleep(0.5)

    if fresh:
        _, adv_data = fresh
        live_name = adv_data.local_name or ""
        in_dfu = _is_dfu_advertisement(live_name, adv_data.service_uuids or [])
        if in_dfu:
            log("Device is already in DFU bootloader mode — skipping trigger.")
            return False
        live_display_name = live_name or device.name or "unknown"
        log(f"Device '{live_display_name}' is in application mode — triggering bootloader…")
    else:
        # Device not visible after retries.  Use the cached device.name as a last resort:
        # if it suggests DFU mode, skip the trigger to avoid sending a spurious OP_START_DFU
        # to an already-booted bootloader (which would corrupt its state machine).
        cached_name = device.name or ""
        if _is_dfu_advertisement(cached_name):
            log("Device not visible in scan but name suggests DFU mode — skipping trigger.")
            return False
        log("Device not visible in scan — attempting bootloader trigger anyway…")
        # Fall through and try to trigger; we'll fail fast if the device isn't reachable.

    disconnected = False

    def _on_disconnect(client: BleakClient) -> None:
        nonlocal disconnected
        disconnected = True

    async with BleakClient(device, disconnected_callback=_on_disconnect) as client:
        # Strategy 1: Buttonless DFU
        for service in client.services:
            for char in service.characteristics:
                if char.uuid.lower() == BUTTONLESS_CP_UUID.lower():
                    log("Found Buttonless DFU characteristic — triggering…")
                    await client.start_notify(BUTTONLESS_CP_UUID, lambda s, d: None)
                    await client.write_gatt_char(BUTTONLESS_CP_UUID, b"\x01", response=True)
                    return True

        # Strategy 2: Legacy DFU reboot command
        for service in client.services:
            for char in service.characteristics:
                if char.uuid.lower() == LEGACY_DFU_CONTROL_POINT_UUID.lower():
                    log("Found Legacy DFU service — sending reboot command…")
                    await client.start_notify(char.uuid, lambda s, d: None)
                    try:
                        await client.write_gatt_char(
                            char,
                            bytes([OP_START_DFU, TYPE_APPLICATION]),
                            response=True,
                        )
                        log("Reboot trigger accepted.")
                    except (BleakError, EOFError, ConnectionError, OSError) as exc:
                        msg = str(exc).lower()
                        disconnect_indicators = (
                            "unlikely error", "0x0e", "not connected", "eof", "connection", "disconnect"
                        )
                        if any(x in msg for x in disconnect_indicators) or disconnected:
                            log("Reboot trigger accepted (device disconnected as expected).")
                        else:
                            raise DFUError(f"Bootloader trigger failed: {exc}") from exc
                    return True

    log("No DFU trigger characteristic found — assuming manual reset or already in bootloader.")
    return False


async def find_dfu_target(
    original_address: str,
    timeout: float = 30.0,
    on_log: LogCallback | None = None,
) -> BLEDevice:
    """Scan for a device that has rebooted into Nordic DFU bootloader mode.

    After a reboot, Nordic bootloaders advertise under a new address: the last
    byte of the original Bluetooth MAC is incremented by 1 (wrapping at 0xFF).
    Example: ``AA:BB:CC:DD:EE:FF`` → ``AA:BB:CC:DD:EE:00``.

    On all platforms real MAC addresses are used (macOS via ``use_bdaddr=True``),
    so the MAC+1 trick works everywhere.  Name and service-UUID matching serve
    as additional fallbacks for bootloaders that change their address.

    Args:
        original_address: Bluetooth address of the application-mode device.
        timeout: Total seconds to keep scanning before giving up.
        on_log: Optional callback for progress messages.

    Returns:
        The :class:`bleak.BLEDevice` found in DFU bootloader mode.

    Raises:
        DeviceNotFoundError: If no matching device is found within *timeout* seconds.
    """
    log: Callable[[str], None] = on_log or (lambda _: None)

    # Compute the expected post-reboot MAC (Nordic increments the last byte by 1).
    mac_parts = original_address.split(":")
    new_last = (int(mac_parts[-1], 16) + 1) % 256
    expected_mac = ":".join(mac_parts[:-1] + [f"{new_last:02X}"])

    deadline = asyncio.get_running_loop().time() + timeout
    attempt = 0
    while asyncio.get_running_loop().time() < deadline:
        results = await BleakScanner.discover(timeout=2, return_adv=True, **_CB_MACOS)
        for device, adv_data in results.values():
            mac_match = device.address.upper() == expected_mac.upper()
            live_name = adv_data.local_name or ""
            cached_name = device.name or ""
            dfu_match = (
                _is_dfu_advertisement(live_name, adv_data.service_uuids or [])
                or _is_dfu_advertisement(cached_name)
            )
            if mac_match or dfu_match:
                return device
        attempt += 1
        log(f"Scan {attempt}: DFU target not found yet, retrying…")
        await asyncio.sleep(1)

    raise DeviceNotFoundError(f"DFU target not found after {timeout:.0f} s")
