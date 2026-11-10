# Integration qualification

## Synthetic runtime load and soak

`integration_soak.py` runs the real session manager, generic controller, runtime
and durable ownership journal against explicitly fake radios, reservation
authority, qualification receipts and process scopes. Run it in an isolated
Linux environment containing the intended core/server builds. It never enables
deployed integrations or supplies a hardware qualification receipt.

```sh
env -u PYTHONPATH -u REMOTERF_CONFIG_DIR \
  XDG_CONFIG_HOME=/tmp/remoterf-runtime-soak/config \
  XDG_CACHE_HOME=/tmp/remoterf-runtime-soak/cache \
  XDG_DATA_HOME=/tmp/remoterf-runtime-soak/data \
  python tools/qualification/integration_soak.py \
    --state-dir /tmp/remoterf-runtime-soak/new-state \
    --seconds 600 --baseline-seconds 30 --raw-rate 100
```

The state directory must not already exist. `--raw-rate` is the target cycle
rate per independent raw radio; each cycle invokes control, RX and TX methods.
Two fake radios run those calls continuously while two others cycle through
nine application scenarios: normal operation, cancelled startup, held action,
refused action, adapter exception, held stop, local expiry, required-worker exit
and monitor failure. Duplicate starts/actions, native-access refusals, status
poll floods and post-handoff native recovery run alongside those scenarios.
Monitor failure leaves the simulated resource alive until cleanup verifies its
release. Separate preflights exercise bounded owner/action admission and an
injected journal ENOSPC failure; no disk is filled.

Before measurement the harness declares these limits: status p99 at most 50ms;
each independent raw-method p99 at most the greater of 5ms or four times its
raw-only baseline; raw throughput at least 90% of baseline; RSS growth at most
64MiB; thread/file-descriptor growth at most four/eight; and at most 16 retained
owners and 64 actions. The harness records one-second resource samples, exact
call/error/cycle counts, and estimated latency percentiles from bounded uniform
reservoirs of up to 8192 observations. It checks empty ownership/scopes/retained
operations and native recovery at the end. Results and exact source identities
go to `evidence.json`; an incomplete or failed run cannot report success.

Finish other CI stress workloads before taking the baseline and timed run.
These results cover in-process scheduling with fake native methods, not physical
UHD throughput, network routes, deployed authority, real calendar expiry, kernel
process containment or production capacity. Use the separate process and
real-radio harnesses for their respective gates.

## Real-radio handoff

`usrp_handoff.py` is an operator-only hardware harness for one explicitly
reserved lab-local USRP. It never enables deployed integrations, publishes
schema approvals, changes inventory or creates a qualification approval. It
writes a JSON evidence report only after assertions have run; a failed or
uncertain cleanup keeps ownership records for inspection and exits nonzero.

Prerequisites:

- A current reservation for the exact device ID and physical serial, and no
  competing native session or diagnostic process on that radio.
- An isolated environment with the matching core/server branch, the independently
  built `remoterf-example-usrp-probe` wheel and existing compatible native UHD.
- An exclusive delegated cgroup v2 root with `pids`, `memory` and `cgroup.kill`
  available. There is no PID/process-name or unqualified process-group fallback.
- A private state/evidence directory and the external authority verifier below.

Example (replace paths and identities with the actual held reservation):

```sh
python tools/qualification/usrp_handoff.py \
  --serial EXACT_SERIAL --device-id 40 --reservation-id ACTUAL_RESERVATION_ID \
  --authority-command /absolute/operator/reservation-check \
  --cgroup-root /sys/fs/cgroup/operator-delegated/qualification \
  --state-dir /absolute/private/new-run \
  --evidence /absolute/private/new-run/evidence.json
```

The current harness qualifies the explicit `usrp2901`/`b200` profile only. It
checks native RX → separately supervised application RX → native RX, duplicate
start, partial startup failure after actual RX readiness, forced child exit,
a delayed stop that exceeds its deadline, injected local reservation expiry,
and a next local holder. A separate child exits abruptly after actual application
RX, leaving its process behind; the parent verifies restart fencing, reconciles
only the recorded cgroup identity, terminates that owned group, and proves native
RX again. The real external booking remains held during injected expiry and
crash recovery so another actual user cannot acquire the test radio mid-cleanup.

The current probe declares its long-lived receiver `required_while_running=True`.
The forced child-exit case now waits for autonomous runtime cleanup without a
health action; the independent cgroup proof must still verify all descendants
gone. Earlier saved reports identify their exact source hashes and may instead
have used an explicit health action. Changing the monitoring behavior requires
fresh exact-build radio qualification before activation.

The local SessionManager's resolver is deliberately replaced by a cached view of
the externally verified reservation. Evidence labels this route and injected
expiry explicitly. These results do **not** qualify deployed Global authentication,
calendar propagation, relay/direct fallback, real calendar expiry, physical
multi-radio aliases, Phaser, production load or another hardware/profile/version.
Each RX check explicitly sets receive settings; preserved settings or continuous
timebase across handoff are not promised. No TX call is made.

A canonical private per-user/per-serial file lock prevents concurrent harness
runs even when they use different state directories. The restart child inherits
that lock; the parent retains it while reconciling. This is an additional local
coordination lock, not a substitute for the real reservation.

## Reservation authority provider

The required executable is operator-owned and is called with:

```text
reservation-check --reservation-id ID --device-id GID --serial SERIAL
```

It must independently check the current authoritative reservation record,
expected account, device mapping and time. For a held reservation, emit exactly
one bounded JSON object (at most 4,096 bytes):

```json
{"active":true,"reservation_id":"ID","device_id":40,"serial":"EXACT_SERIAL","expires_at":1794272400}
```

`expires_at` is an absolute Unix timestamp in seconds, derived from the real
reservation record. Never emit bearer tokens, password hashes or other secrets.
For refusal/uncertainty return `active:false` or exit nonzero. Do not fabricate an
approval flag, infer possession from an operation ID, or use a stale saved JSON
file as proof of ongoing authority.

The harness refreshes every second, gives the provider two seconds to respond,
and expires cached authority after four seconds or the actual booking end,
whichever comes first. Driver/adaptor callbacks read only this cached state and
perform no network or database wait. Provider failure revokes the local test's
authority; cleanup still runs. Stop/removal of a radio owner never relies on the
provider claiming that a process has exited—the cgroup verifier supplies that
independent proof.

A read-only verifier against the lab's live reservation database is appropriate
when its device mapping/account checks are independently established. The harness
does not choose credentials, bookings or a deployment-specific SQL schema.

## Evidence and recovery

The report records exact device/booking identity, route/fault labels, package
versions, source digests and each passed receive/cleanup assertion. `passed:true`
requires all implemented checks, including the surviving-process restart test.
No approval/configuration file is generated. Framework activation remains an
independent operator/release decision and must remain disabled while any required
check is missing or failed.

Keep failed journals and process registry files. Do not delete them to bypass a
fence or kill a PID copied from a file. Inspect the operator recovery tools,
verify the recorded cgroup/root/boot identity and all resources, then release the
radio journal only after proven cleanup. If the delegated cgroup disappeared or
its identity changed, automatic recovery must refuse and an operator must
reconcile the physical radio separately.

## Delegated cgroup setup

Run as the intended service account, such as `prod-remoterf`, with a writable
delegated cgroup v2 domain. The framework requires `pids`, `memory`,
`cgroup.kill` and `cgroup.max.descendants`; an ordinary writable directory is
insufficient. It does not enable controllers or move the server between groups.
On a supporting systemd user manager, an isolated qualification unit can use:

```sh
systemd-run --user --unit=remoterf-qualification \
  --property=Delegate=yes --property=DelegateSubgroup=manager \
  --property=RuntimeMaxSec=300 --wait --pipe \
  /absolute/python /absolute/operator/delegated-entry.py
```

The operator's entry script must verify that its `/proc/self/cgroup` unified
entry ends in this unit's `manager` subgroup, then enable `+pids +memory` in
the parent unit's `cgroup.subtree_control` and pass that parent as the harness's
`--cgroup-root`. Keeping the server in `manager` leaves the parent free of
processes, as required when enabling domain controllers. The supervisor creates
its own `rrf-*` children beneath that parent. Do not reuse another running
unit's delegation or alter a production service to perform qualification.
Missing delegation or unsupported systemd properties must stop the test.

The process-only CI qualification exercised this setup with systemd 259.
Delegation availability and the exact service account still require checking on
each deployment. Keep the delegated root and its children intact while offline
recovery runs: recreating the same path does not preserve the kernel identity.
A timed-out transient unit is a test containment measure, not proof that its
persisted ownership records have been reconciled.

## Offline managed-resource recovery

For a deployed controller, `STATE` below is the private
`<remoterf-root>/db/integrations` directory, containing `ownership/` and
`processes/`. `ROOT` must be its original configured delegated cgroup root.
These commands do **not** accept the harness's separate `radio-owners/` and
`process-owners/` layout; the harness performs its own explicitly labelled
recovery checks.

```sh
python -m remoterf_device_core.integrations.operator inspect \
  --state-dir STATE --cgroup-root ROOT

python -m remoterf_device_core.integrations.operator recover \
  --state-dir STATE --cgroup-root ROOT \
  --physical-id EXACT_PHYSICAL_ID --lease EXACT_RECORDED_LEASE --timeout 5

python -m remoterf_device_core.integrations.operator cleanup-orphan \
  --state-dir STATE --cgroup-root ROOT \
  --owner-id EXACT_UNREFERENCED_OWNER --timeout 5
```

`inspect` is read-only and can run while the server is active. Its bounded
snapshot omits bearer sessions, environment values and raw process logs; it is
not an atomic authorization or evidence that hardware is free. Before recovery,
stop/drain the controller while preserving its cgroup delegation. Recovery
requires both exclusive writer locks, the exact lease/resource mapping and the
recorded boot/root/scope identities. It kills only that verified cgroup, proves
its processes gone, then releases the hardware lease and removes its scope.
`cleanup-orphan` handles one process owner with no hardware reference and never
clears another hardware lease.

If recovery reports `process_recovery_required` with `physical_released:true`,
process release was verified and the hardware journal was cleared, but final
process metadata cleanup failed. Inspect again; an unreferenced released owner
can be reconciled with `cleanup-orphan`. Missing journals, legacy records with
no resource owner, unknown scopes, changed boot/root identities and incomplete
scope creation all fail closed. There is no force-delete or arbitrary-PID kill
option; retain the records for physical/operator reconciliation.

This proof covers framework-managed native ownership and supervised processes.
It does not audit external diagnostic programs, network interfaces, shared
memory or other unmanaged radio owners. Keep integrations disabled until the
required hardware checks and any recovery have completed.

## Process-only sustained qualification

`process_soak.py` exercises an installed core wheel without selecting a radio.
It requires its own transient `remoterf-supervision-soak-*.service`, with the
process in that unit's `manager` subgroup and an empty delegated parent as above.
The wrapper enables `+pids +memory`, then executes:

```sh
python tools/qualification/process_soak.py \
  --cgroup-root /sys/fs/cgroup/EXACT_TRANSIENT_UNIT \
  --state-dir /absolute/private/NEW_STATE_DIRECTORY \
  --evidence /absolute/private/process-soak.json --iterations 100
```

Use an outer `MemoryMax=512M`, `TasksMax=192` and `RuntimeMaxSec=300` for the
100-cycle CI run. These are isolated test containment settings, not production
capacity recommendations. It records thresholds and source hashes before
launching: 60 normal parent/setsid-descendant cycles, 10 target-exec failures,
10 pre-spawn failures, 10 injected pre-ack timeouts, and 10 abrupt controller
exits followed by identity-checked offline recovery of a non-radio lease.
It also checks 16 simultaneous owners and rejection at the 64-scope/4-launch
limits. Normal and restart workers opt into required-worker exit monitoring;
finite failures remain optional by default.

Pass requires every cycle to clean up, the independent sentinel to survive,
no final scopes or leases, no accumulating idle threads or file descriptors,
bounded output tails, stop/recovery below 5.5 seconds for the five-second stop
budget, post-warmup RSS growth below 32 MiB, latter-half RSS growth below 16 MiB,
and unit memory peak below 256 MiB. These are pass/fail limits declared in the
script, not latency or throughput promises. A separate opt-in kernel test
exercises Runtime's autonomous required-worker failure path, including exit
zero and a surviving descendant, without an adapter health call.

Keep unrelated load tests out of the measured interval when collecting capacity
evidence. This short process stress run proves neither RF performance nor Global
authorization, supported released-client compatibility, production rollback,
or aggregate service resource allocation under real radio load.

## Dev Global route and rollback checks

`dev_routes.py` is restricted to `https://dev.global.remoterf.net`, the testlab
identity, and synthetic device IDs 99/401/402. Use the separately installed
[dev fixture](dev_fixture/README.md), a dedicated verified dev account, and a
private disposable client home whose name starts `remoterf-phase1c-dev-client`.
It leaves the operator's saved login and shell HOME unchanged. The coordinator
must hold the dev lease, clone configuration/environment, approve the exact dev
schema through the normal gate, and arrange the temporary service and teardown.
Never run the same-identity canary and original testlab together.

```sh
CLIENT_PY=/absolute/isolated/client/bin/python
"$CLIENT_PY" tools/qualification/dev_routes.py --mode enabled \
  --client-home /tmp/remoterf-phase1c-dev-client \
  --evidence /absolute/dev-enabled.json
```

Modes `baseline`, `baseline-relay`, `schema`, `disabled`, `enabled`, `expiry` and `shutdown` cover
old-service simulated receive, schema gating, disabled broken-package safety,
generic relay/direct calls, retry identity after an injected lost direct reply,
partial starts, autonomous worker exit, reservation cancellation, actual calendar
expiry and service stop. Evidence labels all radio behavior synthetic. `enabled`
requires an actual direct route; do not silently substitute relay-only success.

For `expiry`, optional `--expiry-observation FILE` announces a ten-second interval
after the real deadline with no client RPC. During it, independently check empty
hardware ownership, matching boot/root/scope identities and unpopulated cgroups.
Empty scope metadata can remain until the next valid generic request or shutdown;
this is bounded bookkeeping, not evidence that processes are still running.
The client then checks typed expiry refusal and the next holder's native receive.

For `shutdown`, supply fresh `--shutdown-ready FILE` and
`--shutdown-confirmed FILE` paths. After readiness, stop the isolated unit while
its application is running. Independently verify empty hardware/process journals,
unit inactivity and absent cgroups, then restart the same clean configuration.
Only after the service is online write confirmation with `journals_empty:true`,
`unit_inactive:true` and `service_restarted:true`, plus measured observations.
The client then cancels its booking. A confirmation file is external test evidence,
not a recovery mechanism or permission to erase a fence. Preserve a failed run's
records; never downgrade a real-radio inventory over unresolved ownership.

Finally stop the canary, verify its final journals are empty, restore the original
testlab, repeat baseline receive, remove temporary schema approval/network access,
remove the dedicated account, disable all disposable canary configs and release
the dev lease. Keep any failed synthetic state fenced and separate from restored
inventory. None of these checks authorizes production activation or hardware use.

## Real-radio service lifetime supplement

After the handoff checks, use the [isolated service termination supplement](usrp_service_stop.md)
for actual SIGTERM cleanup and suppression of a pending restart with real receive.
It keeps the deployed service unchanged and requires an independent guardian,
verified reservation and empty-state proof before releasing the delegated root.
The [exact-build UCLA report](../../docs/qualification/phase1c-ucla-exact-build-2026-11-09.md)
records the tested build, retained failed attempt, successful follow-up and scope.
