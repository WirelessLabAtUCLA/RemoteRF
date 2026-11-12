# Phase 1c UCLA dev Global end-to-end qualification — November 10, 2026

**All requested hardware cases and restoration checks passed.** The exact-build
receiver-descendant cleanup, missing/mismatched receipt denials,
normal generic API lifecycle and actual dev Global cancellation/expiry passed
on USRP_00@ucla. Production integrations remain disabled. No production push,
publication or framework/package change is part of this work.

The [combined hardware evidence](phase1c-ucla-dev-e2e-2026-11-10.json),
[focused CI evidence](phase1c-ci-dev-e2e-2026-11-10.json) and
[final restoration evidence](phase1c-ucla-dev-e2e-restoration-2026-11-10.json)
are the retained records for this report. Test services and the outer reservation
are released, the temporary dev approval is restored, the isolated deployment is
deregistered and the dev work lease is released.

## Scope and exact build

The user approved USRP_00@ucla, device 40, serial `32C78EE`, for November 10,
12:00–13:00 Pacific. External reservation 1029 was held independently throughout
the shorter dev reservations and cleanup, then released at 12:12:43 Pacific.
The test uses profile `usrp2901`/B200 and native UHD
`4.10.0.main-release`, with receive only: channel 0, 100 MHz, 1 MS/s, gain zero,
and 4,096 samples per recorded receive assertion. No transmit operation occurs.

| Installed artifact | SHA-256 |
|---|---|
| Core `0.1.7.dev0` | `9d3b88258c7a6298c460f50468a6385bc59d64c72e763dcb21719d230e331d97` |
| Server `1.7.23.dev0` | `f9b382782d457bb7197c22b1bc77f310075d103ea5a688f9f4923d1249397d4e` |
| Probe `0.1.0` | `e6894b585afcf6a769ec8f9112f60a509eb18521568820fa21907780b152c784` |

Framework digest is
`8697958f27a1b1289cc45093258cb3eeed84bdb631cacc122a055f21a3a7d41a`.
All **209 packaged files** matched these wheels before and after hardware testing
and on CI. The adapter fingerprint is
`9c6ae545c37066d23a76f9f5fbb90c5a6f7658e531377ebd0a91ad07a756ad0f`.
Changes for this increment are qualification tools, tests and evidence; the
framework, server wheels and adapter wheel remain the prior exact build.

The isolated deployment is `ucla-integrations-20261110`, display name
`Isolated UCLA Integration Qualification`, UUID
`c59791c0-7645-4a4a-94d8-55c5551061a9`. Its separate lab service uses ordinary
server startup, normal configuration/receipt loading, a private account and
calendar, and the dev Global origin `https://dev.global.remoterf.net`. The
client uses normal HTTPS account authorization and generated USRP bindings,
client `2.13.13.dev0`, and the **relay route**. Its private saved account does
not replace the operator's normal home or login. Direct routing is not a claim
of this hardware run.

Only dev temporarily approved stock schema
`sha256:7276bf3ab3eefcb55270abe10311fb000a1897a0337b63a7810c7a107306384b`.
Its original dev status, `rejected`, was restored. Production schema
approval and production integrations were not enabled.

## Hardware results

| Case | Observed result |
|---|---|
| Real receiver descendant survives required-parent death | Passed: two scoped processes and one observed USB owner before parent death; one surviving process still owns USB afterward; native open refused until verified cleanup; native RX before/application/after each recorded 4,096 samples. |
| Missing receipt, normal server startup | Passed: discovery disabled; start refused with `RemoteRFProtocolError` / `qualification_required`; native RX before and after each received 4,096 finite samples. |
| Mismatched receipt, normal server startup | Passed: an actual all-zero `framework_hash` mismatch caused the same typed refusal and disabled discovery; both native receives passed. |
| Matching receipt and generic lifecycle | Passed: list, describe, start/status, duplicate start, health/status, duplicate action, stop/release and native reacquisition; native access was refused with `RemoteRFNativeUHDError` while the application owned the radio. |
| Actual dev Global reservation cancellation | Passed: live application receive, real account-API cancellation, independent physical/process release observation after **10.696 seconds with no device RPC**, typed old-session refusal, then a new holder receiving 4,096 finite samples. The old operation was hidden from the new holder. |
| Actual dev Global reservation expiry | Passed: one independently observed hardware owner/populated scope before the 12:09 Pacific calendar end, zero afterward without a device RPC, **10.731 seconds** from expiry to the first device RPC, typed old-session refusal, then next-holder RX of 4,096 finite samples and the prior operation hidden. Total case duration: **173.274 seconds**. |

The descendant case uses the unchanged receive worker beneath a qualification-only
required wrapper. Only the verified wrapper is terminated. A bounded cleanup
barrier permits a positive observation of its surviving receiver; the stock
adapter/runtime then handles `process_exited` and drains the owned cgroup without
a health action. The hardware journal was empty, native receive worked again,
the private calendar was empty, and the independent post-run check found zero
hardware/process records and zero owned application cgroups. The separate runner
cgroup also emptied normally; no emergency runner kill was needed.

The accepted private dev receipt derives all ten required checks from the prior
exact-build handoff/service evidence and this new descendant evidence, with
independent cleanup bound to its report digest. Receipt annotations explicitly
say `dev_canary_only: true` and `production_approved: false`. Those annotations
are descriptive, not an additional runtime access-control gate. The receipt and
enabled configuration remain private to the canary. The receipt itself does not
claim Global API/calendar coverage; the subsequent ordinary client runs supply
that evidence.

For cancellation and expiry, the client makes no device RPC or root close while the
independent observer inspects release. Only afterward does its first device RPC
receive `RemoteRFReservationError`; a fresh reservation/root then receives and
gets `unknown_operation` for the prior operation. The independent pre-RPC
snapshot in each case retains one identity-verified **unpopulated** scope record. This is
bounded retained bookkeeping, not a claim that every record had already been
deleted. Final service cleanup subsequently retired those records; both persisted
hardware/process journals were empty before releasing the guardian.

Expiry uses an ordinary ten-minute reservation interval with a past start and
a real future end; no clock, calendar row or authorization callback is patched.
The independent read-only observation at 12:07:13 Pacific positively established
one hardware owner and one populated scope while the client stayed quiet. The
post-expiry observation found both released before any stale-session RPC could
trigger client-side cleanup.

## Observation limits and retained setup corrections

The read-only observer verifies existing regular ownership/process journals,
the delegated root and scope identities, kernel population, and absence of
unknown owned scopes. USB descriptor visibility is **current UID only**; neither
cross-UID descriptors nor hidden in-process runtime counters are observed or
inferred to be zero. Subsequent native RX supplies an additional practical
radio-acquisition check. These observations establish the tested owned-resource
release boundary, not a global process audit.

Two qualification setup corrections are retained, with no hardware failure
claimed for either:

- UCLA's Python lacked the `os`/`signal` pidfd wrappers during the empty-inventory
  guardian preflight. A tested libc pidfd fallback preserves descriptor-bound
  signaling, including liveness checks around capture. Empty-inventory scopes
  were independently cleared before restart. Framework and wheel bytes did not
  change.
- After the passing missing-receipt negative control, the strict observer
  refused absent ownership/process JSON files. These journals are first
  persisted on an actual claim; rejection happened before any claim or write.
  The negative-control cleanup record therefore says
  `journal_files_present: false` and independently verifies empty application
  cgroups, zero kernel population, graceful server exit and no visible USB
  owner. It does not claim nonexistent files contained empty objects. The
  stricter existing-journal requirement remains in force after real operations.

## Focused regression checks

The exact installed build passed **83 focused CI checks, with zero skips**:

| CI group | Passed |
|---|---:|
| Descendant/receipt qualification tooling | 40 |
| Dev client qualification runner | 21 |
| Controller receipt gates and native preservation | 21 |
| Required existing UHD client/server localhost socket regression | 1 |

The socket test uses real gRPC/codecs with fake radio resources and actually
executes; no physical device is used on CI. The changed qualification group
also passed **61 tests locally**. Initial local test invocations lacked either the
client source path or the core/probe packages; selecting the server/probe
environment with the client source path resolved setup. These focused checks
supplement the prior full suites; this increment did not repeat a full suite.

## Existing UHD and restoration status

Ordinary deployed Global/UCLA UHD passed both compatibility checks using client
`2.13.13.dev0` and the unchanged production schema:

| Deployed UHD check | Native open | Samples received | Stream/root closed |
|---|---:|---:|---|
| Before isolated testing | 2.659 s | 4,096 | Yes |
| After isolated service cleanup | 2.681 s | 4,096 | Yes |

Final checks confirmed original production PID `359991`, server `1.7.21`, core
`0.1.6`, the same inventory digest and no integration configuration. No production
deployment, package update, schema approval or integration activation occurred.
All 209 qualification package files still matched their exact wheels.

Both private test units ended `inactive/dead`, `Result=success`, MainPID zero,
with no remaining owned cgroup roots. Persisted hardware/process journals were
empty; the private canary configuration was disabled and its inventory emptied.
The final USB observation found zero owners visible to the service UID, with the
same cross-UID observation limit described above.

The temporary dev account was deleted, with its account sessions/membership
removed and proxy revocation observed at both Global and the private canary.
No pending subject revocation remained. The temporary stock-schema approval
returned to its prior `rejected` status; other schema rows stayed unchanged.
Reservation 1029 was cancelled at **12:12:43 Pacific** after cleanup and the
final deployed UHD check. A subsequent account reservation listing was empty.

The private deployment left dev Global: its status is `left`, its bound name is
cleared and it has no active relay membership. The original dev testlab remained
active with the same PID, inventory and configuration; dev service images were
unchanged. Four disposable local credential files were removed, leaving no
credentials for the temporary account; the operator's other credentials were
untouched and audit evidence was retained. The dev work lease was released
successfully. Final dev status reports `free: true`, the original Global image
and testlab server `1.7.22`.

## Remaining production gates

These results cover a narrow receive-only dev canary, not production activation
approval. The current [integration deployment
requirements](../application-integrations.md) still require:

- Constrain the first enabled adapter profile to the tested settings, or enforce
  and qualify a configuration envelope. A matching profile-name receipt does
  not qualify the probe validator's entire rate/frequency/gain/channel range.
- Ship and test the intended production service/delegation and independent
  cleanup arrangement. Temporary qualification guardians are not shipped
  server/native-crash containment; native crashes and GIL stalls remain limits.
- Audit the full target inventory for broad/address/composite USRP or Phaser
  selectors before enabling physical mapping, while retaining the disabled
  raw-UHD compatibility path.
- Establish aggregate CPU, memory, task and disk-retention budgets; test native
  UHD response under them; run extended real-RF soak and a production-shaped
  drain/recovery/rollback rehearsal, including unresolved journals, changed
  cgroup identities and ordinary UHD after restoration.
- Complete the supported hardware/profile/client/route coverage and review the
  exact production release, schema and receipt approvals. This run covers one
  USRP serial, one receive configuration and relay; it does not establish every
  supported client or route.

OAI, transmit, host application execution, submitted jobs and artifacts remain
later work. Production integrations stay disabled unless the user explicitly
authorizes a later rollout.
