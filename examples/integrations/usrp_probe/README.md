# Receive-only USRP handoff probe

This separately packaged adapter exercises RemoteRF's generic integration
contract without OAI. Installation and metadata discovery do not import UHD,
open a radio or enable integrations. It runs only where the operator enables it
in `integrations.json`.

The `receive` profile selects one RX channel using the runtime-provided serial.
It starts a separately supervised Python process, receives a finite burst of
complex samples, verifies finite data and the actual reported serial, then keeps
receiving periodic bursts while it owns the radio. There is no transmit path.
The `health` action takes an empty object and checks whether the owned process is
still alive. Stop uses the runtime's cgroup service and requires verified process
release. Only the runtime releases the radio lease and reacquires native UHD.

Configuration fields are `rate` (1 kS/s–2 MS/s), `frequency` (1 MHz–6 GHz), `gain`
(0–10 dB), `samples` (64–65,536), `channel` (0 or 1), and `startup_timeout`
(1–20 seconds). Hardware-specific limits still apply. Defaults are 1 MS/s,
100 MHz, 0 dB, 4,096 samples, channel 0 and a 15-second readiness wait.
Configuration cannot select another device, executable, environment or path.

The operator must install this package and suitable native UHD/NumPy into the
worker's Python environment. This package deliberately does not install or
replace UHD. The runtime must provide an absolute owned work directory, exact
`{"serial": "..."}` selector and Linux cgroup v2 process supervision. A process
failure leaves ownership fenced until verified cleanup. Process supervision
covers owned descendants; it does not prove cleanup of arbitrary external files,
interfaces or shared memory. This sample does not create those resources.

Build/install in an isolated test environment, then run `smoke.py` to
check metadata, the disabled default and supervision requirements without a
radio. Run `tests/test_usrp_probe.py` for fake-process contract checks. Neither
exercises a physical radio.

The explicit hardware harness is `tools/qualification/usrp_handoff.py` at the
repository root. Its provider contract, real-reservation requirement, injected
faults and limitations are documented alongside that script. Even a passing
result does not modify deployment configuration or enable the adapter.
