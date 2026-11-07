# Per-device transmit frequency limits

The lab server owns the policy. With no `limits` block, device behavior is unchanged.
Ranges are inclusive Hz; blacklist entries take precedence. Omitting `whitelist`
permits frequencies outside the blacklist. An explicit empty whitelist, unknown
field, malformed range, or unsupported violation mode disables TX for that device
and prints an administrator-facing configuration error. Reads and RX remain available.

Add local devices' limits to their existing entry in the server's `devices.yml`
(or `devices.yaml`):

```yaml
devices:
  - device_id: 3
    device_type: adalm_pluto
    name: bench_pluto
    init:
      serial: YOUR_SERIAL
    limits:
      tx_freq:
        whitelist: [[2.40e9, 2.48e9], [902e6, 928e6]]
        blacklist: [[2.45e9, 2.46e9]]
```

Host-attached devices use a separate overlay in the **server's** same file. This
never creates a local device or opens the host's hardware on the server:

```yaml
host_devices:
  - host_id: lab-host
    device_id: 7
    limits:
      tx_freq:
        whitelist: [[902e6, 928e6]]
```

Use the authenticated host ID and device GID shown in the host directory. The
policy is bound to both identifiers, retained across directory refreshes, and
sent over the authenticated server-to-host tunnel. Upgrade the Host to 1.3.9 or
later before using a restricted device; the server refuses an older Host before
forwarding an operation. The new Host also enforces the policy on direct client
connections. A direct client cannot replace or remove the server's policy.

Restart/reload the server after editing local inventory. Configure limits before
starting reservations; changing policy does not recall samples already queued in
hardware or stop an existing cyclic transmission. Host direct grants install the
current policy before issue/refresh; a failed policy handshake refuses the grant.

## Behavior and supported operations

Only `on_violation: reject` is supported (and it is the default). `default` mode
is refused as invalid configuration, so clients cannot silently transmit at a
substituted frequency. Rejections list the requested frequency and configured
whitelist/blacklist. No client upgrade is needed to display ordinary device errors.

- Pluto and Pluto MIMO: check `tx_lo` changes and the current LO before TX/DDS
  activation. A stale or initial disallowed LO cannot be used by calling `tx()`.
- HackRF: allow RX tuning while idle, check current center frequency at TX start,
  and check retuning while TX is active, including alternate RPC call forms.
- USRP: check legacy and Dynamic v2 `set_tx_freq`, the frequency in
  `send_waveform`, and actual stream channels before sending TX sample frames.
  Structured TuneRequests must contain exactly one automatic target; ambiguous
  targets, manual RF/DSP policies and LO offsets are refused while limited.
  Low-level LO/subdevice/clock changes, property-tree/peripheral access, GPIO and
  register writes, and scheduled command-time changes are unavailable while
  limited. These controls can bypass ordinary frequency setters. RX/TX streamer
  handles remain available. Without limits, the native UHD API is unchanged.

Checks and TX dispatch share device/control locks. Tests use real schema dispatch
with simulated radios and serialized host requests; physical RF behavior still
requires hardware validation. Limits constrain center frequency, not occupied
bandwidth, output power, hardware spurs or frequency accuracy.
