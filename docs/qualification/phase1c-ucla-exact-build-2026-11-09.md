# Exact-build UCLA receive-only qualification — November 9, 2026

The exact dev-qualified build passed isolated real-radio handoff, recovery and
service-termination checks on USRP_00@ucla. Existing deployed UHD receive passed
before and after testing. All test ownership/process state was independently
verified empty before releasing reservation 1021 at approximately 20:35 Pacific.
**Production integrations remain disabled. Nothing was pushed or published.**

The later [normal-auth cold-process follow-up](phase1c-cold-auth-2026-11-09.md)
passed 12 fresh-process real-radio opens on these same artifacts without the
harness's four-second auth cache or extra warmup. It addresses the narrow normal
local-auth concern raised by the initial supplement failure; its precise original
cause remains unproven, and broader release limits still apply.

## Build and reservation

USRP_00@ucla is device 40, serial `32C78EE`, with inventory profile
`usrp2901`/B200. The user approved November 9, 20:00–21:00
America/Los_Angeles and early release after testing. A separate executable
checked the live reservation row, account, device, serial and time every second
using read-only access. Fault injection kept this external booking held through
cleanup; the account's reservation list was empty after cancellation.

The tested implementation is unchanged from commits `7014bbb` and `bc76721`.
All 204 main-package source/data files matched the exact three wheels before
hardware testing. Final verification also compared the server wheel's five
additional bundled `rssi` files: all 209 packaged files matched. The separate
private environment inherited UCLA's native UHD `4.10.0.main-release`; no native
library or production package was replaced.

| Component | SHA-256 |
|---|---|
| Core `0.1.7.dev0` | `9d3b88258c7a6298c460f50468a6385bc59d64c72e763dcb21719d230e331d97` |
| Server `1.7.23.dev0` | `f9b382782d457bb7197c22b1bc77f310075d103ea5a688f9f4923d1249397d4e` |
| Probe `0.1.0`, current required-worker implementation | `e6894b585afcf6a769ec8f9112f60a509eb18521568820fa21907780b152c784` |

The framework digest is
`8697958f27a1b1289cc45093258cb3eeed84bdb631cacc122a055f21a3a7d41a`.
A fresh probe wheel was built from the current source; the older stale probe
artifact was not used. Receive settings were explicit: channel 0, 100 MHz,
1 MS/s, gain 0, finite 4,096-sample bursts. No transmission or saved I/Q capture.

## Radio ownership and recovery

The authoritative [handoff evidence](phase1c-ucla-exact-handoff-2026-11-09.json)
passed 17 recorded checks in **47.466 seconds**, 20:24:26–20:25:13 Pacific.
It used an isolated SessionManager/Runtime and separately installed receiver
probe. Checks covered native RX → application RX → native RX, duplicate start,
startup failure after actual RX, autonomous required-worker exit detection,
held-stop timeout fencing, locally injected expiry during startup/running,
next local-holder RX and abrupt-controller recovery.

After abrupt controller exit, the matching pinned cgroup positively reported
`populated=1` and one process. A fresh controller refused native acquisition
until identity-verified cgroup cleanup completed, cleared the durable ownership
record and then received again. The harness now records this positive kernel
observation separately from release proof; an unreadable or empty scope cannot
be counted as a surviving application. Observation failure still attempts normal
verified cleanup before failing qualification.

The required-worker case deliberately terminates its supervised scope and waits
for autonomous detection without a health action in that case. This is not an
independent-parent-death test preserving descendants; separate Linux kernel
checks cover that pattern. The normal lifecycle separately tests a health action.

The authoritative run's four hardware/process journals were independently read
and empty, no owned cgroup remained, and no process held the USB device. A guardian
preserved the delegated root until this verification; its unit then exited
successfully. The earlier 47.557-second pass and its cleanup observations are
retained in the same bundle, but the later run supplies positive survival proof.

## Isolated service lifetime

The [service supplement evidence](phase1c-ucla-exact-service-stop-2026-11-09.json)
passed in **25.045 seconds**, 20:33:53–20:34:19 Pacific. It runs the actual
Controller, DeferredScope, SessionManager, Runtime, ServerRuntime and main-thread
signal wrapper with real radio receive. It uses internal bindings and an unbound
gRPC server: no public listener, deployed configuration, fabricated qualification
receipt or Global schema approval.

| Case | Observed result |
|---|---|
| SIGTERM during active application receive | Child exited successfully in 0.164 s; admission closed before transport drain; cleanup completed; fresh native RX passed |
| SIGTERM during pending-restart cleanup, then repeated SIGTERM | Child exited successfully in 0.215 s; cleanup completed; zero re-execution calls; fresh native RX passed |
| Independent post-run observation | Four empty hardware/process journals, verified root identity, zero owned scopes, zero USB owners |

Both children restored the prior signal handler. The restart case uses a bounded
transport-stop barrier after admission closes; application cleanup may already
have finished before that signal. The running case separately covers termination
with an active receiver. Stop timings are observations for these runs, not
production deadlines or capacity promises.

The **first supplement attempt remains recorded as failed**. Its child passed
real SIGTERM cleanup, but the parent's follow-up native open/receive was rejected with
`RemoteRFSessionExpiredError`. The live external reservation remained valid.
Independent inspection proved empty hardware/process journals, no owned scopes
and no USB owner before releasing that failed test's guardian. No journal was
deleted, force-released or relabelled as passing.

This is consistent with the harness's four-second cached authority becoming
stale during first native import/open; the original attempt lacks sufficient
timing data to prove the precise cause. The harness now imports NumPy/UHD before
starting authority freshness, records cache/open timing, and retains child cleanup
evidence if a follow-up receive fails. It does not open hardware while importing,
lengthen the TTL, bypass authority checks, add refreshes in native callbacks or
retry an open. The existing background refresh thread remains unchanged.
The fresh run passed both cases with authority active before and after each
native open. Cold-start/GIL-stall containment is not established by that result.
Only qualification harnesses changed; production implementation bytes did not.

## Existing UHD and production state

[Deployed compatibility and final-state evidence](phase1c-ucla-exact-restoration-2026-11-09.json)
records Global/UCLA receive before and after isolated testing. Each received
4,096 samples from the exact serial and closed the streamer/root. Final native
open took **2.752 seconds**. Client `2.13.13.dev0` used relay mode and the unchanged
deployed schema
`sha256:69159a5e174635038b732093f12b78f412c422e281bf2855935f8d121f8c7144`.
This tests old-server/new-client raw compatibility, not deployment of generic RPC.

Final verification found UCLA's original production PID **359991**, server
`1.7.21`, core `0.1.6`, unchanged inventory digest and no `integrations.json`.
Every qualification unit had MainPID zero and no cgroup; the failed attempt
retains its failed unit result. All guarded roots were absent and the USB device
was free. The reservation was then cancelled successfully and the account's
reservation list was empty. Inactive private evidence/state is retained for audit.
No production restart, package update, activation, schema approval, push or
publication occurred.

## Scope and remaining gates

The changed handoff/service harness regression group passed **27 tests** locally,
including real OS signals with fake radio resources, refusal of unresolved
journals, positive population evidence, and preservation of failure evidence.
Prior full UHD/server/host, dev-route and load results remain in the
[dev rollout report](phase1c-dev-rollout-2026-11-09.md); no implementation change
invalidates those tested core/server artifacts in this increment.

Local expiry was injected while the external reservation remained held. This is
not actual Global calendar expiry on a real radio. The service supplement checks
internal lifecycle cleanup, not deployed generic RPC/receipt admission or full
public server startup. Dev covered synthetic route/authorization checks separately.

Results apply to this serial/profile, exact framework/probe build, native UHD
version and receive configuration. They do not qualify TX, OAI, Phaser/other
profiles, host applications, production aggregate resource budgets, extended RF
soak, native crash/GIL-stall containment or production rollback. Matching operator
receipts, exact production schema approvals and remaining deployment/profile
checks are still required before activation. **Production remains disabled by
explicit user instruction regardless of these passes.**
