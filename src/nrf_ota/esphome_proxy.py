"""Safe Legacy DFU through an ESPHome Bluetooth proxy.

The DFU Packet characteristic is write-without-response, and bleak gives no
backpressure for that through an ESPHome proxy: packets are forwarded as fast as
they are queued and the bootloader silently loses them once its small receive
buffer overflows. Legacy DFU cannot retransmit, and the bootloader erases the old
application at START, so a lost packet means the device needs USB recovery.

What makes it reliable (measured on Seeed/Adafruit bootloader 0.9.2 through an
ESPHome 2026.9 proxy):

* The proxy answers *every* write — including write-without-response — with a
  ``BluetoothGATTWriteResponse`` or ``BluetoothGATTErrorResponse`` once the ESP32
  has handed the packet to its BLE stack. aioesphomeapi doesn't wait for it on a
  no-response write, so :class:`AckedPacketWriter` counts those replies and keeps
  at most :data:`PROXY_WINDOW` packets in flight. More in flight overflowed the
  bootloader (``OPERATION_FAILED``).
* Activate is sent on a *fresh* connection with a long (100 ms) connection
  interval. The bootloader writes its settings page while it tears the link down,
  and with the proxy's default 7.5-11.25 ms interval that flash write failed about
  half the time, leaving the new image marked invalid (USB recovery again).

See ``docs/esphome-proxy.md`` for the measurements behind these numbers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from bleak import BleakClient

from ._const import LEGACY_DFU_PACKET_UUID, TYPE_APPLICATION, DFUError, LogCallback, ProgressCallback
from ._zip import DFUZipInfo
from .dfu import LegacyDFU

#: Packets allowed in flight between proxy acknowledgements.
PROXY_WINDOW = 2
#: Packets per receipt notification on the proxy path.
PROXY_PRN = 10
#: Connection parameters for the activate connection, in BLE units:
#: 100 ms interval (80 x 1.25 ms), no slave latency, 4 s supervision timeout.
ACTIVATE_CONN_PARAMS = (80, 80, 0, 400)
#: Seconds to let the connection update take effect before activating.
ACTIVATE_SETTLE = 2.0

# aioesphomeapi.model.BluetoothProxyFeature.CONNECTION_PARAMS_SETTING
_CONNECTION_PARAMS_SETTING = 1 << 7
_ACK_TIMEOUT = 10.0
_PROBE_TIMEOUT = 5.0


class AckedPacketWriter:
    """Write-without-response to one characteristic, paced by the proxy's replies."""

    def __init__(self, api_client: Any, address: int, handle: int, window: int = PROXY_WINDOW) -> None:
        from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
            BluetoothGATTErrorResponse,
            BluetoothGATTWriteRequest,
            BluetoothGATTWriteResponse,
        )

        self._request = BluetoothGATTWriteRequest
        self._error_type = BluetoothGATTErrorResponse
        self._address = address
        self._handle = handle
        self._connection = api_client._get_connection()  # noqa: SLF001 - no public hook for no-rsp replies
        self._slots = asyncio.Semaphore(window)
        self._outstanding = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._error: int | None = None
        self._remove = self._connection.add_message_callback(
            self._on_message, (BluetoothGATTWriteResponse, BluetoothGATTErrorResponse)
        )

    def _on_message(self, msg: Any) -> None:
        if msg.address != self._address or msg.handle != self._handle or self._outstanding == 0:
            return
        self._outstanding -= 1
        if isinstance(msg, self._error_type) and self._error is None:
            self._error = msg.error
        self._slots.release()
        if self._outstanding == 0:
            self._idle.set()

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise DFUError(f"Bluetooth proxy reported GATT error {self._error} for a DFU packet")

    async def write(self, data: bytes) -> None:
        """Send one packet once a slot in the window is free."""
        self._raise_if_failed()
        try:
            await asyncio.wait_for(self._slots.acquire(), _ACK_TIMEOUT)
        except asyncio.TimeoutError:
            raise DFUError(f"Bluetooth proxy stopped acknowledging DFU packets for {_ACK_TIMEOUT:.0f}s") from None
        self._raise_if_failed()
        self._outstanding += 1
        self._idle.clear()
        self._connection.send_message(
            self._request(address=self._address, handle=self._handle, response=False, data=bytes(data))
        )

    async def drain(self, timeout: float = _ACK_TIMEOUT) -> None:
        """Wait until the proxy has acknowledged every packet sent so far."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except asyncio.TimeoutError:
            raise DFUError(
                f"Bluetooth proxy did not acknowledge {self._outstanding} DFU packet(s) within {timeout:.0f}s"
            ) from None
        self._raise_if_failed()

    def close(self) -> None:
        """Stop listening for proxy replies."""
        self._remove()


class EsphomeProxyLink:
    """The ESPHome API connection behind a bleak client that talks through a proxy."""

    def __init__(self, api_client: Any, address: int, feature_flags: int, name: str) -> None:
        self.api_client = api_client
        self.address = address
        self.feature_flags = feature_flags
        self.name = name

    @classmethod
    def from_client(cls, client: BleakClient) -> EsphomeProxyLink | None:
        """Return the proxy link if *client* is connected through bleak-esphome, else ``None``."""
        backend = getattr(client, "_backend", None)
        if backend is None or type(backend).__name__ != "ESPHomeClient":
            return None
        api_client = getattr(backend, "_client", None)
        address = getattr(backend, "_address_as_int", None)
        if api_client is None or address is None:
            return None
        device_info = getattr(backend, "_device_info", None)
        return cls(
            api_client,
            address,
            getattr(backend, "_feature_flags", 0),
            getattr(device_info, "name", None) or "ESPHome proxy",
        )

    @property
    def supports_connection_params(self) -> bool:
        return bool(self.feature_flags & _CONNECTION_PARAMS_SETTING)

    def packet_writer(self, handle: int, window: int = PROXY_WINDOW) -> AckedPacketWriter:
        return AckedPacketWriter(self.api_client, self.address, handle, window)

    async def acknowledges_no_response_writes(self, handle: int) -> bool:
        """Send one probe packet and report whether the proxy acknowledged it.

        Only call this before START: until then the bootloader has no packet type
        selected and ignores Packet characteristic writes.
        """
        writer = self.packet_writer(handle, window=1)
        try:
            await writer.write(b"\x00\x00\x00\x00")
            await writer.drain(_PROBE_TIMEOUT)
        except DFUError:
            return False
        finally:
            writer.close()
        return True

    async def set_connection_params(self, min_interval: int, max_interval: int, latency: int, timeout: int) -> None:
        await self.api_client.bluetooth_device_set_connection_params(
            self.address, min_interval, max_interval, latency, timeout
        )


async def proxy_safe_dfu(
    client: BleakClient,
    link: EsphomeProxyLink,
    info: DFUZipInfo,
    reconnect: Callable[[], Awaitable[BleakClient]],
    *,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
) -> None:
    """Run a full Legacy DFU through an ESPHome proxy, then activate on a fresh link.

    *client* must be connected to the bootloader. It is disconnected after the
    image validates; *reconnect* must return a new connected client to the same
    bootloader, which this function disconnects when done.

    Raises:
        DFUError: The proxy can't do this safely (checked before anything is
            erased), or a DFU step failed.
    """
    log: LogCallback = on_log or (lambda _: None)

    if not link.supports_connection_params:
        raise DFUError(
            f"Bluetooth proxy {link.name} can't set connection parameters, which a safe nRF update needs. "
            "Update its ESPHome version."
        )
    packet_char = client.services.get_characteristic(LEGACY_DFU_PACKET_UUID)
    if packet_char is None:
        raise DFUError("Legacy DFU Packet characteristic not found")
    if not await link.acknowledges_no_response_writes(packet_char.handle):
        raise DFUError(
            f"Bluetooth proxy {link.name} doesn't acknowledge write-without-response packets, so the update "
            "can't be paced safely. Update its ESPHome version."
        )
    log(f"Updating through Bluetooth proxy {link.name}: packets paced by proxy acknowledgements")

    writer = link.packet_writer(packet_char.handle)
    try:
        dfu = LegacyDFU(client, on_progress=on_progress, on_log=log, packet_writer=writer)
        await dfu.start()
        await dfu.start_dfu(len(info.firmware), TYPE_APPLICATION)
        await dfu.init_dfu(info.init_packet)
        await dfu.send_firmware(info.firmware, packets_per_notification=PROXY_PRN)
    finally:
        writer.close()

    # The bootloader keeps the validated image (WAIT_4_ACTIVATE) across a host
    # disconnect and advertises again.
    log("Image validated — reconnecting to activate it")
    await _disconnect(client)
    await asyncio.sleep(1.0)
    activate_client = await reconnect()
    try:
        # Raising here would strand the validated image, so a reconnect through a
        # local adapter (which activates fine without this) just goes ahead.
        activate_link = EsphomeProxyLink.from_client(activate_client)
        if activate_link is not None and activate_link.supports_connection_params:
            await activate_link.set_connection_params(*ACTIVATE_CONN_PARAMS)
            await asyncio.sleep(ACTIVATE_SETTLE)
        dfu = LegacyDFU(activate_client, on_log=log)
        await dfu.start()
        await dfu.activate_and_reset()
    finally:
        await _disconnect(activate_client)


async def _disconnect(client: BleakClient) -> None:
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001 - the bootloader may already have dropped the link
        pass
