"""Unit tests for nrf_ota.esphome_proxy — no proxy or BLE hardware required."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    BluetoothGATTErrorResponse,
    BluetoothGATTWriteRequest,
    BluetoothGATTWriteResponse,
)

from nrf_ota import esphome_proxy
from nrf_ota._zip import DFUZipInfo
from nrf_ota.dfu import DFUError
from nrf_ota.esphome_proxy import (
    ACTIVATE_CONN_PARAMS,
    AckedPacketWriter,
    EsphomeProxyLink,
    proxy_safe_dfu,
)

ADDRESS = 0xFC061CC8DE48
HANDLE = 16


class FakeConnection:
    """Stands in for aioesphomeapi's APIConnection message plumbing."""

    def __init__(self, auto_ack: bool = True) -> None:
        self.auto_ack = auto_ack
        self.sent: list[Any] = []
        self.callbacks: list[Any] = []

    def add_message_callback(self, cb: Any, msg_types: tuple[type, ...]) -> Any:
        self.callbacks.append(cb)
        return lambda: self.callbacks.remove(cb)

    def send_message(self, msg: Any) -> None:
        self.sent.append(msg)
        if self.auto_ack:
            ack = BluetoothGATTWriteResponse(address=msg.address, handle=msg.handle)
            asyncio.get_running_loop().call_soon(self.reply, ack)

    def reply(self, msg: Any) -> None:
        for cb in list(self.callbacks):
            cb(msg)


def make_api(connection: FakeConnection) -> MagicMock:
    api = MagicMock()
    api._get_connection.return_value = connection
    api.bluetooth_device_set_connection_params = AsyncMock()
    return api


class ESPHomeClient:
    """Named like bleak-esphome's backend so EsphomeProxyLink recognises it."""

    def __init__(self, api: Any, feature_flags: int = 0xFF) -> None:
        self._client = api
        self._address_as_int = ADDRESS
        self._feature_flags = feature_flags
        self._device_info = MagicMock()
        self._device_info.name = "test-proxy"


def make_bleak_client(api: Any, feature_flags: int = 0xFF) -> MagicMock:
    client = MagicMock()
    client._backend = ESPHomeClient(api, feature_flags)
    client.disconnect = AsyncMock()
    packet_char = MagicMock()
    packet_char.handle = HANDLE
    client.services.get_characteristic.return_value = packet_char
    return client


# ── AckedPacketWriter ─────────────────────────────────────────────────────────


async def test_writer_sends_no_response_writes_to_the_handle() -> None:
    conn = FakeConnection()
    writer = AckedPacketWriter(make_api(conn), ADDRESS, HANDLE)
    await writer.write(b"\x01\x02")
    await writer.drain()
    [req] = conn.sent
    assert isinstance(req, BluetoothGATTWriteRequest)
    assert (req.address, req.handle, req.response, req.data) == (ADDRESS, HANDLE, False, b"\x01\x02")


async def test_writer_keeps_at_most_window_packets_in_flight() -> None:
    conn = FakeConnection(auto_ack=False)
    writer = AckedPacketWriter(make_api(conn), ADDRESS, HANDLE, window=2)
    await writer.write(b"a")
    await writer.write(b"b")
    third = asyncio.ensure_future(writer.write(b"c"))
    await asyncio.sleep(0.01)
    assert len(conn.sent) == 2 and not third.done()

    conn.reply(BluetoothGATTWriteResponse(address=ADDRESS, handle=HANDLE))
    await asyncio.wait_for(third, 1.0)
    assert len(conn.sent) == 3


async def test_writer_ignores_replies_for_other_handles() -> None:
    conn = FakeConnection(auto_ack=False)
    writer = AckedPacketWriter(make_api(conn), ADDRESS, HANDLE, window=1)
    await writer.write(b"a")
    conn.reply(BluetoothGATTWriteResponse(address=ADDRESS, handle=HANDLE + 2))
    with pytest.raises(DFUError, match="did not acknowledge"):
        await writer.drain(timeout=0.05)


async def test_writer_raises_on_proxy_error_response() -> None:
    conn = FakeConnection(auto_ack=False)
    writer = AckedPacketWriter(make_api(conn), ADDRESS, HANDLE)
    await writer.write(b"a")
    conn.reply(BluetoothGATTErrorResponse(address=ADDRESS, handle=HANDLE, error=133))
    with pytest.raises(DFUError, match="GATT error 133"):
        await writer.write(b"b")


async def test_writer_treats_congestion_as_sent_and_backs_off() -> None:
    """ESP_GATT_CONGESTED (0x8F) on a no-response write means sent, slow down."""
    conn = FakeConnection(auto_ack=False)
    writer = AckedPacketWriter(make_api(conn), ADDRESS, HANDLE, window=1)
    await writer.write(b"a")
    conn.reply(BluetoothGATTErrorResponse(address=ADDRESS, handle=HANDLE, error=0x8F))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await writer.write(b"b")  # no error raised: the packet counts as sent
    assert loop.time() - t0 >= esphome_proxy._CONGESTION_BACKOFF * 0.9
    assert writer.congested == 1
    assert len(conn.sent) == 2


async def test_writer_close_stops_listening() -> None:
    conn = FakeConnection()
    writer = AckedPacketWriter(make_api(conn), ADDRESS, HANDLE)
    writer.close()
    assert conn.callbacks == []


# ── EsphomeProxyLink ──────────────────────────────────────────────────────────


def test_link_recognises_bleak_esphome_backend() -> None:
    api = make_api(FakeConnection())
    link = EsphomeProxyLink.from_client(make_bleak_client(api))
    assert link is not None
    assert (link.api_client, link.address, link.name) == (api, ADDRESS, "test-proxy")


def test_link_is_none_for_a_direct_connection() -> None:
    client = MagicMock()
    client._backend = MagicMock()  # e.g. BleakClientCoreBluetooth
    assert EsphomeProxyLink.from_client(client) is None
    assert EsphomeProxyLink.from_client(object()) is None  # type: ignore[arg-type]


def test_link_reports_connection_params_feature() -> None:
    api = make_api(FakeConnection())
    assert EsphomeProxyLink(api, ADDRESS, 1 << 7, "p").supports_connection_params
    assert not EsphomeProxyLink(api, ADDRESS, 0b0111_1111, "p").supports_connection_params


async def test_probe_detects_acknowledging_proxy() -> None:
    link = EsphomeProxyLink(make_api(FakeConnection()), ADDRESS, 0xFF, "p")
    assert await link.acknowledges_no_response_writes(HANDLE)


async def test_probe_detects_silent_proxy() -> None:
    link = EsphomeProxyLink(make_api(FakeConnection(auto_ack=False)), ADDRESS, 0xFF, "p")
    with patch.object(esphome_proxy, "_PROBE_TIMEOUT", 0.05):
        assert not await link.acknowledges_no_response_writes(HANDLE)


# ── proxy_safe_dfu ────────────────────────────────────────────────────────────

INFO = DFUZipInfo(init_packet=b"\x01\x02", firmware=b"\xab" * 40, bin_file="app.bin", crc16=None, app_version=1)


def make_mock_dfu() -> MagicMock:
    dfu = MagicMock()
    for name in ("start", "start_dfu", "init_dfu", "send_firmware", "activate_and_reset"):
        setattr(dfu, name, AsyncMock())
    return dfu


async def test_safe_dfu_transfers_then_activates_on_a_fresh_link() -> None:
    api = make_api(FakeConnection())
    client = make_bleak_client(api)
    activate_api = make_api(FakeConnection())
    activate_client = make_bleak_client(activate_api)
    transfer_dfu, activate_dfu = make_mock_dfu(), make_mock_dfu()

    with (
        patch.object(esphome_proxy, "LegacyDFU", side_effect=[transfer_dfu, activate_dfu]) as dfu_cls,
        patch.object(esphome_proxy, "ACTIVATE_SETTLE", 0),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await proxy_safe_dfu(
            client, EsphomeProxyLink.from_client(client), INFO, AsyncMock(return_value=activate_client)
        )

    assert isinstance(dfu_cls.call_args_list[0].kwargs["packet_writer"], AckedPacketWriter)
    transfer_dfu.send_firmware.assert_awaited_once_with(INFO.firmware, packets_per_notification=10)
    transfer_dfu.activate_and_reset.assert_not_awaited()
    client.disconnect.assert_awaited()
    activate_api.bluetooth_device_set_connection_params.assert_awaited_once_with(ADDRESS, *ACTIVATE_CONN_PARAMS)
    activate_dfu.start.assert_awaited_once()
    activate_dfu.activate_and_reset.assert_awaited_once()
    activate_client.disconnect.assert_awaited()


async def test_safe_dfu_refuses_proxy_without_connection_params() -> None:
    client = make_bleak_client(make_api(FakeConnection()), feature_flags=0b0111_1111)
    reconnect = AsyncMock()
    with patch.object(esphome_proxy, "LegacyDFU") as dfu_cls:
        with pytest.raises(DFUError, match="connection parameters"):
            await proxy_safe_dfu(client, EsphomeProxyLink.from_client(client), INFO, reconnect)
    dfu_cls.assert_not_called()
    reconnect.assert_not_awaited()


async def test_safe_dfu_refuses_proxy_that_does_not_ack_before_start() -> None:
    client = make_bleak_client(make_api(FakeConnection(auto_ack=False)))
    with (
        patch.object(esphome_proxy, "_PROBE_TIMEOUT", 0.05),
        patch.object(esphome_proxy, "LegacyDFU") as dfu_cls,
    ):
        with pytest.raises(DFUError, match="acknowledge"):
            await proxy_safe_dfu(client, EsphomeProxyLink.from_client(client), INFO, AsyncMock())
    dfu_cls.assert_not_called()


async def test_safe_dfu_does_not_activate_after_a_failed_transfer() -> None:
    client = make_bleak_client(make_api(FakeConnection()))
    transfer_dfu = make_mock_dfu()
    transfer_dfu.send_firmware.side_effect = DFUError("Dropped packet")
    reconnect = AsyncMock()
    with patch.object(esphome_proxy, "LegacyDFU", return_value=transfer_dfu):
        with pytest.raises(DFUError, match="Dropped packet"):
            await proxy_safe_dfu(client, EsphomeProxyLink.from_client(client), INFO, reconnect)
    reconnect.assert_not_awaited()
    transfer_dfu.activate_and_reset.assert_not_awaited()


async def test_safe_dfu_activates_without_params_after_reconnecting_directly() -> None:
    client = make_bleak_client(make_api(FakeConnection()))
    direct_client = MagicMock()
    direct_client.disconnect = AsyncMock()
    activate_dfu = make_mock_dfu()
    with (
        patch.object(esphome_proxy, "LegacyDFU", side_effect=[make_mock_dfu(), activate_dfu]),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await proxy_safe_dfu(client, EsphomeProxyLink.from_client(client), INFO, AsyncMock(return_value=direct_client))
    activate_dfu.activate_and_reset.assert_awaited_once()


async def test_perform_dfu_uses_the_safe_path_through_a_proxy() -> None:
    from nrf_ota import perform_dfu

    service = MagicMock()
    service.uuid = "00001530-1212-efde-1523-785feabcd123"
    client = make_bleak_client(make_api(FakeConnection()))
    client.services.__iter__ = lambda _self: iter([service])
    device = MagicMock()
    device.address, device.name = "FC:06:1C:C8:DE:48", "AdaDFU"

    with (
        patch("nrf_ota.trigger_bootloader", new=AsyncMock(return_value=False)),
        patch("nrf_ota._connect_with_retry", new=AsyncMock(return_value=client)),
        patch("nrf_ota.proxy_safe_dfu", new=AsyncMock()) as safe_dfu,
        patch("nrf_ota.LegacyDFU") as direct_dfu,
    ):
        await perform_dfu(INFO, device)

    safe_dfu.assert_awaited_once()
    direct_dfu.assert_not_called()
