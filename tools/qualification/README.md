# Receive-only ownership qualification

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
