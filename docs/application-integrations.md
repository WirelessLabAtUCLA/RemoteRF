# Application integrations: experimental contract 0.1

This is the first implementation increment of the generic application framework.
It supplies metadata, explicit enablement, lazy construction, and plain-data
validation for independently packaged adapters. It is not yet a live server or
host feature. There is no new RPC, radio ownership transfer, process runner or
OAI dependency. Existing UHD drivers, session behavior and stock schema hashes
are unchanged by this increment.

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
The registry is a configuration snapshot; replacement, draining and package
upgrade coordination belong to the future runtime.

These methods are primitives for a trusted runtime. They are not authentication
or a remote API: a future runtime must derive device identity from reservation
authority, construct the context, and acquire ownership before starting resources.
Never expose `create()` or the allow list directly to a remote caller.

## Lifecycle contract

The factory takes a frozen `Context` with device ID/type, generation, owned work
directory and cancellation event. The context contains no bearer token or native
UHD object. Hardware selectors, resource registration and supervision services
will be added with the lease implementation.

| Hook | Responsibility |
|---|---|
| `validate(profile, configuration)` | Return normalized configuration without acquiring resources. |
| `start(profile, configuration)` | Return after useful readiness, or raise on failure. |
| `status()` | Return a bounded plain-data object. |
| `call(action, parameters)` | Handle a declared action and return bounded plain data. |
| `stop()` | Release resources idempotently, including after partial startup. |

Construction must be side-effect free. Hooks are synchronous; the future runtime
must schedule blocking work outside RPC/reservation locks with bounded queues,
deadlines, cancellation, operation IDs and generation checks. Its wrapper must
validate normalized configurations and every status/action result using
`object_data()`. Direct Python calls to an adapter are not automatically wrapped.

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
enablement, validation, readiness, an action, cancellation and repeated cleanup.
It runs with no native dependencies installed. The sample uses simulated device
type `sim_sdr` and cannot acquire a radio. This is a packaging/contract example,
not proof of hardware handoff or the complete adapter conformance suite.

CI now runs core contract tests, builds and exercises both wheels, installs the
sample into the server test environment while leaving it disabled, and reruns
the required client/server loopback. The loopback imports current `remoterf` and
fails collection if that import breaks instead of silently skipping. Hardware
tests also use the current namespace but still require their explicit hardware
configuration.

## Phases and remaining release gates

1. **Regression repair:** required localhost loopback restored; hardware test
   source discovery/imports corrected.
2. **Contract foundation (this increment):** registry, payload rules, context,
   independent non-radio sample, failure/limit tests and wheel proof.
3. **Ownership runtime:** admitted native calls and handle publication, closing
   worker tracking, reservation/diagnostic/reset coordination, bounded async
   lifecycle, durable recovery and native reacquisition. Preserve raw-only
   replacement/reopen-after-close-error behavior. Keep integrations unavailable
   through all server/host routes until these protections are wired and tested.
4. **Generic RPC and hardware sample:** stable schema surface and approvals,
   retry-safe operation IDs, host unsupported-feature guard, old-client checks,
   all-route tests and a separately packaged adapter proving real hardware
   release/reacquisition. Then qualify optional OAI through the same contract.

Do not treat this increment as completion of Phase 1 in the implementation plan.
It does not establish reservation expiry cleanup, crash recovery, production
load limits or real-radio compatibility. No package has been published and no
deployment has been upgraded. Version bumps and server/host dependency pin
updates are required when a shared-core release is prepared.

## Evidence from November 9, 2026

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
