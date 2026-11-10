# Application integrations: experimental contract 0.1

Phase 1c adds a generic remote API, operator configuration, qualification checks
and Linux process supervision to the Phase 1a/1b contract and ownership runtime.
It is a real shared-runtime and schema change that requires UHD regression and
schema-migration review. It adds no OAI dependency or application-specific RPC.
Third parties implement the same public adapter contract in separate packages.

**Existing deployments remain disabled by default.** A fresh deployment with no configuration or retained ownership state loads no
adapters and opens no process supervisor or application ownership journal.
Installing an adapter does not enable it. The server retains eager native UHD
opening. Starts require explicit operator enablement and matching hardware
qualification evidence; a caller cannot supply either through RPC. Implementation
and deterministic tests are not deployment approval or hardware qualification.

The current activation scope is one serial-addressed, lab-local plain `usrp`.
Host-attached integrations return `unsupported_host`; Phaser and ambiguous or
composite selectors cannot activate applications. Their existing native routes
remain supported. OAI, user-submitted code, bulk artifacts and host integration
execution are deferred.

## Public API on the existing USRP root

All six methods accept one required `request: dict` and return a plain dictionary.
They use existing Dynamic v2 Invoke transport and the owning `MultiUSRP` session;
there is no second token-taking root or independent application reservation.

| Method | Request fields | Result |
|---|---|---|
| `integration_list` | Optional `cursor`, `limit` (1–8; default 8) | Contract version, current native `generation`, availability, metadata rows and `next_cursor`. |
| `integration_describe` | `integration_id` | Manifest metadata and coarse availability; no factory path. |
| `integration_start` | `integration_id`, `profile`, `configuration`, `request_key`, `generation` | Asynchronous operation snapshot with `operation_id`. |
| `integration_status` | `operation_id`; optional `action_id` | Cached operation or action snapshot. |
| `integration_stop` | `operation_id` | Cancellation/cleanup snapshot; acknowledgement is not release proof. |
| `integration_call` | `operation_id`, `action`, `parameters`, `request_key` | Asynchronous action snapshot with `action_id`. |

Unknown fields are rejected. Configuration and parameters are objects. IDs,
profiles, actions and request keys are nonempty strings of at most 128 characters;
generation is a positive integer, not a boolean. Discovery distinguishes
`configured`, `compatible`, `qualified_profiles` and `enabled`. A row is enabled
only with matching profile evidence, operator enablement, supervision and a
non-draining controller. The coarse `application_integrations` session capability
is deliberately conservative; query `integration_list` for current availability.

For an already qualified and enabled radio, using an existing owning root:

```python
page = radio.integration_list({})
request = {
    "integration_id": "org.remoterf.usrp_probe",
    "profile": "receive",
    "configuration": {"rate": 1_000_000, "frequency": 100_000_000, "gain": 0},
    "request_key": "receive-attempt-1",
    "generation": page["generation"],
}
operation = radio.integration_start(request)
status = radio.integration_status({"operation_id": operation["operation_id"]})
# Poll with a bounded delay/deadline. Call declared actions only after RUNNING.
# Keep the original request for a lost-reply retry; do not mint a second key.
radio.integration_stop({"operation_id": operation["operation_id"]})
```

Start and action retries with identical inputs attach to their recorded outcome;
changed inputs under the same key fail with `request_conflict`. Operations are
bound to the exact session/generation/physical radio. Knowing an operation ID
never grants authority. Every remote call checks the current reservation/root.
A completed handoff reports `native_generation`; released status also includes
bounded refreshed `native_capabilities`, or `native_capabilities_truncated`.
Refresh discovery before a new start after native reacquisition. Retry outcomes
are retained until their owning session ends, so capacity exhaustion is explicit.

Integration refusals use the existing protocol-error envelope with a safe
`details.integration_code`, such as `integration_disabled`,
`qualification_required`, `unsupported_host`, `device_busy`, `stale_owner`,
`request_conflict`, `overloaded` or `recovery_required`. Existing native errors
keep their established behavior. Status never invokes adapter hooks or reads
process logs; action execution and status refresh run on bounded workers.
`RUNNING` and cached `ready` describe the last completed lifecycle/status check,
not a continuous heartbeat. An adapter can mark a long-lived process
`required_while_running=True`; its exit, including exit zero, independently
reports `process_exited` and starts serialized cleanup without a follow-up RPC.
Status remains cached: the supervisor records exits asynchronously and the
runtime watcher reads only a nonblocking failure signal. The receive-only probe
opts in. Finite jobs default to `False`; their exit alone does not stop the
application or release its hardware lease. The declared `health` action remains
available for adapter-specific checks beyond process liveness.

## Third-party adapter package

The public interface lives in `remoterf_device_core.integrations`. Importing the
contract does not import UHD, NumPy or the device-schema registry. The operator
supplies an explicit manifest path; there is no automatic package scan/import or
client-controlled factory. A manifest example is:

```json
{
  "id": "org.acme.receiver",
  "version": "1.0.0",
  "contract_version": "0.1",
  "factory": "acme_receiver:create_adapter",
  "device_types": ["usrp"],
  "profiles": ["receive"],
  "actions": ["health"]
}
```

Ship this metadata as package data in an independently installed distribution.
Discovery reads it without executing the adapter. Contract `0.1` matches exactly.
IDs must be namespaced; profile/action/device-type names follow the manifest's
identifier rules. Duplicate IDs, unsupported contracts/profiles/actions and
malformed metadata fail with typed errors. Adding or upgrading an adapter alone
does not modify stock USRP manifests, transport dispatch or client generation.

`create_adapter(context)` returns an object implementing these synchronous hooks:

| Hook | Responsibility |
|---|---|
| `validate(profile, configuration)` | Return normalized plain data without acquiring resources. |
| `start(profile, configuration)` | Return only after useful readiness, or raise. Observe cancellation. |
| `status()` | Return a bounded, sanitized plain-data status object. |
| `call(action, parameters)` | Perform a declared action and return bounded plain data. |
| `stop()` | Release resources idempotently, including after partial startup. |

Construction must be side-effect free. Frozen `Context` supplies `device_id`,
`device_type`, `generation`, an owned `work_dir`, cancellation event, trusted
`selector` (currently the exact serial), and owner-bound `processes`. It contains
no reservation token or native UHD object. Never take another device selector,
executable, arbitrary environment or file path from an unvalidated remote input.

Use `context.processes.launch(argv, env=..., cwd=context.work_dir)` with an
absolute executable and explicit environment. Use its `snapshot`, `log_tail`
and `stop` services for owned children. Raw logs are local trusted data; redact
secrets and expose only bounded summaries through the adapter. The runtime also
attempts supervised cleanup if adapter stop raises. Only the runtime verifies
release and returns the hardware lease; an adapter's success flag is not proof.
Pass `required_while_running=True` only for a worker that must remain alive while
the application runs. Detection does not wait for stdout/stderr EOF, so inherited
pipes held by descendants cannot conceal the worker's exit. Cleanup still proves
the entire owned cgroup empty; the exit signal itself never releases ownership.
A failed process monitor also sets a cached failure and refuses further launches.
It never substitutes for independently verified process/native release.

Adapters are trusted operator-installed Python code. Threads do not contain a
native crash, GIL stall or hung import/hook. Put incompatible native dependencies
and crash-prone application work in supervised processes and keep callbacks
bounded. Do not replace the server's working UHD/native libraries to install an
adapter. External executables/images require separate recorded qualification;
the package fingerprint does not hash arbitrary binaries outside the package.

The independent [counter sample](../examples/integrations/counter) proves the
package/contract path with `sim_sdr` and no radio. The independently packaged
[receive-only USRP probe](../examples/integrations/usrp_probe/README.md) uses the
same interface, supervised RX, and a `health` action; it never transmits. Neither
package is installed or enabled automatically by the runtime.

## Operator configuration and qualification

The server reads `integrations.json` in its RemoteRF configuration root before
opening native inventory.

On Linux, keep `$XDG_CONFIG_HOME/remoterf` aligned with `REMOTERF_CONFIG_DIR`
when isolating a deployment. Setting only an arbitrary `REMOTERF_CONFIG_DIR`
does not relocate the integration configuration/state root used by `db_location`.

This example stays disabled and names no receipt:

```json
{
  "version": 1,
  "enabled": false,
  "manifests": ["integrations/acme_receiver.json"],
  "devices": {
    "7": {"serial": "EXACT_SERIAL", "integrations": ["org.acme.receiver"]}
  },
  "qualifications": [],
  "cgroup_root": "/sys/fs/cgroup/operator-delegated/remoterf"
}
```

Manifest and receipt paths are operator-controlled, relative to this config file
or absolute. `cgroup_root` must be an absolute operator-created path. Config
version must be the integer `1`; `true` and `1.0` are rejected. Missing/broken
optional adapter metadata cannot import the adapter or release native hardware.
Malformed optional configuration disables integrations. Unresolved hardware or
process owners still block unsafe startup even if configuration/package files
were removed. Disabled configuration with no unresolved owner adds no selector
restrictions to existing raw inventories.

For activation, configured gids must match inventory serials. Every alias of a
qualified physical serial shares one durable gate. Broad/address/composite or
Phaser selectors are rejected while this mapping is active because overlap
cannot be ruled out. Changes to the installed physical alias mapping require a
drained restart; inventory reload cannot introduce an unfenced alias. An active
native session, legacy inventory object, child handle, worker, opening alias or
unfinished cleanup prevents handoff before existing UHD resources are released.
Audit the entire inventory before activation: even an unrelated broad/address/
composite USRP or Phaser record can make the enabled mapping fail with
`ambiguous_hardware`. The disabled, owner-free path keeps its existing behavior.

Qualification is a trusted operator record, not a client flag, signature service
or evidence generator. For **each physical serial and adapter profile**, the
record must exactly match the adapter ID/version/hash, native session schema
hash, UHD version and framework hash. Every required check must be the boolean
`true`, `passed` must be `true`, and `run_id` must identify retained evidence.
The following is deliberately a **non-approving template**:

```json
{
  "qualification_version": 1,
  "serial": "EXACT_SERIAL",
  "integration_id": "org.acme.receiver",
  "adapter_version": "1.0.0",
  "adapter_hash": "REPLACE_WITH_RECORDED_ADAPTER_SHA256",
  "profile": "receive",
  "schema_hash": "REPLACE_WITH_QUALIFIED_SESSION_SCHEMA_HASH",
  "uhd_version": "REPLACE_WITH_MEASURED_UHD_VERSION",
  "framework_hash": "REPLACE_WITH_RECORDED_FRAMEWORK_SHA256",
  "run_id": "REPLACE_WITH_REVIEWED_EVIDENCE_ID",
  "passed": false,
  "checks": {
    "native_rx_before": false,
    "application_rx": false,
    "native_rx_after": false,
    "explicit_stop": false,
    "startup_failure": false,
    "expired_start": false,
    "expired_running": false,
    "process_crash": false,
    "cleanup_descendants": false,
    "next_holder_rx": false
  }
}
```

Do not turn these fields true without the corresponding hardware evidence.
Review and install the receipt under an operator-protected path, then reference
it in `qualifications`; enable only the explicitly qualified deployment/device.
Global schema approval is a separate gate and is not replaced by this receipt.

The current receipt matches a **profile name**, not an enforced configuration
envelope. Qualification at one rate/frequency/channel/gain/sample count does not
qualify every value accepted by an adapter's validator. Before production
activation, constrain the enabled profile to its qualified settings or add an
enforced qualification envelope and test its supported bounds. The example
probe's broad validator is not evidence for that entire range. Extra receipt
annotations such as `dev_canary_only` or `production_approved` are not runtime
enforcement; keep dev receipts and enabled configuration private to the canary.

`integrations.fingerprint.adapter_digest(manifest)` returns the unprefixed
64-character adapter SHA-256 without importing it. It binds manifest/factory,
package source/native/resource files, and installed dependency version metadata.
Namespace packages are supported. Missing distribution metadata, symlinks,
excessive scanning or changed files fail closed. `deployment.code_digest()` binds
integration modules plus the server controller, Dynamic v2, device-manager and
server lifetime/transport (`grpc_server.py`) files supplied at bootstrap. Capture these exact digests from the build under
test; do not substitute a package version or hash of an unrelated checkout.
Startup caches these identities; the worker rechecks both before creating its
work directory/cgroup or releasing native UHD. Package or framework changes
invalidate the old qualification. Drain/restart for config or package upgrades.

Main-thread server entry points handle SIGTERM through their normal startup/run
cleanup and restore the previous handler afterward. Admission closes before
transport drain. Repeated termination cannot interrupt cleanup, and termination
during a pending restart suppresses re-execution. Embedded off-main-thread callers
retain responsibility for process signals. Native crashes or GIL stalls still
require process isolation; graceful signal handling does not contain them.

## Linux process support and recovery

Application process ownership requires Linux cgroup v2, an exclusive delegated
domain, `pids`/`memory` controllers and `cgroup.kill`. Missing prerequisites fail
closed; there is no PID-name or process-group fallback. Children enter a pinned
owned cgroup before target execution. The bootstrap gets an empty environment;
the target gets only the explicit environment after admission. Descendants stay
inside the owned domain, including double-fork/session-detach attempts covered
by the Linux suite. This is supervision of trusted code, not an arbitrary-code
sandbox or proof of cleanup for external interfaces/shared memory/files.

The durable process registry records boot, delegated-root and scope identities;
hardware ownership records link the exact resource owner. Startup reconciles
these records before inventory acquisition. Unknown, replaced or populated
unlinked scopes block startup. Verified empty unlinked scopes may finish cleanup;
uncertain scopes are never assumed gone because a service restarted.

Native admission is fenced before durable ownership is written. Persistence,
fingerprint rechecks, resource creation, native release, adapter hooks and
reacquisition run outside RPC locks. Stop/expiry waits for active hooks to finish;
timeout records failure without pretending code stopped. Native reacquisition
occurs only for still-valid authority and refreshes metadata/root generation.
Radio settings and continuous timestamps are **not** preserved promises.

Close invalidates public native handles immediately but retains real resources
until active calls/workers end. A refused handoff leaves ordinary UHD usable.
Raw-only reopen after a completed close error remains supported; unfinished
cleanup blocks reopen/reset/diagnostic/reload. This intentional compatibility
change applies even with integrations disabled. Legacy paths, master access,
new opens and diagnostics must honor the same physical gate.

Stop new starts before shutdown or rollback. Keep unresolved owners unavailable.
Do not delete journals, kill a PID copied from a file, or remove an adapter to
force reuse. Offline trusted recovery uses `CgroupSupervisor.describe/reopen`,
identity-checked scope stop/release, and the ownership gate's positive verification;
`reconcile_released` handles only its narrow verified-release crash window.
There is no remote recovery RPC. The local-only operator CLI and its managed
resource limits are documented in the [recovery runbook](../tools/qualification/README.md#offline-managed-resource-recovery).
Nonempty work directories are retained for review, not recursively deleted.

Session expiry autonomously stops resources and releases the hardware lease only
after verification. The next valid generic request or service shutdown schedules
empty-scope metadata retirement on the shared bounded cleanup pool; bounded empty
scope records can remain until then without retaining hardware or live processes.
Rejected scheduling retains the owner for retry; failed cleanup remains recorded
for operator reconciliation. Discovery never waits for filesystem cleanup.
Controller shutdown allows at most 0.5 seconds total for runtime stop work and
subsequent scope-close jobs, then
refuses completion if ownership/cleanup remains unresolved. No timeout is a
release assertion. Roll back code only after resources and journals are resolved.

## Wire and resource bounds

The integration boundary validates raw JSON **before** the existing UHD
helper/handle decoder on server and host Invoke entry points. It accepts exactly
one named `request`, or the generated positional wire wrapper `__args__:[request]`,
and overload `request` (or the legacy empty overload). No extra kwargs, duplicate
arguments or duplicate JSON fields are accepted. Reserved tags are forbidden
recursively inside request/result objects; native/binary DynamicValue variants
are rejected. Normal UHD codecs and streaming remain on their existing paths.

| Bound | Current default |
|---|---|
| Full encoded protobuf Invoke request | 128 KiB, below the relay request cap |
| Plain object payload/reply | 64 KiB; exact built-in JSON types, signed 64-bit integers, finite floats, valid Unicode |
| Payload shape | 16 levels, 4,096 value nodes; lists are allowed; no bytes, NumPy/native arrays, tuples or conversion hooks |
| Manifest / registry / device declarations / receipts | 16 KiB per manifest; 64 entries each; operator files 64 KiB |
| Discovery | At most 8 rows and 48 KiB before envelope headroom; cursor pagination |
| Cached adapter status / action result | 48 KiB each; native refresh metadata 12 KiB |
| Retained controller bindings / runtime operations / action records | 16 / 16 / 64; completed retry outcomes still count |
| Start/action workers / cleanup workers | 4 / 4, bounded queues; 30-second work/cleanup deadlines |
| Process scopes / launched processes per scope | 64 / 4 retained launches |
| Process scope kernel limits | 128 PIDs, 512 MiB memory, no sub-cgroups |
| Process logs | 64 KiB tail per stdout/stderr stream, continuously drained; not a remote artifact API |
| Journal / shared native cleanup | 64 owners, 64 KiB; 4 cleanup workers, queue 64, at most 8,192 retained jobs |
| Adapter fingerprint scan | 4,096 entries, depth 32, 64 MiB/file, 128 MiB total, 16 roots, 256 distribution records |

Reserved keys are `__remoterf_handle_arg__`, `__remoterf_handle__`,
`__remoterf_type__`, `__uhd_type__`, `__remoterf_uhd_helper__`,
`__remoterf_json_type__`, `__args__` and `__kwargs__`. There is no bulk binary,
chunk/artifact upload/download, unbounded log reader, or per-adapter disk quota.
Do not use work directories for unbounded outputs. These are defensive software
bounds, not measured production capacity, latency or isolation guarantees.

## Version and schema migration gate

Prepared development versions are core `0.1.7.dev0`, server `1.7.23.dev0`, host
`1.3.9.dev0` and client `2.13.13.dev0`; server/host pin that core. They are not a
claim of package publication. USRP driver `0.0.8` and Phaser driver `0.1.1` include
the generic root methods in v2; no new protobuf method or OAI class is required.
Both **v1 and v2 stock hashes change**, including the v1 version metadata:

| Driver | v1 | v2 |
|---|---|---|
| `usrp` | `sha256:b4483acb336adac00dd57c647fe50927a5ce4fee91ad7fa5d13c9f4195c8d19f` | `sha256:7276bf3ab3eefcb55270abe10311fb000a1897a0337b63a7810c7a107306384b` |
| `usrp_phaser` | `sha256:d47556615f614e657792e5da820add2d6f69a337ea0ae84be966186886a96f74` | `sha256:61ba38ea1a9190f14c6bcfbfc6678ecc2538f99cb2a81168f8b4824c3074e4c0` |

Review regenerated `schemas.json`, client snapshot/stub and schema hashes
against the exact release build. Obtain required Global hash approvals **before
any rollout using these new stock hashes**, including a dev canary. Keep the raw
UHD canary separate from application activation. Phaser schema inheritance does
not mean Phaser applications are supported. Adapter-only upgrades leave these
stock hashes unchanged but still invalidate adapter qualification as appropriate.

The prior client binder was checked against both new manifests: existing native
methods and all six generic methods bind. Cached older schemas retain the
existing explicit refresh/reimport-and-retry behavior; no cache bypass was added.
This is focused compatibility evidence, not a released multi-version/live Global
matrix. Generated stubs now annotate known primitives; unknown/native types
remain `Any`. Test raw client/server/host compatibility before publishing.

## Verification and remaining release gates

Keep three decisions separate: rollout of the framework with integrations
disabled, activation of a qualified adapter, and later OAI support. Passing an
isolated dev radio run does not authorize production activation. Remaining
production work includes:

- A shipped, reviewed service/delegation setup with stable ownership identity
  across server restarts and independent worker cleanup after server/native
  failure. Temporary qualification guardians do not ship this containment.
- Aggregate service CPU, memory, task and retained-directory budgets, followed
  by extended physical-radio soak and ordinary UHD responsiveness/fairness checks.
  Per-scope limits and fake-radio load tests do not establish these budgets.
- Recovery/rollback rehearsal: stop admission, drain, verify managed processes,
  hardware and journals, restart disabled, restore supported prior artifacts and
  schemas, then receive through ordinary UHD. Never downgrade over unresolved
  ownership. Reboot/replaced cgroup identities need an explicit operator procedure.
- A declared supported configuration, inventory, hardware, native-UHD, client
  and route matrix; required release/schema/receipt approvals remain separate.
  Receive-only evidence does not qualify TX or unsupported application profiles.

The [Phase 1c evidence summary](qualification/phase1c-2026-11-09.md) links the
recorded software, Linux-process and receive-only hardware results.

The later [dev rollout verification](qualification/phase1c-dev-rollout-2026-11-09.md)
adds deployed synthetic relay/direct routes, three published clients, actual
calendar expiry, service stop/restart, restoration and measured process/runtime
soaks. It also records the required-worker/monitor and SIGTERM cleanup fixes.
Final suites passed core 248, server 1,017 and host 176 tests; documented opt-in
skips remain. The later [exact-build UCLA verification](qualification/phase1c-ucla-exact-build-2026-11-09.md)
qualifies isolated receive-only handoff, autonomous worker cleanup, positive
surviving-process recovery and actual SIGTERM cleanup on that same framework.
Deployed raw UHD receive passed afterward; production remained unchanged and the
reservation was released. An initial failed follow-up receive is retained with
verified cleanup and its diagnostic limits. This is not a production activation
receipt, deployed generic-radio RPC test, real-radio calendar-expiry test or
extended RF soak; the remaining gates in that report still apply.

The [normal-auth cold-process follow-up](qualification/phase1c-cold-auth-2026-11-09.md)
passed 12 real-radio first opens with no test-only warmup and unmodified local
account/calendar/token validation, including six under a one-CPU ceiling. All
opens took 4.709–4.933 seconds and received successfully. CI verified valid slow
opens plus actual private-calendar expiry/cancellation refusal and next-holder
reuse. This closes that narrow cold-process concern for the tested exact build;
the original harness failure's precise cause remains unproven. Public new-server
startup/Global routes, physical power-cycle starts and broader production gates
remain separate.

The [November 10 real-radio dev Global run](qualification/phase1c-ucla-dev-e2e-2026-11-10.md)
then passed normal private-server startup, missing/mismatched/matching receipts,
the generic client lifecycle, receiver-descendant cleanup and actual Global
cancellation/expiry on the unchanged exact build. Independent observations
preceded every post-revocation device RPC; next-holder and unchanged production
UHD receive passed. The temporary account, schema approval, deployment and work
lease were cleaned up, and the physical booking was released. CI passed 83
focused checks without skips. This closes those scoped dev/RX gaps for the
tested radio and configuration; the production gates above remain open.

Deterministic tests cover disabled/broken packages, raw request rejection before
native construction, current authority and cross-session IDs, start/action
retries, cancellation/expiry, journal failures, alias publication/reload races,
metadata refresh, cleanup backpressure, hung/failed cleanup and startup orphans.
Local generated-client gRPC, federation bridge, host refusal and loopback direct
ICE/QUIC routes are exercised with fake radio/controller state. These do not
establish deployed Global authorization, real calendar expiry or real-radio RF
behavior. The [hardware harness](../tools/qualification/README.md) requires an
explicit held reservation and produces evidence, never deployment enablement or
a qualification approval.

Phase 1c regression runs recorded during implementation:

| Scope | Result |
|---|---|
| macOS shared core | 227 passed, 4 skips |
| Linux installed-wheel shared core | 229 passed, 2 delegated-cgroup tests skipped |
| macOS / Linux host | 176 passed on each |
| macOS client | 634 passed, 1 optional skip |
| Linux installed-wheel server | 988 passed, 5 opt-in historical legacy-pipeline skips |
| Final macOS non-hardware server, completed source | 996 passed, 5 historical legacy skips; 97 subtests; 77.52 seconds |
| Required socket client/server loopback | 1 passed, no skips |
| Startup-abort cleanup | 8 cases passed: certificate/inventory/transport/direct-path failures, with and without cleanup failures |
| CI-box affected startup/lifecycle/controller group | 66 passed |

Focused controller/routes/native-lifetime tests also passed (83); earlier client
schema/binder/schema-review tests passed (45 and 4 subtests). These runs are not
the complete deployed release matrix. CI also builds and smoke
tests independent sample wheels and runs the required client/server loopback
with samples installed but disabled. Run core, server, host and client suites
in separate processes/environments where their driver-registry/dependency
fixtures differ; use fresh test configuration directories. Startup-abort tests
exercise the real journal writer lock with fake hardware/transports: admission
stops, independent cleanup is attempted, the original failure (including an
interrupt) is preserved, and an idle journal can be reopened immediately.
Uncertain ownership still blocks completion. The cleanup retry test waits for
the worker's completion marker, not just the fake scope's earlier `closed` flag.

### Isolated UCLA receive-only evidence — November 9, 2026

The [recorded hardware run](qualification/phase1c-ucla-2026-11-09.json) passed
16:23:55–16:24:42 PST on UCLA USRP00, gid 40, serial `32C78EE`, under approved
booking 1020. It used UHD `4.10.0.main-release`, core `0.1.7.dev0`, server
`1.7.23.dev0` and the independently installed probe `0.1.0`; exact source digests
are retained in the report. RX used channel 0, 1 MS/s, 100 MHz, 0 dB and finite
4,096-sample checks. No TX was performed.

Native RX → supervised application RX → native RX passed, including partial
startup failure after RX, process exit, a delayed stop retaining ownership past
its deadline, injected local expiry during startup/running, and next local-holder
RX. Abrupt test-process death left a surviving application; restart refused native
acquisition until identity-verified cgroup cleanup, after which native RX passed.

This exercised an **isolated local SessionManager/Runtime**, with a cached view
of a real held reservation and injected local revocation. It did not exercise
the deployed generic RPC controller/receipt gate, Global relay/direct fallback,
actual calendar expiry, another hardware profile, Phaser, multi-radio aliases,
OAI, load/soak or production service shutdown. The raw evidence is not an
activation receipt and cannot be installed as one: its structured observations
and check names differ from the exact approval format above. It changed no
activation setting or schema approval.

After that cleanup, a [separate raw-UHD check](qualification/phase1c-ucla-global-rx-2026-11-09.json)
used branch client `2.13.13.dev0` through the unchanged deployed Global/UCLA
service with direct routing disabled. Native open took 2.58 seconds; exact serial
verification, tuning and 4,096 finite RX samples passed, and streamer/root closed.
It used deployed schema
`sha256:69159a5e174635038b732093f12b78f412c422e281bf2855935f8d121f8c7144`.
This is old-server/client compatibility and post-cleanup native proof, **not**
deployment or qualification of the new generic API; no TX was performed.

Keep activation disabled until the exact deployment build, controller/authority
routes and required hardware checks are reviewed and an explicit matching
operator receipt and rollout approvals exist. The isolated pass supplies real
handoff evidence for its recorded radio/profile; it does not waive the remaining
gates. Service death/restart, process recovery, retained settings,
metadata/timebase assumptions, installed-package behavior, supported older-client
routes and measured load/soak still require applicable deployment evidence.
No OAI, host execution, artifacts or production readiness is inferred from this
increment. Schema approval, release publication and deployment activation remain
separate pending decisions.

The following historical sections describe earlier increments only; their
statements about absent features apply to those recorded versions.

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
