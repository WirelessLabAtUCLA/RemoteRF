# Isolated real-radio service termination supplement

`usrp_service_stop.py` complements `usrp_handoff.py`. It uses the exact installed
framework with a real reserved USRP and receive-only probe to exercise actual
`SessionManager`, `Controller`, `DeferredScope`, `Runtime`, `headless_main`,
`_service_lifetime` and `ServerRuntime.stop` cleanup. It opens an unbound gRPC
server with **no listening port**, starts no federation agent and never writes
the deployed service's configuration or a qualification receipt.

Run it only after the baseline handoff harness passes, under the same verified
external booking and explicit physical serial. Keep the delegated root alive in
an independent guardian until hardware/process journals and cgroups are verified
empty, including when this payload fails. Its per-user serial lock prevents
another simultaneous local qualification payload but does not replace reservation
or production ownership controls.

```sh
python -I tools/qualification/usrp_service_stop.py \
  --serial EXACT_SERIAL --device-id EXACT_GID --reservation-id ACTUAL_BOOKING \
  --authority-command /absolute/operator/reservation-check \
  --cgroup-root /sys/fs/cgroup/EXACT_GUARDED_ROOT \
  --state-dir /absolute/private/NEW_SERVICE_STOP_STATE \
  --evidence /absolute/private/service-stop.json \
  --expected-framework-hash EXACT_REVIEWED_FRAMEWORK_SHA256
```

All paths must be absolute and the state directory must be fresh. The framework
hash uses the same core/controller/native/lifetime files as the activation gate;
it is checked in both parent and child before hardware use. The JSON records
source hashes, adapter content hash, versions, external authority and outcomes.
Native receive uses the same conservative defaults as the main harness: channel
zero, 100 MHz, 1 MS/s, gain zero and 4,096 samples. There is no transmit action.
The harness imports NumPy, UHD and the native session module before creating the
reservation authority monitor; it does not open or discover hardware during
this prewarm. The existing four-second authority cache and fail-closed native
recheck remain unchanged. Next-holder receive records read-only authority
snapshots and stage timings, and a passed child cleanup remains in the evidence
if the subsequent receive fails. There is no automatic retry or TTL extension.

Two fresh child processes run sequentially:

1. Native receive → real supervised application receive → SIGTERM while running.
   The parent signals only its exact `Popen` child, then checks child exit,
   restored signal handler, closed admission before transport drain, empty
   hardware/process journals, preserved root identity and no owned cgroups.
   A fresh local session must receive from the same radio afterward.
2. The application reaches real receive readiness, then the isolated service
   begins an operator restart. A ten-second test-only barrier in the transport
   stop wrapper lets the parent deliver SIGTERM after cleanup begins. Repeated
   SIGTERM must not interrupt cleanup, and the pending restart must not execute.
   `_reexec` is instrumented to fail if reached; it never replaces the guardian
   or starts another production process. Empty journals and next-holder receive
   must pass again. Application resources may already have stopped before this
   second signal; the first case separately covers termination while running.

Only the transport-stop barrier, unused surrounding services and restart action
are instrumented. Application ownership is wired through an **internal Binding**
for qualification, with no fabricated hardware receipt and no public integration
start. This proves real-radio lifecycle cleanup, not deployed network startup,
Global RPC/receipt gates, calendar propagation, original service shutdown,
production aggregate capacity, sustained RF load, or another profile/radio.

On failure the parent stops only its own child if it is still running, preserves
all ownership records, writes `passed:false` and returns nonzero. The guardian
must remain alive. The case state directories `running` and `pending_restart`
use the standard `ownership/` and `processes/` layout, so the offline operator
`inspect`, `recover` and `cleanup-orphan` commands in the qualification README
can reconcile the exact recorded identity. Never release the guardian, open a
new native owner, erase records or create a new cgroup root to bypass uncertainty.
No pass result enables integrations or substitutes for an operator approval.
