# Contributed device drivers

Reference copies of contributed drivers. Only `sim_sdr_schema.py` is contrib-only
(not shipped or seeded by `remoteRF-server`). Both `bluetooth_schema.py`
(`remoterf-device-core >= 0.1.10`) and `gps_schema.py` (`remoterf-device-core >= 0.1.16`)
are built into `remoterf-device-core`, so their copies here are only for older
servers/hosts — copying them into `~/.config/remoterf/drivers/` on a newer
server shadows future built-in upgrades.

| File | `device_type` | Hardware | Host packages |
|---|---|---|---|
| `bluetooth_schema.py` | `bluetooth` | any USB Bluetooth dongle BlueZ supports (CSR8510, RTL8761B, ...); a built-in adapter with `allow_fallback` | `bleak`, `dbus-fast`, `bluetooth-numbers` (optional: vendor and service names) |
| `gps_schema.py` | `gps` | any NMEA-0183 USB receiver (u-blox 7/8/M9/M10 auto-detected; others need `port`) | `pynmea2`, `pyserial` |
| `sim_sdr_schema.py` | `sim_sdr` | none: simulated receiver for test labs (`tools/dev-smoke`) | none |

Both are Linux-only in practice: Bluetooth needs BlueZ on the system D-Bus, and
the GPS reads a serial port (`/dev/ttyACM*`).

## Install

On a current server (`remoterf-device-core >= 0.1.10` for Bluetooth, `>= 0.1.16`
for GPS), both drivers are already built in; only install their optional Python
packages into the server's environment and restart the server:

```bash
pip install 'remoteRF-server[bluetooth]'        # Bluetooth (bleak, dbus-fast, bluetooth-numbers)
pip install 'remoteRF-server[gps]'              # GPS (pynmea2, pyserial)
```

Only copy a file into `~/.config/remoterf/drivers/` when running `sim_sdr_schema.py`
or an older server/host whose `remoterf-device-core` does not yet ship that driver
(copying a built-in driver into `drivers/` shadows future upgrades):

```bash
cp contrib/drivers/sim_sdr_schema.py ~/.config/remoterf/drivers/
# Older servers only (< 0.1.16 for GPS, < 0.1.10 for Bluetooth):
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
`wait_for_fix` waits at most 20 s (it holds the device meanwhile).

`gps_schema.py` is the server's built-in GPS driver
(`remoterf_device_core.drivers.gps.gps_schema`) with only the `idl` import
changed, so a server or host with `remoterf-device-core >= 0.1.16` does not need
this copy; keep the two files identical.

## Versions

- **Bluetooth** (`driver_version` 0.1.4): built into `remoterf-device-core >= 0.1.10`
  (`remoterf_device_core.drivers.bluetooth`), so a server or host on 0.1.10 or
  newer does not need this copy. The driver wraps its lists of dicts (scan
  results) in `_JsonList` so that servers up to 1.3.0 send them as JSON; servers
  from 1.3.1 do that themselves, and the wrapper stays for the older ones.
- **GPS** (`driver_version` 0.2.0): built into `remoterf-device-core >= 0.1.16`
  (`remoterf_device_core.drivers.gps`), so a server or host on 0.1.16 or newer
  does not need this copy.
- **Sim SDR** (`sim_sdr_schema.py`): contrib-only; not shipped built in.
