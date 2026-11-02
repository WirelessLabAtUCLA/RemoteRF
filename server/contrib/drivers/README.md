# Contributed device drivers

Drivers maintained here but **not** shipped or seeded by `remoteRF-server`:
nothing in this folder is installed automatically. A deployment that has the hardware
copies the file it needs into its drivers directory and restarts the server.

| File | `device_type` | Hardware | Host packages |
|---|---|---|---|
| `bluetooth_schema.py` | `bluetooth` | any USB Bluetooth dongle BlueZ supports (CSR8510, RTL8761B, ...); a built-in adapter with `allow_fallback` | `bleak`, `dbus-fast`, `bluetooth-numbers` (optional: vendor and service names) |
| `gps_schema.py` | `gps` | any NMEA-0183 USB receiver (u-blox 7/8/M9/M10 auto-detected; others need `port`) | `pynmea2`, `pyserial` |
| `sim_sdr_schema.py` | `sim_sdr` | none: simulated receiver for test labs (`tools/dev-smoke`) | none |

Both are Linux-only in practice: Bluetooth needs BlueZ on the system D-Bus, and
the GPS reads a serial port (`/dev/ttyACM*`).

## Install

Into the Python environment the server runs in:

```bash
pip install bleak dbus-fast bluetooth-numbers   # Bluetooth
pip install pynmea2 pyserial                    # GPS
```

Then copy (or symlink) the driver and restart the server:

```bash
cp contrib/drivers/gps_schema.py ~/.config/remoterf/drivers/
cp contrib/drivers/bluetooth_schema.py ~/.config/remoterf/drivers/
```

## `~/.config/remoterf/devices.yml`

```yaml
devices:
  - device_id: 10
    device_type: bluetooth
    name: Bluetooth CSR8510
    init: {}                        # auto-detect the USB dongle

  - device_id: 20
    device_type: gps
    name: USB GPS
    init:
      port: /dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00
      baud: 9600
```

Bluetooth `init` keys:
- `adapter` (optional, e.g. `hci1`): pin one adapter by name. Leave it out so
  the dongle's current `hciN` is looked up each time the device is opened, and
  again when BlueZ no longer knows the old object (a replug renumbers it).
  Auto-detection takes a CSR8510 first, then any other USB dongle.
- `allow_fallback` (default `false`): without `adapter`, the device stays
  offline when no dongle is plugged in rather than taking the machine's
  built-in adapter (one not on USB, on a port the kernel marks `fixed`, or
  Intel's). Set `true` to accept it.

GPS `init` keys: `port` (default: auto-detect any u-blox, or a port whose USB
name says GPS/GNSS; other serial ports are never probed, so a receiver behind a
generic USB-serial chip such as PL2303, CP210x or CH340 needs `port`), `baud`
(9600; SiRF receivers such as the BU-353S4 use 4800), `timeout` (1.0 s). After an unplug the driver reopens the **same port
path** every second, so prefer a `/dev/serial/by-id/...` path: `ttyACMn` can
change on a replug. Until a new fix arrives the last position is kept and
reported with `stale: true` (`connected` says whether the receiver is there).

## Versions

`driver_version` 0.1.2. The Bluetooth driver wraps its lists of dicts (scan
results) in `_JsonList` so that servers up to 1.3.0 send them as JSON; servers
from 1.3.1 do that themselves, and the wrapper stays for the older ones.
