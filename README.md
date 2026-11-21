<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://remoterf.net/images/remoterf_logo_dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="https://remoterf.net/images/remoterf_logo_light.svg">
  <img alt="RemoteRF" src="https://remoterf.net/images/remoterf_logo_light.svg" width="320">
</picture>

<h3>A Sandbox for Wireless Research. Accessible from Anywhere.</h3>

<p>Transmit and receive with actual software-defined radios over the internet as if they were connected directly to your computer.</p>

<p>
  <a href="https://pypi.org/project/remoterf/"><img src="https://img.shields.io/pypi/v/remoterf?label=remoterf" alt="PyPI - remoterf"></a>
  <a href="https://pypi.org/project/remoterf-server/"><img src="https://img.shields.io/pypi/v/remoterf-server?label=remoterf-server" alt="PyPI - remoterf-server"></a>
  <a href="https://pypi.org/project/remoterf-host/"><img src="https://img.shields.io/pypi/v/remoterf-host?label=remoterf-host" alt="PyPI - remoterf-host"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="Python >= 3.10">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue" alt="License - GPL-3.0"></a>
  <a href="https://arxiv.org/abs/2606.22161"><img src="https://img.shields.io/badge/arXiv-2606.22161-b31b1b" alt="arXiv 2606.22161"></a>
</p>

<p>
  <a href="https://remoterf.net">Website</a> ·
  <a href="https://remoterf.net/docs/client/">Documentation</a> ·
  <a href="https://remoterf.net/devices/">Devices</a> ·
  <a href="https://remoterf.net/deployments/">Deployments</a> ·
  <a href="https://remoterf.net/terms/">Terms of Service</a> ·
  <a href="https://forms.gle/bQcR1G884BeJtSXa9">Feedback</a>
</p>

</div>

---

## Overview

RemoteRF was developed by Ethan Ge and Ian Roberts in the [Wireless Lab at UCLA](https://wireless.ee.ucla.edu) to democratize access to software-defined radios in wireless research and education. RemoteRF lowers the barrier to entry for instructors, students, and researchers by making shared SDR hardware remotely accessible from virtually anywhere over a network. Users write and run Python code on their personal machines, transmitting and receiving with real SDRs as if they were connected directly to their computer.

Beyond standalone lab deployments, servers can also connect to the RemoteRF Global network to share hardware across partner institutions or make devices publicly accessible worldwide.

## How It Works

A typical RemoteRF deployment consists of one server, multiple devices, many clients, and optionally one or more hosts.

```text
+-----------------------+                         +-------------------------+        USB        +------------------+
|    Client Machines    |                         |     RemoteRF Server     | ----------------> |  Local Devices   |
|  (remoterf CLI / SDK) | <--- Networked RPCs --> |  (serverrf on Linux)    |                   | (SDRs, Radars…)  |
|   Local Python Code   |                         | Accounts · Reservations |                   +------------------+
+-----------------------+                         | Safeguards · Lockouts   |
                                                  +------------+------------+
                                                               |
                                                      LAN / Wi-Fi / VPN
                                                               |
                                                               v
                                                  +-------------------------+        USB        +------------------+
                                                  |      RemoteRF Host      | ----------------> |  Remote Devices  |
                                                  | (hostrf, e.g. RPi SBC)  |                   | (Distributed RF) |
                                                  +-------------------------+                   +------------------+
```

- **Device** — A RemoteRF device is an SDR, multi-modal peripheral, or virtually any instrument (or even software process) that can be controlled through Python.
- **Server** (`remoterf-server` / `serverrf`) — The RemoteRF server is a Linux computer with one or more connected devices, typically deployed on a university campus or in a research lab. It manages accounts, reservations, user groups, and server-side RF lockouts, and executes device calls on behalf of clients.
- **Client** (`remoterf`) — A RemoteRF client connects to the RemoteRF server and enables a user to remotely access the server's devices through Python via a network connection. User scripts run locally on the user's personal computer—only device interactions are routed to the server as RPCs—so clients require no vendor-specific hardware drivers, VMs, or SSH access.
- **Host** (`remoterf-host` / `hostrf`) — An optional component that links one or more devices to the server over Wi-Fi, LAN, or VPN instead of a direct wired connection like USB. A host can be a lightweight single-board computer such as a Raspberry Pi, making it easy to distribute devices across a room, building, or campus.

## Key Features

- **RF Safeguards** — Software lockouts limit device operation to protect hardware, manage interference, and comply with regulations.
- **Exclusive Reservations** — Users make reservations to get exclusive access to SDRs during desired time slots.
- **Managed Access** — User groups and enrollment codes provide users with unique permissions and privileges.
- **Extensible Hardware Schemas** — RemoteRF uses a schema-driven driver architecture so anyone can add support for new SDRs or lab instruments by writing a [Python device schema](https://remoterf.net/docs/server/device-setup/custom/).

## Quick Start

### Begin Using RemoteRF

Reserve an SDR and access it through standard Python scripting.

1. **Install**
   ```bash
   pip install remoterf
   ```
2. **Login**
   ```bash
   remoterf --login
   ```
3. **Run**
   ```python
   from remoterf.drivers.adalm_pluto import adi

   sdr = adi.Pluto(token="Pluto_00@ucla")
   sdr.rx_lo = int(915e6)
   sdr.tx(tx_waveform)
   iq = sdr.rx()
   ```

See the [User Guide](https://remoterf.net/docs/client/) and [`client/README.md`](client/README.md) for full CLI and Python SDK documentation.

### Deploy Your Own Node

Turn a Linux machine and a few SDRs into a shared testbed.

1. **Install**
   ```bash
   pip install remoterf-server
   ```
2. **Configure**
   ```bash
   serverrf devices add
   ```
3. **Serve**
   ```bash
   serverrf --serve
   ```

See the [Server Guide](https://remoterf.net/docs/server/) and [`server/README.md`](server/README.md) for server setup and configuration. To attach remote radios over LAN, Wi-Fi, or VPN (for example, on a Raspberry Pi), install `remoterf-host` (`hostrf`) and see [`host/README.md`](host/README.md).

## Supported Devices

RemoteRF natively supports a range of SDRs, radars, and peripherals, and can be extended to support custom hardware (see [remoterf.net/devices](https://remoterf.net/devices/)):

- **SDRs:** [ADALM-PLUTO](https://remoterf.net/docs/client/adalm-pluto/), [Pluto+](https://remoterf.net/docs/client/adalm-pluto/#mimo), [USRP](https://remoterf.net/docs/client/usrp/), [HackRF One](https://remoterf.net/docs/client/hackrf/), [KrakenSDR](https://remoterf.net/docs/client/kraken-sdr/), [RTL-SDR](https://remoterf.net/docs/client/rtl-sdr/)
- **Radars & Peripherals:** [TI AWR6843 Radar](https://remoterf.net/docs/client/ti-mmwave/), [RFbeam K-MD7 Radar](https://remoterf.net/docs/client/rfbeam-kmd7/), [GPS Receiver](https://remoterf.net/docs/client/gps/), [Bluetooth Dongle](https://remoterf.net/docs/client/bluetooth/), [Wi-Fi Dongle](https://remoterf.net/docs/client/wifi-dongle/)
- **Custom Hardware:** [Add Your Own Device](https://remoterf.net/docs/server/device-setup/custom/)

## Use Cases

- **[AI-Driven Wireless](https://remoterf.net/research/ai-wireless/)** — Train and test deep learning models and deploy agents for AI-driven research.
- **[Experimental Evaluation](https://remoterf.net/research/)** — Develop and experimentally evaluate new communication and sensing techniques.
- **[Measurement Collection](https://remoterf.net/research/ota-experiments/)** — Collect over-the-air measurements by transmitting and receiving with actual radios.
- **[Engineering Education](https://remoterf.net/education/)** — Students can master wireless fundamentals by completing lab exercises remotely.

## Repository Structure

This repository holds the full RemoteRF software stack:

| Path | Package | What it is |
|---|---|---|
| [`client/`](client/) | `remoterf` | CLI and Python SDK (`pip install remoterf`) |
| [`server/`](server/) | `remoterf-server` | Lab server: owns radios, accounts, reservations, and safeguards (`pip install remoterf-server`) |
| [`host/`](host/) | `remoterf-host` | Host agent that attaches remote radios (e.g., a Raspberry Pi) to a lab server (`pip install remoterf-host`) |
| [`packages/remoterf-device-core/`](packages/remoterf-device-core/) | `remoterf-device-core` | Shared device protocol, IDL, and built-in driver schemas |
| [`packages/remoterf-federation-core/`](packages/remoterf-federation-core/) | `remoterf-federation-core` | Shared federation protocol library (lab ↔ RemoteRF Global) |

Additional design notes, integration guides, and example workflows are available in [`docs/`](docs/) and [`examples/`](examples/).

## Citation

**RemoteRF: A Global, Open-Source Sandbox for Wireless Research and Education**  
Ethan Y. Ge and Ian P. Roberts · Wireless Lab at UCLA · [Read on arXiv (2606.22161)](https://arxiv.org/abs/2606.22161)

```bibtex
@misc{remoterf_2026,
  title={{RemoteRF}: A Global, Open-Source Sandbox for Wireless
    Research and Education},
  author={Ethan Y.~Ge and Ian P.~Roberts},
  year={2026},
  url={https://arxiv.org/abs/2606.22161},
}
```

## Contributing

If you develop a new device schema, fix a bug, or improve the documentation, we welcome pull requests and issues on GitHub.

Work happens through GitHub issues and pull requests into `main`. Every PR runs the tests for the packages it touches and needs a maintainer's review before it can merge. Releases to PyPI are published by CI, never by hand.

## Acknowledgements

RemoteRF has been generously supported in part by UCLA’s Teaching and Learning Center’s Educational Innovation Grants program.

## Security

See [SECURITY.md](SECURITY.md). Please do not open public issues for vulnerabilities.

## License

RemoteRF is distributed under the terms of the GNU General Public License v3.0 (GPL-3.0); see [LICENSE](LICENSE). You are free to run, study, share, and modify the software for research, teaching, or private lab deployments without licensing fees.

Use of the RemoteRF platform and connected radio hardware is subject to our [Terms of Service](https://remoterf.net/terms/) and applicable spectrum regulations.
