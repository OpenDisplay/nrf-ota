"""nrf-ota — Nordic Legacy DFU over BLE.

Typical usage::

    from bleak import BLEDevice
    from nrf_ota import perform_dfu, scan_for_devices

    devices = await scan_for_devices(timeout=5)
    await perform_dfu("firmware.zip", devices[0], on_progress=lambda p: print(f"{p:.0f}%"))
"""

from __future__ import annotations

import asyncio
import urllib.request
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from ._const import (
    DEFAULT_PRN,
    LEGACY_DFU_SERVICE_UUID,
    TYPE_APPLICATION,
    DeviceNotFoundError,
    DFUError,
    LogCallback,
    ProgressCallback,
)
from ._zip import DFUZipInfo, _parse_zip_bytes, parse_dfu_zip
from .dfu import LegacyDFU
from .scan import (
    _connect_with_retry,
    _resolve_address,
    _safe_disconnect,
    find_dfu_target,
    scan_for_devices,
    trigger_bootloader,
)

__version__ = "0.2.0"

__all__ = [
    "perform_dfu",
    "scan_for_devices",
    "parse_dfu_zip",
    "DFUZipInfo",
    "DFUError",
    "DeviceNotFoundError",
]


async def _load_zip(
    source: str,
    on_progress: ProgressCallback | None = None,
) -> DFUZipInfo:
    """Load a Nordic DFU ZIP from a local path or HTTP(S) URL.

    For local paths, parses synchronously.  For URLs, downloads via
    ``urllib.request`` in a thread-pool executor so the event loop is not blocked.
    """
    if not source.startswith(("http://", "https://")):
        return parse_dfu_zip(source)

    loop = asyncio.get_running_loop()

    def _blocking() -> bytes:
        with urllib.request.urlopen(source) as resp:  # noqa: S310
            total_str = resp.headers.get("Content-Length")
            total = int(total_str) if total_str else None
            chunks: list[bytes] = []
            downloaded = 0
            while chunk := resp.read(65_536):
                chunks.append(chunk)
                downloaded += len(chunk)
                if on_progress and total and downloaded < total:
                    loop.call_soon_threadsafe(on_progress, downloaded / total * 100)
            return b"".join(chunks)

    try:
        data = await loop.run_in_executor(None, _blocking)
    except OSError as exc:
        raise DFUError(f"Download failed: {exc}") from exc
    if on_progress:
        on_progress(100.0)
    return _parse_zip_bytes(data)


async def perform_dfu(
    zip_path: str | DFUZipInfo,
    device: BLEDevice | str,
    *,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
    packets_per_notification: int = DEFAULT_PRN,
) -> None:
    """Perform a Nordic Legacy DFU firmware update over BLE.

    Handles the full flow: bootloader triggering, DFU-target discovery,
    connection retries, and the protocol itself.

    Args:
        zip_path: Path or HTTP(S) URL to the Nordic DFU ZIP file (contains
            ``.bin`` + ``.dat``), or a pre-parsed :class:`DFUZipInfo`.
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
        FileNotFoundError: If *zip_path* is a local path that does not exist.
    """
    log: Callable[[str], None] = on_log or (lambda _: None)

    # 1. Load firmware ZIP (URL or path) — or use pre-parsed DFUZipInfo directly
    if isinstance(zip_path, DFUZipInfo):
        info = zip_path
    else:
        info = await _load_zip(zip_path, on_progress=on_progress)
        ver_str = f" — v{info.app_version}" if info.app_version is not None else ""
        crc_str = f" — CRC {info.crc16:#06x} ✓" if info.crc16 is not None else ""
        log(f"Firmware: {info.bin_file} ({len(info.firmware):,} bytes){ver_str}{crc_str}")

    # 2. Resolve address string
    ble_device: BLEDevice | None
    original_address: str

    if isinstance(device, str):
        original_address = device
        log(f"Address string given — scanning to resolve {device}…")
        ble_device = await _resolve_address(device, on_log=log)
    else:
        ble_device = device
        original_address = device.address

    # 3. Trigger the bootloader if in application mode
    needs_reboot = await trigger_bootloader(ble_device, on_log=log)

    dfu_device: BLEDevice
    if needs_reboot:
        log("Waiting for device to reboot into DFU mode…")
        await asyncio.sleep(1.5)
        dfu_device = await find_dfu_target(original_address, timeout=30.0, on_log=log)
        log(f"Found DFU target: {dfu_device.name} ({dfu_device.address})")
    else:
        dfu_device = ble_device

    # 4. Connect (with retries + fresh scan each attempt)
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

    # 5. Run DFU protocol
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

        await dfu.start_dfu(len(info.firmware), TYPE_APPLICATION)
        await dfu.init_dfu(info.init_packet)
        await dfu.send_firmware(info.firmware, packets_per_notification=packets_per_notification)
        await dfu.activate_and_reset()

        log("DFU complete — device is rebooting with new firmware.")
        await _safe_disconnect(client)

    except Exception:
        await _safe_disconnect(client)
        raise
