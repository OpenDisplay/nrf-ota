# Legacy DFU through an ESPHome Bluetooth proxy

Findings from testing in October 2026, and the reasoning behind `nrf_ota.esphome_proxy`.

## Test setup

- **Device:** Seeed XIAO nRF52840 Plus (OpenDisplay 7.3" Color Kit). The bootloader reports `0.9.2-29-g6a9a6a3 s140 7.3.0`, DFU version 0.8.
  - Seeed ships plain upstream Adafruit nRF52 bootloader commit `6a9a6a3` (2025-06-30, board `xiao_nrf52840_ble`). There is no Seeed fork.
- **Proxy:** ESP32 Bluetooth proxy running ESPHome 2026.9.1, with the device and bootloader at about −50…−75 dBm.
- **Firmware:** alternating flashes of OpenDisplay 2.26.7 and 2.27.0, about 275 KB each. Every run was verified afterwards by reading the version back from the app.

## What this bootloader version does

- **Packet characteristic:** the DFU Packet characteristic is **write-without-response only**, and the MTU stays at 23 (the app gets 247 over the same proxy). Adafruit 0.10.0 (January 2026) added write-with-response and MTU 247, but fielded Seeed units don't have it.
- **No timeouts:** there is no DFU inactivity timer. A stuck DFU session stays stuck until reset.
- **The app is erased at START.** From then on, any failure leaves the device without a valid app. It boots into UF2 USB mode (steady red LED, `XIAO-BOOT` drive), and only a USB flash recovers it.
- **A failed transfer can't be retried over BLE.** After `RECEIVE_FW` fails with `OPERATION_FAILED` (`10 03 06`), every later `START` gets `INVALID_STATE` (`10 01 02`). `SYS_RESET` then lands in USB mode.
- **Activate is fragile.** On `ACTIVATE` the bootloader disconnects and writes its settings page (`pstorage_clear` + `pstorage_store`). Any flash error there goes through `APP_ERROR_CHECK`, which resets the chip with the settings erased, so the new image is marked invalid. This version has neither the `NO_MEM` retry nor the vector-table fallback that upstream added later.
- **Packet writes before START are ignored** (no packet type is selected yet). That makes them a safe probe.
- **The validated state survives a host disconnect.** The bootloader stays in `WAIT_4_ACTIVATE` and advertises again, so activate can be sent on a new connection.

## What goes wrong through a proxy

1. **Dropped packets.** bleak has no backpressure for write-without-response through bleak-esphome. Packets get queued as fast as the caller writes them, and the bootloader loses them once its small receive buffer overflows. No fixed `inter_packet_delay` (0.01–0.05 s) was reliable.
2. **Activate doesn't commit.** Even with a byte-perfect image (flash dump identical, validate OK), only **4 of 8** activates committed. The rest left the device in USB mode. Signal strength made no difference: the strong-signal runs failed as often as the weak ones. Direct from a Mac it committed 4 of 4.
3. **Setup flakiness.** Connecting to the app through the proxy often fails with GATT error 133. Most runs needed at least one retry, so callers need connect retries.
4. **The proxy can wedge.** After hours of use the proxy refused every connection (error 133 or timeouts, with free slots shown as 3/3) until it was power-cycled.

## What fixed it

**Pacing by proxy acknowledgements.** The proxy answers *every* write, including write-without-response, with a `BluetoothGATTWriteResponse` or `BluetoothGATTErrorResponse`. aioesphomeapi just doesn't wait for it on a no-response write. Counting those replies gives real flow control:

| Writes in flight | Receipt (PRN) every | Transfer (275 KB) | Result |
|---|---|---|---|
| 1 | 10 | ≈450 s | 0 drops in every run |
| **2** | **10** | **≈290 s** | 0 drops |
| 4 | 10 | ≈290 s | 2 passed; 1 of 2 overflowed when the transfer interval was changed to 15–30 ms |
| 2 | 30 | 267–291 s | 0 drops, no real gain |
| 4 | 30 | aborted at 30.6 KB | `OPERATION_FAILED` |
| 8 | 10 | aborted at 9.6 KB | `OPERATION_FAILED` |

The library uses **2 writes in flight and a receipt every 10 packets**. With 4 in flight the bootloader only kept up thanks to the pauses at each receipt, so 4 has no safety margin.

**Activating on a fresh, long-interval connection.** After validate: disconnect, reconnect, set the connection interval to 100 ms (`80, 80, 0, 400` in BLE units), wait 2 s, then activate.

| Before activate | Committed |
|---|---|
| Nothing (proxy default 7.5–11.25 ms interval) | 4 / 8 |
| 5 s delay | 1 / 3 |
| 30 ms interval (with or without reconnect) | 10 / 12 |
| **Reconnect + 100 ms interval** | **13 / 13** |

The interval matters because the settings flash write runs in radio-idle time, and the proxy's short default interval leaves very little of it. The reconnect's own contribution was never isolated, because all the 100 ms runs also reconnected.

**Refusing unsafe proxies before anything is erased.** `proxy_safe_dfu` requires the proxy's `CONNECTION_PARAMS_SETTING` feature flag. It also sends one probe write to the Packet characteristic before `START` and refuses if the proxy doesn't acknowledge it.

## Open questions

- **Why the direct path is faster.** From a Mac the same image goes unpaced in 143 s (≈1.9 KiB/s), so the bootloader isn't what caps the proxy path at ≈1 KiB/s. A 15–30 ms interval during the transfer did *not* help with 4 in flight: 1 run passed and 1 overflowed at 24.2 KB. The remaining explanation is untested:
  - every acknowledgement crosses Wi-Fi (20–30 ms median round trip), so 2 in flight is latency-bound;
  - larger windows let the ESP32 forward queued writes in bursts that overflow the bootloader's receive buffer, while macOS flow control is local and smooth.
  - Possible next tests: 4 in flight with a minimum gap between writes, or pacing done inside ESPHome itself.
- **Whether the reconnect is needed** on top of the 100 ms interval.
- **Other bootloaders.** Adafruit 0.10.0+ (write-with-response, MTU 247, `NO_MEM` handling, vector-table fallback) should be much more robust, but it hasn't been tested.

## Recovering a device

- **Bootloader still advertising (`AdaDFU`, at app MAC + 1) and the transfer never started:** send `SYS_RESET` (`0x06`) to the control point. The app is still intact.
- **Steady red LED, or a `XIAO-BOOT` drive:** copy the matching `.uf2` onto the drive. macOS's "could not copy extended attributes" message is harmless. The `CURRENT.UF2` on the drive is a dump of flash 0x1000–0xEA000; the app starts at 0x27000.
- **LED off, nothing advertising, no drive:** a single reset may not be enough. A full power cycle (battery off and on) brought it back to USB mode.
