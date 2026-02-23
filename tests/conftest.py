"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from bleak import BleakClient

from nrf_ota._zip import crc16_ccitt
from nrf_ota.dfu import LegacyDFU

_FIRMWARE = b"\xde\xad\xbe\xef" * 64
_INIT_PACKET = b"\x01\x02\x03\x04"


@pytest.fixture
def dfu_zip(tmp_path: Path) -> Path:
    """A minimal but valid Nordic DFU ZIP with manifest, .bin, and .dat."""
    zip_path = tmp_path / "firmware.zip"
    manifest = json.dumps({
        "manifest": {
            "application": {
                "bin_file": "application.bin",
                "dat_file": "application.dat",
                "init_packet_data": {"firmware_crc16": crc16_ccitt(_FIRMWARE)},
            }
        }
    })
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("manifest.json", manifest)
        z.writestr("application.bin", _FIRMWARE)
        z.writestr("application.dat", _INIT_PACKET)
    return zip_path


@pytest.fixture
def mock_ble_client() -> MagicMock:
    """A MagicMock of BleakClient with async GATT methods stubbed out."""
    client = MagicMock(spec=BleakClient)
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=bytearray(b"\x06\x01"))  # version 6.1
    client.start_notify = AsyncMock()
    client.is_connected = True
    client.services = []
    return client


@pytest.fixture
def dfu(mock_ble_client: MagicMock) -> LegacyDFU:
    """A LegacyDFU instance wired to the mock BleakClient."""
    return LegacyDFU(mock_ble_client)
