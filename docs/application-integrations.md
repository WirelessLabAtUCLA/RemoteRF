# Application integrations: experimental contract 0.1

Phases 1a and 1b implement the adapter contract and internal ownership runtime.
The shared server/host native lifecycle now tracks in-flight calls, closing
handles and workers through cleanup. This is a real change to existing runtime
code and needs baseline verification even when no adapter is installed.

Applications remain unavailable through normal configuration and RPC. Default
inventory gates have no durable journal and reject application claims. There is
no new public RPC, production activation, process supervisor or OAI dependency.
Stock UHD drivers, schema hashes, codecs and client code are unchanged. Qualified
physical identities, startup reconciliation, resource supervision and the generic
remote API are activation gates for Phase 1c.

The contract lives in `remoterf_device_core.integrations`. Importing it does not
import native drivers, UHD, NumPy or the device-schema registry. No adapter is
discovered or enabled automatically. Adding it to a package does not activate it
on existing deployments. Contract 0.1 is experimental, uses exact version
matching, and may change before radio support is released.

## Adapter package and operator metadata

The separate `examples/integrations/counter` package demonstrates the public
interface without OAI or hardware. Its JSON manifest declares:

- a namespaced integration ID and adapter version;
- the exact contract version, supported device types, profiles and actions;
- an operator-controlled `module:factory` entry point.

An operator supplies explicit manifest paths and a device-to-integration allow
list when constructing a `Registry`. Metadata files are read without importing
adapter code. Duplicate IDs, unknown enabled IDs, malformed manifests and
oversized configuration fail with typed `IntegrationError` codes. Discovery
returns copied metadata and enabled/compatible flags; it omits the factory path.
Compatibility means the declared contract/device type match, not that dependencies
or hardware have been qualified. Import and factory errors appear only on
construction, with a safe client-facing message and the original local cause.

Use `Registry.validate_start()` or `validate_call()` to check enablement,
compatibility, declared profiles/actions and input bounds before calling adapter
code. `create()` lazily imports the factory after admission checks and creates a
fresh instance for the supplied context. It does not grant a lease or start work.
The registry is a configuration snapshot; deployment configuration replacement
and package upgrade coordination remain in Phase 1c.

These methods are primitives for a trusted runtime. They are not authentication
or a remote API: the binding derives device identity from reservation authority,
constructs the context, and acquires ownership before starting resources. Remote
call authorization and deployment wiring remain in Phase 1c.
Never expose `create()` or the allow list directly to a remote caller.

## Lifecycle contract

The factory takes a frozen `Context` with device ID/type, generation, owned work
directory and cancellation event. The context contains no bearer token or native
UHD object. Hardware selectors, resource registration and supervision services
remain activation gates for Phase 1c; the current binding accepts trusted runtime
callbacks, not adapter-provided ownership assertions.

| Hook | Responsibility |
|---|---|
| `validate(profile, configuration)` | Return normalized configuration without acquiring resources. |
| `start(profile, configuration)` | Return after useful readiness, or raise on failure. |
| `status()` | Return a bounded plain-data object. |
| `call(action, parameters)` | Handle a declared action and return bounded plain data. |
| `stop()` | Release resources idempotently, including after partial startup. |

Construction must be side-effect free. Hooks are synchronous. The internal
`Runtime` schedules lifecycle work outside RPC/reservation locks, bounds queues
and deadlines, and validates normalized configuration and cached status using
`object_data()`. Generic action dispatch and remote request validation remain in
Phase 1c. Direct Python calls to an adapter are not automatically wrapped.

Adapters are trusted operator-installed code. Catching an import exception does
not contain a hung import, native crash or stuck call. Process supervision and
dependency isolation remain necessary where those failure boundaries matter.

## Plain-data envelope

`object_data()` accepts a dictionary; `plain_data()` also accepts scalar/list
values. Both validate and copy exact built-in JSON types: `None`, booleans, signed
64-bit integers, finite floats, valid Unicode strings, lists, and dictionaries
with string keys. They reject bytes, NumPy arrays, tuples, custom objects,
non-finite numbers and recursively reserved protocol tags. They never stringify
an unsupported object or invoke its conversion hooks.

Limits are 64 KiB of encoded JSON, 16 levels of nesting and 4,096 value nodes.
String-key bytes count toward the text/encoded budget. Manifest input is limited
to 16 KiB, registry entries to 64, and per-device enabled IDs to 64. The reserved
keys are `__remoterf_handle_arg__`, `__remoterf_handle__`, `__remoterf_type__`,
`__uhd_type__`, `__remoterf_uhd_helper__`, `__remoterf_json_type__`, `__args__` and
`__kwargs__`. This validation is separate from the existing UHD codec.

The envelope budget is not a complete RPC-frame budget. Discovery may need paging
once exposed remotely. Bulk binary transfers, arrays and artifacts need their own
bounded contract; they are not supported inside these envelopes. Integration
validation must run before existing UHD helper/handle conversion when RPC wiring
is added. This increment does not change that existing conversion path.

## Verification and installed-package example

From the repository root, in a disposable test environment:

```sh
python -m pip install -e packages/remoterf-device-core pytest
python -m pytest packages/remoterf-device-core/tests -q
python -m pip wheel --no-deps packages/remoterf-device-core examples/integrations/counter --wheel-dir /tmp/rrf-integration-wheels
python -m venv /tmp/rrf-integration-example
/tmp/rrf-integration-example/bin/pip install --no-deps /tmp/rrf-integration-wheels/*.whl
/tmp/rrf-integration-example/bin/python examples/integrations/counter/smoke.py
```

The wheel smoke uses distribution metadata to locate the manifest without
importing the adapter. It verifies disabled discovery/construction, explicit
enablement, validation, readiness, an action, cancellation and repeated cleanup. It also exercises the installed durable journal and
asynchronous runtime with simulated native callbacks and retry-safe start/stop.
It runs with no native dependencies installed. The sample uses simulated device
type `sim_sdr` and cannot acquire a radio. This is a packaging/contract example,
not proof of hardware handoff or the complete adapter conformance suite.

CI now runs core contract tests, builds and exercises both wheels, installs the
sample into the server test environment while leaving it disabled, and reruns
the required client/server loopback. The loopback imports current `remoterf` and
fails collection if that import breaks instead of silently skipping. Hardware
tests also use the current namespace but still require their explicit hardware
configuration.

## Phase 1b ownership and native lifetime

`integrations.ownership.Journal` takes a single-writer OS lock and persists
bounded ownership records using atomic replacement and file/directory fsync.
`Gate` represents one physical device. Aliases using the same identity and
journal share the same gate; this primitive does not discover physical aliases.
A qualified deployment must use one canonical journal and map every physical
alias to it before allowing any native access. Default gid-based inventory gates
are accounting only and cannot start applications.

A claim first fences admission in memory. Its worker persists the claim before
releasing native hardware or constructing an adapter. Slow persistence does not
hold the status/admission lock. Failed writes, corrupt/ambiguous records and
unresolved records after restart refuse ownership. Restarting, disabling an
adapter or removing its package never proves resource release.

`integrations.runtime.Binding` connects trusted session authority, native
release/reacquisition and independent application-resource verification to the
runtime. It carries no bearer token into the adapter. The local-server binding
currently supports only plain `usrp` with a deliberately injected durable gate;
Phaser, composite/ambiguous radio selectors and host integrations cannot be
enabled through it. Host bindings explicitly return `unsupported`.

Startup requires no active native calls, child handles, closing children or
known failed child cleanup. A refused claim leaves existing UHD streams usable.
Accepted startup drops the native root reference and verifies native release
before loading/starting the adapter. Stop verifies application release before
reacquiring a fresh native schema and refreshing metadata/root generation.
Expired authority never authorizes reacquisition; expiry during reacquisition
closes the newly created object. A handoff is reinitialization: radio settings
and continuous timestamps are **not** preserved promises. Real hardware,
preload/firmware assumptions and client metadata refresh still need qualification.

Operation IDs and request keys are scoped to session generation and physical
identity. Identical retries attach to the recorded outcome; changed inputs
produce `request_conflict`. Outcomes consume capacity until the owning generation
is retired; unresolved owners are never evicted to accept more work. Status is a
cached bounded snapshot, including lifecycle phase and a safe error code. It does
not call the adapter. Stop signals cancellation and never runs concurrently with
unfinished start code. Separate cleanup workers reserve capacity for teardown.
A deadline marks an error and retains ownership until actual completion/proof;
it cannot kill Python/native code. Trusted authority callbacks must be fast,
local bookkeeping and must not perform blocking network or driver operations.

| Default bound | Value |
|---|---|
| Retained operations per runtime, including completed retry outcomes | 16; configurable up to 64 |
| Application start workers / reserved cleanup workers | 4 / 4; each queue bounded by operation capacity |
| Start deadline / separate cleanup deadline | 30 seconds each; monotonic, not force termination |
| Cached adapter status JSON | 48 KiB; room remains for operation metadata |
| Journal | 64 owners, 64 KiB; single writer, local durable filesystem |
| Shared native cleanup pool | 4 daemon workers, queue 64, at most 8,192 retained jobs |
| Host reservation hooks | At most 64 pending per device, ordered through the shared pool |

These are defensive software bounds, not measured deployment capacity or latency
SLOs. One stuck task leaves other workers available; exhausting a fixed pool
stops further cleanup without declaring the affected radios free. In-process
code is trusted: a native crash or GIL-holding call is not contained by threads.
Production process/resource supervision, dependency isolation, log/disk limits
and process identity verification are still required before activation.

### Shared UHD behavior and lock order

Server and host hold a short admission ticket through native dispatch and child
publication. Per-entry/control locks are taken before lifecycle bookkeeping;
manager/state locks protect maps only. Native calls, adapter hooks, cleanup waits
and journal I/O do not run under a manager/lifecycle/admission lock. RX/TX retain
their existing separate streamer locks rather than a global session I/O lock.
An operation queued behind a control lock revalidates authority before dispatch.

Close invalidates public handles immediately, signals workers and retains strong
references/closing records until admitted calls and actual worker threads end.
A late native child is disposed before the radio becomes available. RX prefetch
uses a new stop event per worker and never replaces a still-running worker.
Failed opens clear their opening slot even if secondary cleanup fails. Shutdown
stops new session admission and attempts cleanup independently across devices;
an in-process restart is refused while earlier native session work is pending.
Inventory shutdown also uses the bounded pool rather than waiting indefinitely
on one driver's close hook.

Reservation reset/preparation waits for opening/closing work and application
ownership; stale holder preparation is discarded. Legacy/master paths, new
opens, diagnostics (including force), availability and inventory reload honor
ownership. Diagnostic tickets survive timed-out test workers. Reload refuses
while native work/cleanup remains active and can be retried after it drains.

**Intentional compatibility change:** a close acknowledgement is not proof that
a hung native call has stopped. Reopening, reset, diagnostic access and reload
wait/refuse while cleanup is still pending. Raw-only reopening after a completed
close error and ordinary session replacement remain supported, with cleanup
failure counters. Application leases use stricter positive release proof and
stay fenced after cleanup failure. No new driver/UHD version gate is introduced.

### Recovery and rollback for the experimental runtime

1. Stop admitting starts with `Runtime.shutdown()` and request cancellation;
   inspect operation state/phase, gate ownership and native closing/failure counts.
2. If a start/cleanup hook is still executing, retain the lease. A timeout,
   cleared public handle or stop flag is insufficient. Do not delete the journal
   to force access, kill an unverified PID, or start a replacement radio owner.
3. After work has actually stopped, independently verify **all** native and
   application resources are released, including supervised descendants and
   interfaces/shared memory where applicable. `Runtime.recover()` accepts this
   trusted operator proof and refuses recovery while hooks are still running.
   Recovered journal-only owners use `Gate.release()` with the same proof.
4. Keep uncertain owners unavailable. Preserve a corrupt journal for investigation;
   rebuilding it requires independently reconciling every affected physical radio.
   The journal stores lease/session/generation/integration identity, **not** enough
   process identity for automatic orphan termination. Phase 1c must supply that
   supervision and run reconciliation before inventory acquisition on startup.
5. Only after all ownership is resolved may the runtime be closed, an adapter
   removed or code rolled back. `close()` refuses unresolved work. Disabling
   discovery does not stop children or undo shared native-lifetime changes.

This is an internal operator API/runbook, not a shipped CLI or remote recovery
endpoint. Normal installs cannot create application leases yet. No physical-radio
handoff, automatic orphan recovery or production service-stop qualification is
claimed by the current deterministic tests.

## Phases and remaining release gates

1. **Regression repair:** required localhost loopback restored; hardware test
   source discovery/imports corrected.
2. **Contract foundation (implemented in 1a):** registry, payload rules, context,
   independent non-radio sample, failure/limit tests and wheel proof.
3. **Ownership runtime (internal implementation in 1b):** admitted native calls and handle publication, closing
   worker tracking, reservation/diagnostic/reset coordination, bounded async
   lifecycle, durable recovery and native reacquisition. Preserve raw-only
   replacement/reopen-after-close-error behavior. Keep integrations unavailable
   through all server/host routes until these protections are wired and tested.
4. **Generic RPC and hardware sample:** stable schema surface and approvals,
   retry-safe operation IDs, host unsupported-feature guard, old-client checks,
   all-route tests and a separately packaged adapter proving real hardware
   release/reacquisition. Then qualify optional OAI through the same contract.

Do not treat this increment as completion of Phase 1 in the implementation plan.
Deterministic tests cover cancellation, expiry and journal recovery primitives;
production startup reconciliation, process containment, load/soak limits and
real-radio compatibility remain unqualified. No package has been published and no
deployment has been upgraded. Version bumps and server/host dependency pin
updates are required when a shared-core release is prepared.

## Phase 1a evidence from November 9, 2026

The worktree was based on `8413aa7`. The loopback repair is commit `1a3dcb7`.
Tests use disposable environments and configuration directories. No deployment
service, radio reservation or production package was changed.

| Environment and scope | Result |
|---|---|
| macOS, Python 3.12.10, full non-hardware server suite | 900 passed, 5 opt-in legacy-pipeline skips; 97 subtests passed |
| macOS, Python 3.12.10, full host suite | 147 passed; 39 subtests passed |
| macOS, Python 3.12.10, full client suite excluding integration directory | 626 passed, optional MCP module skipped; 25 subtests passed |
| macOS and CI-box Linux, Python 3.12, shared device-core suite | 55 passed; 9 subtests passed on each |
| CI-box Linux, Python 3.12.16, server/UHD regression group | 93 passed; 6 subtests passed |
| CI-box Linux, Python 3.12.16, full host suite | 147 passed; 39 subtests passed |
| Fresh core/sample wheels, macOS 3.12 and Linux 3.14 | Installed-package smoke passed without native dependencies |
| Sample wheel installed but disabled, macOS 3.12 and Linux 3.14 | Required client/server UHD loopback passed on each |

The server regression group is `test_usrp_client_server_localhost.py`,
`test_dynamic_v2.py`, `test_usrp_preload.py`, `test_default_usrp_schema.py`,
`test_usrp_profiles.py`, `test_reservation_handoff.py`, `test_diag.py`,
`test_host_dynamic_v2.py` and `test_federated_dynamic_v2.py`. It includes existing
raw reopen-after-close-error and legacy-eviction behavior. Test groups run in
separate Python processes to avoid sharing the mutable driver registry.

Server/host tests installed this branch's device-core and federation-core 0.22.0;
the separate client environment installed its declared federation-core 0.16.0.
An additional Linux 3.14 run passed the same core/server regression groups and
146 host tests, with one existing TLS fixture skipped because of stricter
certificate validation (missing Authority Key Identifier). Rechecking under the
CI Python 3.12 line passed all 147 host tests. That 3.14 observation remains a
compatibility follow-up, not evidence of full 3.14 host qualification.

The skipped legacy pipeline requires explicit historical-version fixtures and
was not claimed as passing. No physical-radio, OAI, deployed Global relay or
production soak test was performed. These remain gates for the ownership/RPC
increments; the current sample cannot exercise them.


## Phase 1b evidence from November 9, 2026

The internal runtime (`c7320c3`) and shared native-lifetime changes were tested in isolated
macOS environments and `/tmp/remoterf-phase1b-source` on CI-box. No dev/production
service, inventory, reservation or physical radio was changed. New tests use
deterministic fake native resources and adapters; the required UHD loopback uses
the real client/server transport with fake hardware.

| Environment and scope | Result |
|---|---|
| macOS Python 3.12.10, full non-hardware server suite | 920 passed, 5 opt-in historical legacy-pipeline skips; 97 subtests passed |
| macOS Python 3.12.10, full host suite | 159 passed; 39 subtests passed |
| macOS Python 3.12.10, client suite excluding integration directory | 626 passed, 1 optional MCP skip; 25 subtests passed |
| macOS / CI-box Linux Python 3.12, shared-core suite | 78 passed; 9 subtests passed on each |
| CI-box Python 3.12.16, expanded server/UHD regression group | 127 passed, no skips; 6 subtests passed |
| CI-box Python 3.12.16, full host suite | 159 passed; 39 subtests passed |
| Fresh core/sample wheels without native dependencies, macOS and CI-box Python 3.12 | Metadata/opt-in and durable asynchronous lifecycle smoke passed |
| CI-box installed core wheel and disabled sample wheel | 78 core tests and 21 server ownership/native/required-loopback tests passed in separate processes |

The expanded server group adds `test_native_lifetime.py`,
`test_integration_ownership.py`, `test_home_device_plane.py`, `test_bind_host.py`
and `test_device_init.py` to the Phase 1a group. The Linux installed-wheel run
replaces the editable core install. Core and server tests run in separate
processes because their driver-registry fixtures are not mutually isolated;
combining them causes an unrelated load-order assertion failure. A narrow macOS
diagnostic/native group passed all 40 tests with fresh XDG directories. Reusing
prior test configuration exposed three `prompt_toolkit` closed-stdout failures;
the same three failures reproduce on the pre-1b `f62da0a` source (25 diagnostic
tests pass there with fresh configuration). This is recorded as a test-environment
isolation limitation, not hidden by weakening assertions.

New coverage includes late child publication, pending RX/TX worker retention,
failed-open secondary cleanup, inventory/token changes during admission, expiry
during open/start/reacquisition, duplicate requests, bounded outcome retention, hung start/stop, independent device cleanup,
slow/disk-full/corrupt/duplicate-key journals, writer exclusion, abrupt process
exit and restart fencing, legacy/reload/availability guards, deferred reset,
shutdown exceptions/queue failure and in-process restart after native drain.
An unknown native/adapter failure produces a phase-specific safe code; exception
messages and credentials are not copied into cached operation status.

Stock driver/schema/protocol/client files are unchanged relative to Phase 1a.
No supported older-client matrix, actual hardware release/reacquisition,
deployed Global/direct-route test, service SIGTERM/orphan-process recovery or
production throughput/soak qualification is claimed. Defaults remain provisional
until those gates pass; application activation remains unavailable.
