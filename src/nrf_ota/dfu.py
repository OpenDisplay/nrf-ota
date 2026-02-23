"""Nordic Legacy DFU protocol implementation.

Handles the low-level BLE GATT operations defined by Nordic's Legacy DFU
bootloader (nRF5 SDK ≤ 15.x).  This module is intentionally I/O-free: no
``print``, no ``sys.exit``, no ``input``.  All progress / logging is surfaced
through caller-supplied callbacks.
"""

from __future__ import annotations

import asyncio
import struct

from bleak import BleakClient

# Re-export so callers that do `from nrf_ota.dfu import ...` keep working.
from ._const import (  # noqa: F401
    BUTTONLESS_CP_UUID,
    BUTTONLESS_SERVICE_UUID,
    LEGACY_DFU_CONTROL_POINT_UUID,
    LEGACY_DFU_PACKET_UUID,
    LEGACY_DFU_SERVICE_UUID,
    LEGACY_DFU_VERSION_UUID,
    OP_ACTIVATE_N_RESET,
    OP_INIT_DFU_PARAMS,
    OP_PACKET_RECEIPT_NOTIF_REQ,
    OP_RECEIVE_FW,
    OP_START_DFU,
    OP_VALIDATE_FW,
    TYPE_APPLICATION,
    DeviceNotFoundError,
    DFUError,
    LogCallback,
    ProgressCallback,
)
from ._zip import DFUZipInfo, _crc16_ccitt, parse_dfu_zip  # noqa: F401


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
