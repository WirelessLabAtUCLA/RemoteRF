# Exact-build receive-only dev qualification

These tools add the missing positive receiver-descendant evidence for an
isolated dev canary. They do not enable production,
change the installed framework/probe packages, or prove the public API by
themselves. The external physical-radio reservation must remain active while
the canary exercises its separate dev reservations.

## Receiver-descendant fault injection

`usrp_e2e_descendants.py` runs with the unchanged installed probe distribution.
Its qualification-only wrapper launches the original receiver as a child with
inherited output pipes and a separate session. The supervisor owns the wrapper
as a required process; both processes remain in one verified cgroup. After
real RX, the harness captures and signals the exact wrapper's pidfd. It never
signals a PID read from a file or from the USB ownership query.

A ten-second cleanup barrier allows a positive observation of the real receiver
holding USB after its required parent dies. The barrier is then released;
ordinary runtime failure detection and adapter stop must empty the scope and
release the hardware journal. Native admission must remain fenced while the
receiver survives, and native RX must work again after cleanup. No health
action triggers that cleanup. This is an explicit launch-topology fault
injection, not a claim that the unmodified probe normally forks a receiver.

Run only under an independent delegated guardian. The qualifier must be in
`<root>/runner` (the legacy manager placement is accepted by the qualifier,
but a separate runner cgroup also contains native preload helpers on failure).
The root must belong exclusively to this qualification.
The guardian retains the physical serial lock, passes its verified descriptor
with `--lock-fd`, checks the external booking throughout, imposes a deadline,
and preserves the cgroup root until independent cleanup inspection. On an
unexpected qualifier exit it must stop the exact owned child and all verified
payload cgroups while retaining unresolved ownership records for reconciliation.
The guardian must stop the pinned runner domain on failure, require it empty
on normal exit, and remove it only after kernel population reaches zero. It
must not terminate its own manager subgroup. The qualifier uses Python pidfd
wrappers when present and an errno-preserving libc fallback on Linux builds
that omit those Python wrappers; both signal only the captured owned child.

The qualifier requires sibling `usrp_cold_start.py`, `usrp_service_stop.py`, and
`usrp_handoff.py`. Supply absolute paths for a new private `--state-dir`,
`--evidence`, `--cgroup-root`, executable `--authority-command`, the approved
`--reservation-id`, `--serial` and `--device-id`. No public server listener is started. Its private
account, calendar and token resolver use the ordinary server implementation.

## Independent cleanup

After the qualifier exits successfully, independently inspect its hardware
ledger, process ledger, pinned root identity, cgroup population and the actual
USB node. Also verify production configuration was unchanged. Preserve a JSON
record with these fields, using the digest of the exact raw evidence file:

```json
{
  "format": "remoterf-receiver-descendant-independent-cleanup-1",
  "passed": true,
  "serial": "<observed serial>",
  "device_id": 40,
  "evidence_sha256": "<sha256 of raw descendant evidence>",
  "hardware_records": 0,
  "process_records": 0,
  "owned_cgroups": 0,
  "usb_owner_count": 0,
  "usb_observation_scope": "current uid only",
  "cross_uid_usb_owners_observed": false,
  "root_identity_verified": true,
  "production_unchanged": true,
  "observed_at": 1794340801,
  "guarded_root": {"root": "<observed root>", "identity": [0, 0]}
}
```

The example values are a schema illustration, not evidence. Use the actual
identity and a finite inspection timestamp after the raw run's `finished_at`.
Do not create a passing record until every observation is established.
An unprivileged USB descriptor query only establishes the owners visible to that
user. Record that limitation; do not interpret zero visible owners as a global
cross-user descriptor audit. Verified empty managed cgroups and subsequent
native receive provide separate cleanup and reacquisition evidence.

## Public dev canary sequence

Use the exact built server through its ordinary startup path, ordinary dev
account authentication and generated client schema. Hold the real production
reservation exclusively throughout the nested dev checks and avoid concurrent
production requests for that radio. Retain the unchanged production service and
its disabled integration configuration; the operator guardian independently
checks the outer reservation and stops the private service before it ends.

1. With integrations disabled, verify native RX before and after generic
   discovery and a start denied with `integration_disabled`; the denial must
   precede native handoff.
2. Enable the probe for this serial only in the private canary's
   `integrations.json`. Restart normally, discover the probe, start receive, observe state/logs and
   health through the public generic API, and stop explicitly. Verify native
   RX, empty ownership records and no remaining USB owner.
3. With an active application, let a real dev reservation expire through the
   ordinary Global-to-deployment synchronization path. Verify revoked access,
   bounded cleanup, and a subsequent holder's native RX.
4. Repeat with cancellation of a real dev reservation. The outer physical UCLA
   booking remains active through both nested cases and all cleanup checks.
5. Stop the canary, verify empty persistent and kernel ownership independently,
   restore the original disabled deployment configuration, release guardians,
   and release the outer booking after cleanup. Retain exact artifact hashes,
   startup logs, public-route results, calendar timings and independent cleanup
   records. Do not claim a missing check passed based on unit evidence alone.

Qualification helper tests:

```text
python -m pytest tools/qualification/tests/test_usrp_e2e_descendants.py
```
