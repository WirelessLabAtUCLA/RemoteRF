# Isolated dev integration route fixture — never hardware qualification

This separate test-only package is under `tools/qualification`, outside every
production package/wheel. It never imports native UHD or opens radio hardware.
It supplies in-memory UHD-shaped helpers/streams and a benign supervised Python
child so the unchanged generic controller, schema gate, reservation authority,
relay/direct routing and cleanup paths can be exercised together on dev Global.
No authentication, receipt validation or ownership gate is bypassed in core.

The launcher refuses unless all are true:

- Hostname is exactly `remoterf-global-dev`, service user is `testlab`, and
  `REMOTERF_DEV_INTEGRATION_FIXTURE=1` is explicit.
- `XDG_CONFIG_HOME=/opt/remoterf-integration-canary-20261109/config` and
  `REMOTERF_CONFIG_DIR` points at its `remoterf` child (no production config).
  Keep the user's existing HOME; do not repurpose it.
- Inventory contains only the existing SimSDRs 95–99 and both synthetic USRPs:
  gid 401/serial `DEV_INTEGRATION_01`, gid 402/serial `DEV_RAW_02`, each with
  `profile: usrp2901` (optional `type: b200`). Real/ambiguous selectors are refused.

Build a standalone wheel and install it only into the disposable canary venv:

```sh
python -m build --wheel tools/qualification/dev_fixture
CANARY_PY=/opt/remoterf-integration-canary-20261109/venv/bin/python
"$CANARY_PY" -m pip install --no-deps /path/remoterf_dev_integration_fixture-0.1.3-py3-none-any.whl
```

Run `rrf-integration-dev-server` as a delegated transient systemd unit named
`remoterf-integration-canary-20261109.service`, `User=testlab`,
`Delegate=yes`, `DelegateSubgroup=manager`. Set the two config variables and
explicit fixture flag above. The entry point keeps ordinary server arguments
`--serve --headless`, creates no booking, modifies no Global account/approval,
and does not read credentials. The root coordinator leases dev, clones state,
pauses the original testlab, approves only dev hashes through the normal admin
interface, creates/cancels real dev reservations, and restores the old service.
Never run this simultaneously with the original same-identity/port testlab.

`REMOTERF_DEV_INTEGRATION_ENABLE=0` is the default. With
`REMOTERF_DEV_INTEGRATION_BROKEN=1`, the disabled manifest names a missing package.
With explicit enablement `1`, the guarded launcher uses its delegated parent to
prepare `apps`, creates a clearly labelled **synthetic, non-radio** receipt bound
to the exact package/framework/schema and fake UHD version
`dev-fixture-no-hardware`, and enables gid 401 only. Gid 402 remains raw-only.
These synthetic assertions exercise the receipt format; they are not evidence
that any physical-radio check passed and must never become deployment approval.
All generated files stay in the cloned config; runtime authority remains real.

Adapter `org.remoterf.dev_fixture`, profile `test`, actions:

- `echo`: any bounded plain-object parameters, returns echo plus per-instance call
  count. Repeated request keys must retain the same result/count via the runtime.
- `health`: empty parameters; verifies the supervised child is still alive.

Configuration: `start_delay` 0–10s, `stop_delay` 0–35s, `exit_after` 0–10s (zero keeps
child alive), and boolean `fail_start`/`fail_stop`. Defaults are zero/false.
`fail_start` occurs after child readiness; `exit_after` produces a real child exit.
The worker is marked `required_while_running=True`: its exit must trigger the
runtime's cached resource failure probe even without a client action.
`stop_delay` can exceed the runtime deadline while ownership stays fenced.
`fail_stop` intentionally leaves recovery-required state after child cleanup;
use only when the coordinator is prepared to reconcile that test owner explicitly.
No remote input selects command, environment, directory or radio serial.

Synthetic RX returns a finite complex tone (at most 4096 samples per native call);
synthetic TX consumes memory only. The stock schema/hash is unchanged. This is
transport/control compatibility evidence, not RF, timing, TX or UHD qualification.

`tests/loopback_probe.py` runs the normal server driver loader, then exercises
the selected driver through generated-client gRPC control, finite RX, memory-only
TX and cleanup. Its fake unit-test authority is outside the installed wheel.
`tests/client_compat_probe.py` accepts an exported schema directory and a result
JSON path; run it under each published client's own isolated environment. It
checks all four USRP/Phaser schemas, generic method binding and stale-cache
refresh without accessing a deployment or reservation.
