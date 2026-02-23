"""CLI entry point for nrf-ota.

Usage::

    # via uvx (no install required):
    uvx nrf-ota firmware.zip

    # via python -m:
    python -m nrf_ota firmware.zip

    # installed:
    nrf-ota firmware.zip
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak.backends.device import BLEDevice

from . import _load_zip, perform_dfu
from ._const import DEFAULT_PRN, DeviceNotFoundError, DFUError
from ._zip import DFUZipInfo
from .scan import _discover_with_adv


def _print_progress(pct: float, *, width: int = 40) -> None:
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    print(f"\r  [{bar}] {pct:5.1f}%", end="", flush=True)
    if pct >= 100:
        print()


def main() -> None:
    """Synchronous entry point required by ``[project.scripts]``."""
    asyncio.run(_async_main())


async def _async_main() -> None:
    parser = argparse.ArgumentParser(
        prog="nrf-ota",
        description="Flash Nordic Legacy DFU firmware to an nRF5x device over BLE.",
    )
    parser.add_argument("zip_path", help="Path or HTTP(S) URL to the Nordic DFU ZIP file")
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="BLE scan timeout (default: 5 s)",
    )
    parser.add_argument(
        "--prn",
        type=int,
        default=DEFAULT_PRN,
        metavar="N",
        help=f"Packets per receipt notification (default: {DEFAULT_PRN} on this platform).",
    )
    parser.add_argument(
        "--device",
        metavar="ADDR_OR_NAME",
        help="Skip the device picker. Pass a full Bluetooth address or exact device name (case-insensitive).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all output except errors.",
    )
    args = parser.parse_args()

    def on_log(msg: str) -> None:
        print(f"  {msg}", flush=True)

    try:
        # Pre-resolve firmware (fail fast before 5 s BLE scan)
        if not args.quiet and args.zip_path.startswith(("http://", "https://")):
            print(f"Downloading {args.zip_path} …")
        zip_info: DFUZipInfo = await _load_zip(
            args.zip_path,
            on_progress=None if args.quiet else _print_progress,
        )
        if not args.quiet:
            ver_str = f" — v{zip_info.app_version}" if zip_info.app_version is not None else ""
            crc_str = f" — CRC {zip_info.crc16:#06x} ✓" if zip_info.crc16 is not None else ""
            print(f"Firmware: {zip_info.bin_file} ({len(zip_info.firmware):,} bytes){ver_str}{crc_str}")

        # Scan
        if not args.quiet:
            print(f"Scanning for BLE devices ({args.timeout:.0f} s)…")
        raw_scan = await _discover_with_adv(args.timeout)

        # Prefer the live advertisement name over the cached device.name so that after a
        # successful flash the device shows as "OD*" rather than the stale "AdaDFU".
        devices: list[tuple[BLEDevice, str]] = [
            (dev, adv.local_name or dev.name or dev.address)
            for dev, adv in raw_scan.values()
            if adv.local_name or dev.name
        ]

        if not devices:
            print("No named BLE devices found.", file=sys.stderr)
            sys.exit(1)

        # --device matching (non-interactive)
        if args.device:
            needle = args.device.strip().upper()
            matches = [
                (dev, name) for dev, name in devices
                if dev.address.upper() == needle or name.upper() == needle
            ]
            if not matches:
                print(f"No device found matching '{args.device}'.", file=sys.stderr)
                print("\nAvailable devices:", file=sys.stderr)
                for dev, name in devices:
                    print(f"  {name}  ({dev.address})", file=sys.stderr)
                sys.exit(1)

            selected, selected_name = matches[0]
            if not args.quiet:
                print(f"Selected: {selected_name}  ({selected.address})")

        else:
            print(f"\nFound {len(devices)} device(s):")
            for i, (dev, name) in enumerate(devices):
                print(f"  [{i}] {name}  ({dev.address})")

            # Device picker
            selected_index: int | None = None
            while selected_index is None:
                try:
                    raw = input(f"\nSelect device [0–{len(devices) - 1}]: ").strip()
                    idx = int(raw)
                    if 0 <= idx < len(devices):
                        selected_index = idx
                    else:
                        print(f"  Please enter a number between 0 and {len(devices) - 1}.")
                except ValueError:
                    print("  Please enter a number.")
                except (EOFError, KeyboardInterrupt):
                    print("\nAborted.")
                    sys.exit(0)

            selected, selected_name = devices[selected_index]
            print(f"\nSelected: {selected_name}  ({selected.address})")

        # DFU
        if not args.quiet:
            print()
        await perform_dfu(
            zip_info,
            selected,  # BLEDevice
            on_progress=None if args.quiet else _print_progress,
            on_log=None if args.quiet else on_log,
            packets_per_notification=args.prn,
        )
        if not args.quiet:
            print("\nUpdate complete.")

    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    except DeviceNotFoundError as exc:
        print(f"\nDevice not found: {exc}", file=sys.stderr)
        print("  → Press reset on the device and try again.", file=sys.stderr)
        sys.exit(1)
    except DFUError as exc:
        print(f"\nDFU failed: {exc}", file=sys.stderr)
        msg = str(exc).lower()
        if "0x06" in msg or "operation failed" in msg:
            print("  → Try a lower --prn value (e.g. --prn 4).", file=sys.stderr)
        if "0x06" in msg or "operation failed" in msg or "timeout" in msg:
            print("  → The bootloader may still be active — run the command again immediately.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\nUnexpected error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
