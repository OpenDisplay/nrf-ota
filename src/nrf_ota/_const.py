"""Nordic Legacy DFU protocol constants, exceptions, and callback types."""

from __future__ import annotations

import sys
from collections.abc import Callable

# UUIDs

LEGACY_DFU_SERVICE_UUID = "00001530-1212-efde-1523-785feabcd123"
LEGACY_DFU_CONTROL_POINT_UUID = "00001531-1212-efde-1523-785feabcd123"
LEGACY_DFU_PACKET_UUID = "00001532-1212-efde-1523-785feabcd123"
LEGACY_DFU_VERSION_UUID = "00001534-1212-efde-1523-785feabcd123"

BUTTONLESS_SERVICE_UUID = "8ec90003-f315-4f60-9fb8-838830daea50"
BUTTONLESS_CP_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"

# Command op-codes

OP_START_DFU: int = 0x01
OP_INIT_DFU_PARAMS: int = 0x02
OP_RECEIVE_FW: int = 0x03
OP_VALIDATE_FW: int = 0x04
OP_ACTIVATE_N_RESET: int = 0x05
OP_PACKET_RECEIPT_NOTIF_REQ: int = 0x08

# Response / notification opcodes

OP_RESPONSE: int = 0x10           # CP notification: response to a command
OP_PKT_RECEIPT_NOTIF: int = 0x11  # CP notification: PRN receipt

# Response status codes

RSP_SUCCESS: int = 0x01
RSP_INVALID_STATE: int = 0x02    # from validate = m_data_received != m_image_size (incomplete image); NOT a success
RSP_OP_FAILED: int = 0x06        # firmware upload failed (e.g., macOS flow control)

TYPE_APPLICATION: int = 0x04

# Platform defaults

DEFAULT_PRN: int = 8 if sys.platform == "darwin" else 10
"""Packets per receipt notification; 8 on macOS (CoreBluetooth flow control limit), 10 elsewhere."""

# Callback types

ProgressCallback = Callable[[float], None]
LogCallback = Callable[[str], None]

# Exceptions


class DFUError(Exception):
    """Raised when the DFU process cannot complete."""


class DeviceNotFoundError(DFUError):
    """Raised when the target BLE device cannot be located."""
