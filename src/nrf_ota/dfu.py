"""Nordic Legacy DFU protocol implementation.

Handles the low-level BLE GATT operations defined by Nordic's Legacy DFU
bootloader (nRF5 SDK ≤ 15.x).  This module is intentionally I/O-free: no
``print``, no ``sys.exit``, no ``input``.  All progress / logging is surfaced
through caller-supplied callbacks.
"""

from __future__ import annotations

import asyncio
import json
import struct
import zipfile
from collections.abc import Callable
from typing import NamedTuple

from bleak import BleakClient

# ── UUIDs ─────────────────────────────────────────────────────────────────────

LEGACY_DFU_SERVICE_UUID = "00001530-1212-efde-1523-785feabcd123"
LEGACY_DFU_CONTROL_POINT_UUID = "00001531-1212-efde-1523-785feabcd123"
LEGACY_DFU_PACKET_UUID = "00001532-1212-efde-1523-785feabcd123"
LEGACY_DFU_VERSION_UUID = "00001534-1212-efde-1523-785feabcd123"

BUTTONLESS_SERVICE_UUID = "8ec90003-f315-4f60-9fb8-838830daea50"
BUTTONLESS_CP_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"

# ── Op-codes ──────────────────────────────────────────────────────────────────

OP_START_DFU: int = 0x01
OP_INIT_DFU_PARAMS: int = 0x02
OP_RECEIVE_FW: int = 0x03
OP_VALIDATE_FW: int = 0x04
OP_ACTIVATE_N_RESET: int = 0x05
OP_PACKET_RECEIPT_NOTIF_REQ: int = 0x08

TYPE_APPLICATION: int = 0x04

# ── Callback types ────────────────────────────────────────────────────────────

ProgressCallback = Callable[[float], None]
LogCallback = Callable[[str], None]

# ── Exceptions ────────────────────────────────────────────────────────────────


class DFUError(Exception):
    """Raised when the DFU process cannot complete."""


class DeviceNotFoundError(DFUError):
    """Raised when the target BLE device cannot be located."""


# ── ZIP parsing ───────────────────────────────────────────────────────────────


class DFUZipInfo(NamedTuple):
    """Parsed contents of a Nordic DFU ZIP file."""

    init_packet: bytes
    firmware: bytes
    bin_file: str
    crc16: int | None
    app_version: int | None  # None if sentinel 0xFFFFFFFF or absent


def _crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE — the variant Nordic uses in DFU manifest init_packet_data."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def parse_dfu_zip(path: str) -> DFUZipInfo:
    """Parse a Nordic DFU ZIP file and return a :class:`DFUZipInfo`.

    Reads ``manifest.json`` to locate the correct ``.bin`` and ``.dat`` files,
    then validates the firmware against the CRC16 stored in the manifest.

    Args:
        path: Filesystem path to the ``.zip`` produced by nRF5 SDK tools.

    Returns:
        A :class:`DFUZipInfo` with ``init_packet``, ``firmware``, ``bin_file``,
        ``crc16``, and ``app_version`` fields.

    Raises:
        DFUError: If the ZIP is malformed, missing ``manifest.json``, or the
            firmware CRC does not match.
        FileNotFoundError: If *path* does not exist.
    """
    try:
        with zipfile.ZipFile(path, "r") as z:
            if "manifest.json" not in z.namelist():
                raise DFUError("Not a Nordic DFU ZIP: manifest.json not found")

            try:
                app = json.loads(z.read("manifest.json"))["manifest"]["application"]
                bin_file: str = app["bin_file"]
                dat_file: str = app["dat_file"]
            except (ValueError, KeyError) as exc:
                raise DFUError(f"Invalid manifest.json: {exc}") from exc

            try:
                firmware = z.read(bin_file)
                init_packet = z.read(dat_file)
            except KeyError as exc:
                raise DFUError(f"Manifest references a file not in the archive: {exc}") from exc

            if not firmware:
                raise DFUError(f"Firmware file '{bin_file}' is empty")
            if not init_packet:
                raise DFUError(f"Init packet '{dat_file}' is empty")

            ipd = app.get("init_packet_data", {})
            crc_expected: int | None = ipd.get("firmware_crc16")
            if crc_expected is not None:
                crc_computed = _crc16_ccitt(firmware)
                if crc_computed != crc_expected:
                    raise DFUError(
                        f"Firmware CRC mismatch: expected {crc_expected:#06x}, "
                        f"got {crc_computed:#06x} — ZIP may be corrupt"
                    )

            raw_version: int | None = ipd.get("application_version")
            app_version = raw_version if raw_version not in (None, 0xFFFFFFFF) else None

            return DFUZipInfo(
                init_packet=init_packet,
                firmware=firmware,
                bin_file=bin_file,
                crc16=crc_expected,
                app_version=app_version,
            )
    except zipfile.BadZipFile as exc:
        raise DFUError(f"Invalid ZIP file: {exc}") from exc


# ── Protocol class ────────────────────────────────────────────────────────────


class LegacyDFU:
    """Orchestrates the Nordic Legacy DFU protocol over an active BLE connection.

    The caller is responsible for connecting :attr:`client` before calling
    :meth:`start`, and for disconnecting afterwards.  All progress / logging
    goes through callbacks rather than printed to stdout.
    """

    #: Seconds to wait for a Control Point notification before raising DFUError.
    #: Exposed as a class attribute so tests can shrink it without monkeypatching asyncio.
    _response_timeout: float = 30.0

    def __init__(
        self,
        client: BleakClient,
        on_progress: ProgressCallback | None = None,
        on_log: LogCallback | None = None,
    ) -> None:
        self.client = client
        self._on_progress: ProgressCallback = on_progress or (lambda _: None)
        self._on_log: LogCallback = on_log or (lambda _: None)
        self._evt: asyncio.Event = asyncio.Event()
        self.last_rsp: bytearray | None = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def read_version(self) -> tuple[int, int]:
        """Read and return the DFU bootloader version as ``(major, minor)``."""
        data = await self.client.read_gatt_char(LEGACY_DFU_VERSION_UUID)
        if len(data) < 2:
            raise DFUError(f"DFU version characteristic too short ({len(data)} bytes)")
        version = struct.unpack("<H", bytes(data))[0]
        return (version >> 8) & 0xFF, version & 0xFF

    async def start(self) -> None:
        """Subscribe to Control Point notifications (must be called before any step)."""
        await self.client.start_notify(LEGACY_DFU_CONTROL_POINT_UUID, self._on_notify)

    def _on_notify(self, sender: object, data: bytearray) -> None:
        self.last_rsp = data
        self._evt.set()

    async def _wait_for_response(self) -> bytearray:
        """Block until the next Control Point notification arrives (30 s timeout)."""
        # Fast path: notification already queued before we started waiting
        if self.last_rsp is not None and self._evt.is_set():
            rsp = self.last_rsp
            self._evt.clear()
            self.last_rsp = None
            return rsp

        self._evt.clear()
        self.last_rsp = None

        try:
            await asyncio.wait_for(self._evt.wait(), timeout=self._response_timeout)
        except asyncio.TimeoutError:
            raise DFUError("Timeout waiting for DFU response") from None

        if self.last_rsp is None:
            raise DFUError("Notification received but data was empty")

        rsp = self.last_rsp
        self._evt.clear()
        self.last_rsp = None
        return rsp

    # ── DFU steps ─────────────────────────────────────────────────────────────

    async def start_dfu(self, image_size: int, mode: int = TYPE_APPLICATION) -> None:
        """Send the Start DFU command together with the firmware image size."""
        self._evt.clear()
        self.last_rsp = None

        await self.client.write_gatt_char(
            LEGACY_DFU_CONTROL_POINT_UUID,
            bytes([OP_START_DFU, mode]),
            response=True,
        )
        size_packet = struct.pack("<III", 0, 0, image_size)
        await self.client.write_gatt_char(LEGACY_DFU_PACKET_UUID, size_packet, response=False)

        rsp = await self._wait_for_response()
        if len(rsp) < 3 or rsp[2] not in (0x01, 0x02):
            raise DFUError(f"Start DFU failed — response: {list(rsp)}")

    async def init_dfu(self, init_packet: bytes) -> None:
        """Send the init packet (firmware metadata / signature)."""
        self._evt.clear()
        self.last_rsp = None

        await self.client.write_gatt_char(
            LEGACY_DFU_CONTROL_POINT_UUID,
            bytes([OP_INIT_DFU_PARAMS, 0x00]),
            response=True,
        )
        await asyncio.sleep(0.05)

        chunk_size = 20
        for i in range(0, len(init_packet), chunk_size):
            chunk = init_packet[i : i + chunk_size]
            await self.client.write_gatt_char(LEGACY_DFU_PACKET_UUID, chunk, response=False)
            if i + chunk_size < len(init_packet):
                await asyncio.sleep(0.02)

        await asyncio.sleep(0.05)
        await self.client.write_gatt_char(
            LEGACY_DFU_CONTROL_POINT_UUID,
            bytes([OP_INIT_DFU_PARAMS, 0x01]),
            response=True,
        )

        rsp = await self._wait_for_response()
        if len(rsp) < 3 or rsp[2] not in (0x01, 0x02):
            raise DFUError(f"Init packet rejected — response: {list(rsp)}")

    async def send_firmware(self, firmware: bytes, packets_per_notification: int = 10) -> None:
        """Transfer the full firmware image and request on-device validation.

        Args:
            firmware: Raw firmware bytes from the DFU ZIP.
            packets_per_notification: How many 20-byte BLE packets to send
                before expecting a receipt notification.  Higher values are
                faster but leave less headroom for flow control.
        """
        self._on_log(f"Sending firmware ({len(firmware):,} bytes)…")
        self._evt.clear()
        self.last_rsp = None

        prn_value = struct.pack("<H", packets_per_notification)
        await self.client.write_gatt_char(
            LEGACY_DFU_CONTROL_POINT_UUID,
            bytes([OP_PACKET_RECEIPT_NOTIF_REQ]) + prn_value,
            response=True,
        )
        await self.client.write_gatt_char(
            LEGACY_DFU_CONTROL_POINT_UUID,
            bytes([OP_RECEIVE_FW]),
            response=True,
        )

        chunk_size = 20
        total = len(firmware)
        sent = 0
        packet_count = 0

        for i in range(0, total, chunk_size):
            chunk = firmware[i : i + chunk_size]
            await self.client.write_gatt_char(LEGACY_DFU_PACKET_UUID, chunk, response=False)
            sent += len(chunk)
            packet_count += 1

            if packet_count >= packets_per_notification:
                await self._wait_for_response()
                packet_count = 0

            self._on_progress(sent / total * 100)

        # Await the final transfer-complete notification from the bootloader
        rsp = await self._wait_for_response()

        if len(rsp) < 3:
            raise DFUError(f"Invalid notification after firmware transfer: {list(rsp)}")

        # Some bootloaders send a PRN notification (0x11) before the final response
        if rsp[0] == 0x11:
            self._evt.clear()
            self.last_rsp = None
            rsp = await self._wait_for_response()
            if len(rsp) < 3 or rsp[0] != 0x10 or rsp[1] != OP_RECEIVE_FW:
                raise DFUError(f"Unexpected response to RECEIVE_FW: {list(rsp)}")

        if rsp[0] != 0x10 or rsp[1] != OP_RECEIVE_FW:
            raise DFUError(f"Unexpected notification format after transfer: {list(rsp)}")
        if rsp[2] == 0x06:
            raise DFUError(
                "Firmware upload failed: status 0x06 (operation failed). "
                "On macOS try a lower --prn value (e.g. --prn 4)."
            )
        if rsp[2] not in (0x01, 0x02):
            raise DFUError(f"Firmware upload rejected — status {rsp[2]:#04x}")

        # Request on-device CRC validation
        self._evt.clear()
        self.last_rsp = None
        await self.client.write_gatt_char(
            LEGACY_DFU_CONTROL_POINT_UUID,
            bytes([OP_VALIDATE_FW]),
            response=True,
        )
        rsp = await self._wait_for_response()
        if len(rsp) < 3 or rsp[2] not in (0x01, 0x02):
            raise DFUError(f"Firmware validation failed — response: {list(rsp)}")

    async def activate_and_reset(self) -> None:
        """Send Activate + Reset.  A disconnect at this point is expected."""
        self._evt.clear()
        self.last_rsp = None
        try:
            await self.client.write_gatt_char(
                LEGACY_DFU_CONTROL_POINT_UUID,
                bytes([OP_ACTIVATE_N_RESET]),
                response=True,
            )
            await asyncio.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if not any(x in msg for x in ("not connected", "disconnect", "eof", "connection")):
                self._on_log(f"Warning during activate_and_reset: {exc}")
