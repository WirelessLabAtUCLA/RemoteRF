# Isolated UCLA dev Global receive-only qualification

`usrp_dev_e2e.py` uses the ordinary client account backend, reservation API,
generated USRP root, generic integration routes and sample transport. It is an
operator qualification runner for one private deployment, not a service or an
integration activation mechanism. It makes no production requests and no TX
calls. Relay is the required route; direct connectivity is outside this run.

The runner is deliberately pinned to:

- Global origin `https://dev.global.remoterf.net`.
- Deployment UUID `c59791c0-7645-4a4a-94d8-55c5551061a9`, slug
  `ucla-integrations-20261110`, display name
  `Isolated UCLA Integration Qualification`.
- Device `40`, physical USRP serial `32C78EE` and the qualified stock v2 schema
  `sha256:7276bf3ab3eefcb55270abe10311fb000a1897a0337b63a7810c7a107306384b`.
- Separately installed `org.remoterf.usrp_probe`, `receive` profile, `health`
  action; RX at 1 Msps, 100 MHz, gain zero, 4096 finite samples per native check.

Before invoking it, the operator must hold the real UCLA reservation throughout
all runs and cleanup, isolate and independently supervise the private lab
service, verify the exact installed wheels/framework/UHD/receipt identities,
and confirm no competing owner on this serial. The client runner cannot prove
the remote build identity from a schema hash alone. The service's independent
startup/configuration/receipt and physical resource evidence are part of the
qualification record. Production services and integration configuration remain
untouched.

Use a dedicated directory owned by the invoking account with mode `0700`, whose
name begins `remoterf-phase1c-dev-client`. Register, verify, log in and enroll a
normal dev Global account there before invoking the runner. The runner resumes
that saved account and selects/binds the same dev backend through the normal
client functions. It never replaces credentials or auth callbacks. The private
home override is process-local; the operator's `HOME` and saved login are not
changed. Keyring and direct paths are disabled for this invocation.

For each receipt mode, stop and cleanly restart only the isolated canary service
using ordinary integration configuration/startup. Configure an actually missing
receipt for `receipt-missing`, an actual tuple mismatch for `receipt-mismatch`,
and the truthful accepted receipt for `enabled`, `cancel` and `expiry`. Do not
manufacture receipt checks or patch server qualification/authentication. The
client verifies the observable refusal; independently retain the precise
configuration that caused each denial.

```sh
python tools/qualification/usrp_dev_e2e.py \
  --mode receipt-missing \
  --deployment-id c59791c0-7645-4a4a-94d8-55c5551061a9 \
  --client-home /tmp/remoterf-phase1c-dev-client-e2e-20261110 \
  --evidence /absolute/private/receipt-missing.json
```

Repeat with `receipt-mismatch` and `enabled`, each with a new evidence path.
All three modes exercise native RX before and after the integration attempt.
Receipt denials must preserve native UHD access. Enabled mode checks discovery,
describe, start/status, duplicate start, native refusal while the application
owns the radio, health/status, duplicate action, stop/release and native RX
reacquisition using the same generated root. Duplicate checks establish stable
operation/action identity, not a packet-loss test.

Calendar modes create and cancel actual reservations through the dev Global API:

```sh
python tools/qualification/usrp_dev_e2e.py \
  --mode expiry \
  --deployment-id c59791c0-7645-4a4a-94d8-55c5551061a9 \
  --client-home /tmp/remoterf-phase1c-dev-client-e2e-20261110 \
  --observation-request /absolute/private/expiry-request.json \
  --observation-confirmed /absolute/private/expiry-confirmed.json \
  --evidence /absolute/private/expiry.json
```

Repeat with `cancel` and fresh paths. `expiry` uses a normal ten-minute calendar
interval with a past start and an end approximately two to three minutes ahead.
It does not modify clocks, reservation rows or authority callbacks. Leave ample
time inside the outer UCLA booking for startup, expiry, independent observation,
next-holder RX and final cleanup. The operator must stop the run safely if its
external booking or supervisor becomes uncertain.

After the application reaches receive readiness, cancellation is performed via
the dev account API or the real calendar expiry is awaited. The runner then
writes the observation request and performs **no device RPCs or root close**
until external proof arrives. An independent observer must inspect the live
canary ownership journal, delegated cgroup identity/population and physical USB
owners, without calling the owning client session or acquiring the USRP. After
at least ten seconds following `quiet_started_at`, write the confirmation JSON
atomically (temporary file followed by rename), copying these exact fields from
the request:

```json
{
  "run_id": "COPY_FROM_REQUEST",
  "mode": "expiry",
  "deployment_id": "c59791c0-7645-4a4a-94d8-55c5551061a9",
  "device_id": 40,
  "serial": "32C78EE",
  "operation_id": "COPY_FROM_REQUEST",
  "reservation_id": "COPY_FROM_REQUEST",
  "observed_at": 1794337200.0,
  "root_identity_verified": true,
  "hardware_owners": 0,
  "populated_owned_scopes": 0,
  "unknown_owned_scopes": 0,
  "observable_usb_owners": 0,
  "usb_observation_scope": "current uid only"
}
```

The timestamp above is illustrative: use the actual observation time. Never
copy zero counts without measuring them. Retain the source observation evidence
separately. Empty, verified retained scope metadata is permitted at this point;
populated or unidentified scopes are not. The runner accepts only a matching,
fresh proof within 180 seconds. Failed or missing proof fails the case; ensuing
safety cleanup is not counted as autonomous release evidence.

The external observer proves an empty ownership journal, empty owned process
scopes and no USB owners visible to the service user. It cannot rule out USB
descriptors held by other users or inspect hidden in-process runtime counters.
The evidence explicitly records both limits. Successful subsequent native RX
provides an additional practical check that the next holder can acquire the radio.

Only after proof is accepted does the runner issue its first post-revocation
device RPC, require the typed reservation/session refusal, close that old client,
create a next dev reservation and prove native receive with a fresh root. The
previous operation must be hidden from the next holder. The runner cancels its
own dev reservations on completion/failure. It does not release the independent
outer UCLA reservation. The operator checks final journals, processes/cgroups,
USB/native ownership and private-service teardown before releasing that booking.

Evidence excludes account credentials and reservation tokens. It records checks,
resource observation, exact routing/device/schema identity and sanitized failure
type/stack locations. A passed client report alone does not qualify another
radio, adapter, hardware alias, transport, wheel build or production activation.

Offline runner checks:

```sh
python -m pytest -q tools/qualification/tests/test_usrp_dev_e2e.py
```

These tests use no network or real hardware. They check destination/private-state
guards, calendar policy/cleanup, receipt refusal without native regression, and
that stale/unproven external observations cannot authorize post-revocation device
calls or a next reservation.
