"""Nordic Legacy DFU protocol constants, exceptions, and callback types."""

from __future__ import annotations

from collections.abc import Callable

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
