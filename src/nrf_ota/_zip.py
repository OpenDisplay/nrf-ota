"""Nordic DFU ZIP parsing utilities."""

from __future__ import annotations

import io
import json
import zipfile
from typing import NamedTuple

from ._const import DFUError


class DFUZipInfo(NamedTuple):
    """Parsed contents of a Nordic DFU ZIP file."""

    init_packet: bytes
    firmware: bytes
    bin_file: str
    crc16: int | None
    app_version: int | None  # None if sentinel 0xFFFFFFFF or absent


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE — the variant Nordic uses in DFU manifest init_packet_data."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _parse_zip(z: zipfile.ZipFile) -> DFUZipInfo:
    """Core ZIP parsing logic — shared by :func:`parse_dfu_zip` and :func:`_parse_zip_bytes`."""
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
        crc_computed = crc16_ccitt(firmware)
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
            return _parse_zip(z)
    except zipfile.BadZipFile as exc:
        raise DFUError(f"Invalid ZIP file: {exc}") from exc


def _parse_zip_bytes(data: bytes) -> DFUZipInfo:
    """Parse a Nordic DFU ZIP from in-memory *data* and return a :class:`DFUZipInfo`."""
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as z:
            return _parse_zip(z)
    except zipfile.BadZipFile as exc:
        raise DFUError(f"Invalid ZIP file: {exc}") from exc
