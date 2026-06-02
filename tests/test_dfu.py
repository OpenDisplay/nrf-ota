"""Unit tests for nrf_ota.dfu — no BLE hardware required."""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.scanner import AdvertisementData

from nrf_ota import perform_dfu
from nrf_ota._zip import DFUZipInfo
from nrf_ota.dfu import LEGACY_DFU_SERVICE_UUID, DeviceNotFoundError, DFUError, LegacyDFU, parse_dfu_zip
from nrf_ota.scan import _connect_with_retry, find_dfu_target, trigger_bootloader


def make_adv_data(
    local_name: str | None = None,
    service_uuids: list[str] | None = None,
) -> AdvertisementData:
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={},
        service_data={},
        service_uuids=service_uuids or [],
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )


def fire_notify(dfu_instance: LegacyDFU, data: bytes, delay: float = 0.01) -> asyncio.Task[None]:
    """Schedule a simulated Control Point notification on *dfu_instance*."""

    async def _fire() -> None:
        await asyncio.sleep(delay)
        dfu_instance._on_notify(None, bytearray(data))

    return asyncio.ensure_future(_fire())


# ── parse_dfu_zip ─────────────────────────────────────────────────────────────


def test_parse_dfu_zip_returns_dat_and_bin(dfu_zip: Path) -> None:
    info = parse_dfu_zip(str(dfu_zip))
    assert info.init_packet == b"\x01\x02\x03\x04"
    assert info.firmware == b"\xde\xad\xbe\xef" * 64
    assert info.bin_file == "application.bin"
    assert info.crc16 is not None


def test_parse_dfu_zip_no_manifest(tmp_path: Path) -> None:
    zip_path = tmp_path / "no_manifest.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("application.bin", b"\x01")
        z.writestr("application.dat", b"\x01")
    with pytest.raises(DFUError, match="manifest.json"):
        parse_dfu_zip(str(zip_path))


def test_parse_dfu_zip_missing_bin(tmp_path: Path) -> None:
    """Manifest references a .bin that isn't in the archive."""
    zip_path = tmp_path / "no_bin.zip"
    manifest = json.dumps({"manifest": {"application": {"bin_file": "app.bin", "dat_file": "app.dat"}}})
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("manifest.json", manifest)
        z.writestr("app.dat", b"\x01")
    with pytest.raises(DFUError, match="not in the archive"):
        parse_dfu_zip(str(zip_path))


def test_parse_dfu_zip_crc_mismatch(tmp_path: Path) -> None:
    zip_path = tmp_path / "bad_crc.zip"
    firmware = b"\xde\xad\xbe\xef" * 64
    manifest = json.dumps({
        "manifest": {
            "application": {
                "bin_file": "app.bin",
                "dat_file": "app.dat",
                "init_packet_data": {"firmware_crc16": 0xDEAD},
            }
        }
    })
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("manifest.json", manifest)
        z.writestr("app.bin", firmware)
        z.writestr("app.dat", b"\x01\x02\x03\x04")
    with pytest.raises(DFUError, match="CRC mismatch"):
        parse_dfu_zip(str(zip_path))


def test_parse_dfu_zip_bad_zip(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip file at all")
    with pytest.raises(DFUError, match="Invalid ZIP"):
        parse_dfu_zip(str(bad))


def test_parse_dfu_zip_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        parse_dfu_zip("/nonexistent/path/firmware.zip")


# LegacyDFU.read_version


async def test_read_version_returns_major_minor(dfu: LegacyDFU) -> None:
    # mock_ble_client returns b"\x06\x01" → little-endian u16 = 0x0106
    major, minor = await dfu.read_version()
    assert major == 1
    assert minor == 6


# LegacyDFU.start_dfu


async def test_start_dfu_success(dfu: LegacyDFU) -> None:
    # Response: opcode=0x10, request=START_DFU(0x01), status=0x01 (success)
    fire_notify(dfu, b"\x10\x01\x01")
    await dfu.start()
    await dfu.start_dfu(image_size=256)
    # No exception → success


async def test_start_dfu_bad_status(dfu: LegacyDFU) -> None:
    fire_notify(dfu, b"\x10\x01\x04")  # status 0x04 = invalid state
    await dfu.start()
    with pytest.raises(DFUError, match="Start DFU failed"):
        await dfu.start_dfu(image_size=256)


async def test_start_dfu_short_response(dfu: LegacyDFU) -> None:
    fire_notify(dfu, b"\x10")  # too short
    await dfu.start()
    with pytest.raises(DFUError, match="Start DFU failed"):
        await dfu.start_dfu(image_size=256)


# LegacyDFU._wait_for_response timeout


async def test_wait_for_response_timeout(dfu: LegacyDFU) -> None:
    """Set a tiny response timeout so no notification → DFUError quickly."""
    dfu._response_timeout = 0.05  # 50 ms — fast for CI

    await dfu.start()
    with pytest.raises(DFUError, match="Timeout"):
        await dfu._wait_for_response()


# LegacyDFU callbacks


async def test_on_progress_called(dfu_zip: Path, mock_ble_client: MagicMock) -> None:
    progress_calls: list[float] = []
    instance = LegacyDFU(mock_ble_client, on_progress=progress_calls.append)

    # Simulate the full send_firmware happy path with a tiny firmware blob
    firmware = b"\xAA" * 20  # exactly one chunk

    # Set up sequential notifications:
    # 1. receipt notification (0x11) after PRN packets — skipped since 1 chunk < PRN=30
    # 2. final response: opcode=0x10, RECEIVE_FW, status=0x01
    fire_notify(instance, b"\x10\x03\x01", delay=0.05)

    # Also fire the validate response
    import asyncio

    async def fire_validate() -> None:
        await asyncio.sleep(0.15)
        instance._on_notify(None, bytearray(b"\x10\x04\x01"))

    asyncio.ensure_future(fire_validate())

    await instance.start()
    await instance.send_firmware(firmware)

    assert len(progress_calls) >= 1
    assert progress_calls[-1] == pytest.approx(100.0)


async def test_on_log_called(mock_ble_client: MagicMock) -> None:
    logs: list[str] = []
    instance = LegacyDFU(mock_ble_client, on_log=logs.append)

    fire_notify(instance, b"\x10\x03\x01", delay=0.05)

    import asyncio

    async def fire_validate() -> None:
        await asyncio.sleep(0.15)
        instance._on_notify(None, bytearray(b"\x10\x04\x01"))

    asyncio.ensure_future(fire_validate())

    await instance.start()
    await instance.send_firmware(b"\xBB" * 20)

    assert any("Sending firmware" in msg for msg in logs)


def _autorespond(instance: LegacyDFU, total: int) -> AsyncMock:
    """A write_gatt_char mock that fires the bootloader notifications the protocol
    expects: a transfer-complete once all firmware bytes are written, and a
    validate response when VALIDATE_FW is sent. Deterministic — no timers."""
    from nrf_ota._const import (
        LEGACY_DFU_CONTROL_POINT_UUID,
        LEGACY_DFU_PACKET_UUID,
        OP_VALIDATE_FW,
    )

    state = {"bytes": 0}

    async def on_write(uuid: str, data: bytes, response: bool = False) -> None:
        if uuid == LEGACY_DFU_PACKET_UUID:
            state["bytes"] += len(data)
            if state["bytes"] >= total:
                instance._on_notify(None, bytearray(b"\x10\x03\x01"))  # transfer complete
        elif uuid == LEGACY_DFU_CONTROL_POINT_UUID and bytes(data)[:1] == bytes([OP_VALIDATE_FW]):
            instance._on_notify(None, bytearray(b"\x10\x04\x01"))  # validate OK

    return AsyncMock(side_effect=on_write)


async def test_send_firmware_paces_within_batch_when_delay_set(mock_ble_client: MagicMock) -> None:
    """inter_packet_delay paces each within-batch data packet so write-without-
    response survives an ESPHome proxy (the burst that init_dfu avoids and
    send_firmware previously did not)."""
    instance = LegacyDFU(mock_ble_client)
    mock_ble_client.write_gatt_char = _autorespond(instance, total=100)

    with patch("nrf_ota.dfu.asyncio.sleep", new=AsyncMock()) as spy:
        await instance.send_firmware(b"\xAA" * 100, inter_packet_delay=0.02)  # 5 packets < PRN

    paced = [c for c in spy.await_args_list if c.args and c.args[0] == 0.02]
    assert len(paced) == 4  # 5 packets, paced between each except the final one


async def test_send_firmware_does_not_pace_by_default(mock_ble_client: MagicMock) -> None:
    """Default (inter_packet_delay=0.0) keeps the fast direct-connection behaviour
    — no per-packet sleeps."""
    instance = LegacyDFU(mock_ble_client)
    mock_ble_client.write_gatt_char = _autorespond(instance, total=100)

    with patch("nrf_ota.dfu.asyncio.sleep", new=AsyncMock()) as spy:
        await instance.send_firmware(b"\xAA" * 100)

    assert spy.await_count == 0


async def test_send_firmware_final_packet_on_prn_boundary(mock_ble_client: MagicMock) -> None:
    """Image ending exactly on a PRN boundary: the bootloader's transfer-complete
    response must be consumed by the post-loop wait, not the in-loop receipt wait.
    Regression — the in-loop wait used to swallow it and the final wait then hung."""
    instance = LegacyDFU(mock_ble_client)
    instance._response_timeout = 1.0  # fail fast instead of 30s if regressed
    mock_ble_client.write_gatt_char = _autorespond(instance, total=200)

    # 200 bytes = exactly 10 × 20-byte packets = PRN(10) boundary. Must not hang.
    await instance.send_firmware(b"\xAA" * 200)


def _autorespond_with_receipts(instance, total, prn, drop_after=None):
    """write_gatt_char mock that fires a PRN receipt (reporting cumulative bytes
    received) every `prn` packets, plus the transfer-complete and validate
    responses. If drop_after is set, the device under-reports by one packet once
    that many bytes have been sent — simulating a silently dropped packet."""
    import struct as _struct

    from nrf_ota._const import (
        LEGACY_DFU_CONTROL_POINT_UUID,
        LEGACY_DFU_PACKET_UUID,
        OP_PKT_RECEIPT_NOTIF,
        OP_VALIDATE_FW,
    )

    st = {"sent": 0, "pkts": 0}

    async def on_write(uuid, data, response=False):  # noqa: ARG001
        if uuid == LEGACY_DFU_PACKET_UUID:
            st["sent"] += len(data)
            st["pkts"] += 1
            received = st["sent"] - 20 if drop_after is not None and st["sent"] > drop_after else st["sent"]
            if st["sent"] >= total:
                instance._on_notify(None, bytearray(b"\x10\x03\x01"))  # transfer complete
            elif st["pkts"] % prn == 0:
                instance._on_notify(None, bytearray([OP_PKT_RECEIPT_NOTIF]) + _struct.pack("<I", received))
        elif uuid == LEGACY_DFU_CONTROL_POINT_UUID and bytes(data)[:1] == bytes([OP_VALIDATE_FW]):
            instance._on_notify(None, bytearray(b"\x10\x04\x01"))

    return AsyncMock(side_effect=on_write)


async def test_send_firmware_accepts_matching_receipts(mock_ble_client: MagicMock) -> None:
    """A PRN receipt whose byte count matches what was sent passes through."""
    instance = LegacyDFU(mock_ble_client)
    instance._response_timeout = 1.0
    total = 20 * 25  # 25 packets, PRN 10 -> receipts at 10 and 20
    mock_ble_client.write_gatt_char = _autorespond_with_receipts(instance, total, prn=10)
    with patch("nrf_ota.dfu.asyncio.sleep", new=AsyncMock()):
        await instance.send_firmware(b"\xAA" * total)  # default PRN=10


async def test_send_firmware_detects_dropped_packet(mock_ble_client: MagicMock) -> None:
    """A receipt reporting fewer bytes than sent (a dropped write-without-response
    packet) fails fast with a clear DFUError instead of corrupting the image."""
    instance = LegacyDFU(mock_ble_client)
    instance._response_timeout = 1.0
    total = 20 * 25
    mock_ble_client.write_gatt_char = _autorespond_with_receipts(instance, total, prn=10, drop_after=150)
    with patch("nrf_ota.dfu.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DFUError, match="Dropped packet"):
            await instance.send_firmware(b"\xAA" * total)


# find_dfu_target


def _make_ble_device(address: str, name: str | None) -> MagicMock:
    d = MagicMock()
    d.address = address
    d.name = name
    return d


async def test_find_dfu_target_mac_plus_one() -> None:
    """Matches a device whose address is original MAC + 1 on last byte."""
    dfu_device = _make_ble_device("AA:BB:CC:DD:EE:02", "DfuTarg")
    adv = make_adv_data(local_name="DfuTarg")
    discover_result = {"AA:BB:CC:DD:EE:02": (dfu_device, adv)}
    with patch("nrf_ota.scan.BleakScanner.discover", new=AsyncMock(return_value=discover_result)):
        result = await find_dfu_target("AA:BB:CC:DD:EE:01", timeout=5.0)
    assert result.address == "AA:BB:CC:DD:EE:02"



async def test_find_dfu_target_name_fallback() -> None:
    """Live advertisement name 'DFU' matches even when cached device.name gives no hint."""
    dfu_device = _make_ble_device("AA:BB:CC:DD:EE:FF", "ODC9D54")  # stale cached name
    adv = make_adv_data(local_name="DfuTarg")  # live advertisement says DFU
    discover_result = {"AA:BB:CC:DD:EE:FF": (dfu_device, adv)}
    with patch("nrf_ota.scan.BleakScanner.discover", new=AsyncMock(return_value=discover_result)):
        result = await find_dfu_target("AA:BB:CC:DD:EE:00", timeout=5.0)  # MAC doesn't match
    assert result.address == "AA:BB:CC:DD:EE:FF"


async def test_find_dfu_target_service_uuid_match() -> None:
    """Matches on Legacy DFU service UUID even when address doesn't produce a MAC+1 match."""
    dfu_device = _make_ble_device("AA:BB:CC:DD:EE:FF", "ODC9D54")  # cached app-mode name
    adv = make_adv_data(local_name=None, service_uuids=[LEGACY_DFU_SERVICE_UUID])
    discover_result = {"AA:BB:CC:DD:EE:FF": (dfu_device, adv)}
    with patch("nrf_ota.scan.BleakScanner.discover", new=AsyncMock(return_value=discover_result)):
        result = await find_dfu_target("BB:BB:CC:DD:EE:FE", timeout=5.0)  # MAC+1 = BB:BB:CC:DD:EE:FF, no match
    assert result.address == "AA:BB:CC:DD:EE:FF"


# trigger_bootloader


async def test_trigger_bootloader_skips_when_live_name_is_dfu() -> None:
    """Returns False immediately when live advertisement name contains 'DFU'."""
    device = _make_ble_device("AA:BB:CC:DD:EE:FF", "OD355226")
    adv = make_adv_data(local_name="AdaDFU")
    scan_result = {device.address: (device, adv)}
    with patch("nrf_ota.scan.BleakScanner.discover", new=AsyncMock(return_value=scan_result)):
        with patch("nrf_ota.scan.BleakClient") as mock_client_cls:
            result = await trigger_bootloader(device)
    assert result is False
    mock_client_cls.assert_not_called()


async def test_trigger_bootloader_skips_when_cached_name_suggests_dfu() -> None:
    """Returns False (no connect) when device not visible but cached name implies DFU."""
    device = _make_ble_device("AA:BB:CC:DD:EE:FF", "AdaDFU")
    with patch("nrf_ota.scan.BleakScanner.discover", new=AsyncMock(return_value={})):
        with patch("nrf_ota.scan.BleakClient") as mock_client_cls:
            result = await trigger_bootloader(device)
    assert result is False
    mock_client_cls.assert_not_called()


async def test_find_dfu_target_not_found() -> None:
    """Raises DeviceNotFoundError if the scan window expires."""
    with patch("nrf_ota.scan.BleakScanner.discover", new=AsyncMock(return_value={})):
        with pytest.raises(DeviceNotFoundError):
            await find_dfu_target("AA:BB:CC:DD:EE:01", timeout=0.1)


# _connect_with_retry


async def test_connect_with_retry_success_first_attempt() -> None:
    """Returns a connected BleakClient when device is found and connects on the first try."""
    mock_device = _make_ble_device("AA:BB:CC:DD:EE:FF", "DfuTarg")
    adv = make_adv_data(local_name="DfuTarg")
    scan_result = {"AA:BB:CC:DD:EE:FF": (mock_device, adv)}

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.is_connected = True

    disconnect_calls: list[object] = []

    with patch("nrf_ota.scan.BleakScanner.discover", new=AsyncMock(return_value=scan_result)):
        with patch("nrf_ota.scan.BleakClient", return_value=mock_client) as mock_cls:
            result = await _connect_with_retry(
                "AA:BB:CC:DD:EE:FF",
                on_disconnect_cb=disconnect_calls.append,
            )

    assert result is mock_client
    mock_client.connect.assert_awaited_once()
    assert mock_cls.call_count == 1


async def test_connect_with_retry_device_not_found_raises() -> None:
    """Raises DFUError when device never appears in scan across all attempts."""
    disconnect_calls: list[object] = []

    with patch("nrf_ota.scan.BleakScanner.discover", new=AsyncMock(return_value={})):
        with patch("nrf_ota.scan.BleakClient") as mock_cls:
            with pytest.raises(DFUError, match="not found after multiple scan attempts"):
                await _connect_with_retry(
                    "AA:BB:CC:DD:EE:FF",
                    max_attempts=1,
                    on_disconnect_cb=disconnect_calls.append,
                )

    mock_cls.assert_not_called()


# perform_dfu


async def test_perform_dfu_happy_path_simple(mock_ble_client: MagicMock) -> None:
    """perform_dfu calls all DFU steps in order when device is already in DFU mode."""
    mock_service = MagicMock()
    mock_service.uuid = LEGACY_DFU_SERVICE_UUID
    mock_ble_client.services = [mock_service]
    mock_ble_client.disconnect = AsyncMock()

    mock_dfu = MagicMock()
    mock_dfu.read_version = AsyncMock(return_value=(1, 6))
    mock_dfu.start = AsyncMock()
    mock_dfu.start_dfu = AsyncMock()
    mock_dfu.init_dfu = AsyncMock()
    mock_dfu.send_firmware = AsyncMock()
    mock_dfu.activate_and_reset = AsyncMock()

    fake_info = DFUZipInfo(
        init_packet=b"\x01\x02",
        firmware=b"\xAB" * 40,
        bin_file="app.bin",
        crc16=None,
        app_version=1,
    )

    logs: list[str] = []

    with patch("nrf_ota.parse_dfu_zip", return_value=fake_info):
        with patch("nrf_ota.trigger_bootloader", new=AsyncMock(return_value=False)):
            with patch("nrf_ota._connect_with_retry", new=AsyncMock(return_value=mock_ble_client)):
                with patch("nrf_ota.LegacyDFU", return_value=mock_dfu):
                    await perform_dfu(
                        "firmware.zip",
                        _make_ble_device("AA:BB:CC:DD:EE:FF", "DfuTarg"),
                        on_log=logs.append,
                    )

    mock_dfu.start.assert_awaited_once()
    mock_dfu.start_dfu.assert_awaited_once()
    mock_dfu.init_dfu.assert_awaited_once()
    mock_dfu.send_firmware.assert_awaited_once()
    mock_dfu.activate_and_reset.assert_awaited_once()
    assert any("DFU complete" in msg for msg in logs)


async def test_perform_dfu_accepts_dfu_zip_info(mock_ble_client: MagicMock) -> None:
    """perform_dfu skips _load_zip entirely when passed a DFUZipInfo directly."""
    mock_service = MagicMock()
    mock_service.uuid = LEGACY_DFU_SERVICE_UUID
    mock_ble_client.services = [mock_service]
    mock_ble_client.disconnect = AsyncMock()

    mock_dfu = MagicMock()
    mock_dfu.read_version = AsyncMock(return_value=(1, 6))
    mock_dfu.start = AsyncMock()
    mock_dfu.start_dfu = AsyncMock()
    mock_dfu.init_dfu = AsyncMock()
    mock_dfu.send_firmware = AsyncMock()
    mock_dfu.activate_and_reset = AsyncMock()

    fake_info = DFUZipInfo(
        init_packet=b"\x01\x02",
        firmware=b"\xAB" * 40,
        bin_file="app.bin",
        crc16=None,
        app_version=1,
    )
    logs: list[str] = []

    with patch("nrf_ota.parse_dfu_zip") as mock_parse, \
         patch("nrf_ota.trigger_bootloader", new=AsyncMock(return_value=False)), \
         patch("nrf_ota._connect_with_retry", new=AsyncMock(return_value=mock_ble_client)), \
         patch("nrf_ota.LegacyDFU", return_value=mock_dfu):
        await perform_dfu(fake_info, _make_ble_device("AA:BB:CC:DD:EE:FF", "DfuTarg"), on_log=logs.append)

    mock_parse.assert_not_called()
    mock_dfu.send_firmware.assert_awaited_once()


async def test_perform_dfu_url_string(mock_ble_client: MagicMock) -> None:
    """perform_dfu downloads and flashes when given an HTTPS URL."""
    import io
    import json
    import zipfile

    from nrf_ota._zip import crc16_ccitt

    mock_service = MagicMock()
    mock_service.uuid = LEGACY_DFU_SERVICE_UUID
    mock_ble_client.services = [mock_service]
    mock_ble_client.disconnect = AsyncMock()

    mock_dfu = MagicMock()
    mock_dfu.read_version = AsyncMock(return_value=(1, 6))
    mock_dfu.start = AsyncMock()
    mock_dfu.start_dfu = AsyncMock()
    mock_dfu.init_dfu = AsyncMock()
    mock_dfu.send_firmware = AsyncMock()
    mock_dfu.activate_and_reset = AsyncMock()

    firmware = b"\xde\xad" * 20
    init_packet = b"\x00\x01\x02\x03"
    manifest = json.dumps({
        "manifest": {
            "application": {
                "bin_file": "app.bin",
                "dat_file": "app.dat",
                "init_packet_data": {"firmware_crc16": crc16_ccitt(firmware)},
            }
        }
    })
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", manifest)
        z.writestr("app.bin", firmware)
        z.writestr("app.dat", init_packet)
    zip_bytes = buf.getvalue()

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.headers.get.return_value = str(len(zip_bytes))
    mock_resp.read.side_effect = [zip_bytes, b""]

    with patch("urllib.request.urlopen", return_value=mock_resp), \
         patch("nrf_ota.trigger_bootloader", new=AsyncMock(return_value=False)), \
         patch("nrf_ota._connect_with_retry", new=AsyncMock(return_value=mock_ble_client)), \
         patch("nrf_ota.LegacyDFU", return_value=mock_dfu):
        await perform_dfu(
            "https://example.com/firmware.zip",
            _make_ble_device("AA:BB:CC:DD:EE:FF", "DfuTarg"),
        )

    mock_dfu.send_firmware.assert_awaited_once()
