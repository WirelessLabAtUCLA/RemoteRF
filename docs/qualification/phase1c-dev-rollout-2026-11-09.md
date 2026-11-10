# Phase 1c dev rollout verification — November 9, 2026

The final framework passed dev Global generic-route, calendar, released-client,
service-stop/restart and rollback checks. Dev has been restored. The 10-minute CI runtime soak passed 22,856
lifecycle cycles; the final server-wrapper regression is recorded separately
from the measured runtime source below. No production host, package,
inventory, schema approval or integration setting was changed. Nothing was
pushed or published. All new radio traffic was synthetic.

## Fixes discovered by qualification

1. Required long-lived workers could exit while cached application status stayed
   `RUNNING` until an explicit health action. Adapters can now opt into
   `required_while_running=True`; a cached exit signal triggers serialized
   cancellation and independent resource-release checks. Finite jobs retain the
   default behavior. Monitor-thread failure also latches a failure and refuses
   new launches; a monitor failure alone is never process-release proof.
2. Controller shutdown's existing 0.5-second grace covered scope removal but
   could miss ordinary runtime stop work. One total bounded grace now covers
   both, retaining ownership when completion is uncertain. RPC calls do not
   wait on this grace.
3. A real dev `systemctl stop` exposed default SIGTERM termination bypassing
   cleanup. The server now installs a lifetime-scoped main-thread handler before
   startup, unwinds through cleanup, closes admission before transport drain,
   ignores repeated SIGTERM during cleanup and restores the prior handler. A first
   SIGTERM during pending-restart cleanup also suppresses re-execution.
   Startup, control-socket publication and agent/welcome failures are covered.
   Embedded off-main-thread callers retain control of their signals.

The last fix affects ordinary server shutdown as well as integrations. Real
signal subprocess tests cover all three server entry points, startup interruption,
active application ownership and empty-journal cleanup. Native crashes, an
uninterruptible native call or a GIL stall still require separate process
isolation; signal handling does not provide that containment.

## Exact final build

[Dev evidence](phase1c-dev-routes-2026-11-09.json) contains module hashes, route
assertions, client versions, independent expiry observations and restoration.
All 16 deployed integration/server module hashes matched the final worktree.
The receipt digest now includes `grpc_server.py`, so service-lifetime changes
invalidate prior qualification as well as controller/native/runtime changes.

| Installed wheel | SHA-256 |
|---|---|
| core `0.1.7.dev0` | `9d3b88258c7a6298c460f50468a6385bc59d64c72e763dcb21719d230e331d97` |
| server `1.7.23.dev0` | `f9b382782d457bb7197c22b1bc77f310075d103ea5a688f9f4923d1249397d4e` |
| isolated dev fixture `0.1.3` | `d6e40fae028148dc1287a6ad091932d8078757d599ce3b1d0e5eeab1d017c716` |

Framework digest:
`8697958f27a1b1289cc45093258cb3eeed84bdb631cacc122a055f21a3a7d41a`.
The fixture's normal receipt gate uses fake UHD identity
`dev-fixture-no-hardware` and explicitly records `physical_radio_qualification:false`.
It cannot qualify a real radio. The fixture lives outside production packages,
refuses other hostnames/users/inventories and never imports native UHD.
Development version numbers alone do not identify these tested artifacts.

## Software and process checks

Final CI-box tests ran in separate processes/configurations using installed
core/server wheels. Server collection initially needed explicit isolated test
ports/bind settings, which were supplied before the successful full rerun.

| Check | Final result |
|---|---|
| Shared core | 248 passed; 4 explicit delegated-cgroup opt-in skips; 9 subtests |
| Non-hardware server | 1,017 passed; 5 historical legacy opt-in skips; 97 subtests |
| Host | 176 passed; 39 subtests |
| Probe/hardware-harness regression | 30 passed |
| Required socket client/server UHD loopback | 1 passed, no skips; actual codecs/socket, fake radio |
| Independent counter/probe wheel smoke | Passed without native UHD |
| Actual delegated-cgroup tests | 4 passed, no skips, on the final core wheel |
| Dev fixture tests | 29 passed, including driver loading and gRPC synthetic RX/TX |
| Local SIGTERM/startup/native/controller regression | 81 passed |
| Final installed-wheel signal and required socket regression | 9 passed, no skips |
| Published-client offline compatibility | 8 checks each on 2.12.0, 2.13.9 and 2.13.12 |

The [process stress report](phase1c-ci-process-soak-2026-11-09.md) distinguishes
three exact builds and links the authoritative final evidence. The final run
passed 100 cycles in 101.09 seconds: normal descendants, launch failures, abrupt
controller exits with offline identity-checked recovery, a 16-owner wave and
admission limits. All ten hardware journals and eleven process registries were
empty. Cleanup maximum was 64.33 ms; idle descriptors/threads stayed at 10/2;
post-warmup RSS range was 240 KiB. These are measured test results, not production
latency promises or RF capacity measurements.

The [10-minute runtime soak](phase1c-ci-runtime-soak-2026-11-09.md) passed
**22,856 cycles across nine lifecycle/failure patterns**, with zero unexpected
errors. Its 699,601 cached status calls had p99 latency 0.593 ms. Each independent,
paced synthetic raw control/RX/TX path sustained 195.318 calls/second, 1.00114 times
its baseline rate. RSS peak grew 4.941 MiB; threads stayed at 21; descriptors peaked
at six and returned to five. Ownership, actions and queues were empty afterward.
These are in-process runtime/control measurements, not Global network QPS or RF
streaming capacity. Thresholds were declared before the run.

The measured server wheel was `523b28e108dd5e699313e4a5edac9ef22a453ec861c0a45edd043aeadaa6cb1d`.
The final dev wheel differs in exactly one packaged Python file, `grpc_server.py`,
for the later stop-during-restart latch fix. That service wrapper is not executed
by the soak. The final core/controller/runtime bytes are unchanged; the final
wrapper is verified separately by its full server suite, real-signal tests and
repeated dev routes/stop/restart checks. The evidence retains both identities.
An earlier preliminary soak is also retained separately and is not substituted
for this nine-pattern run.

## Dev Global route and lifetime results

The canary used the original testlab identity with a separate environment and
configuration while the original testlab was stopped. Five existing SimSDRs
were retained; only fake USRP IDs 401/402 were added to the disposable inventory.
Global authentication, reservations, schema approval and transport authorization
remained active. The deployed Global container image was unchanged.

All three published clients, **2.12.0, 2.13.9 and 2.13.12**, passed the final
`enabled` route checks sequentially. Offline checks also bind all six methods
on both USRP/Phaser v2 manifests, compile v1/v2 sources and exercise the existing
cached-schema refresh/reimport boundary. This is a tested three-version matrix,
not a claim about every released client or a deployed v1/Phaser application path.

| Dev check | Result |
|---|---|
| Disabled integration plus missing optional adapter | Discovery disabled; typed start refusal; synthetic UHD RX before and after |
| Generic discovery/start/action/status/stop | Passed over relay and actual fixed direct QUIC |
| Retry after lost direct reply | Injected after successful owner execution; decoded direct and fallback operation IDs match |
| Duplicate action | Same action ID and exact echoed result, call count one |
| Concurrent independent raw radio | Synthetic UHD RX continues while another device has an application owner |
| Isolation and payload rules | Wrong-session operation and reserved payload tags receive expected typed errors |
| Native access during application ownership | Native error refusal while application remains `RUNNING`; native RX works after release |
| Partial startup and required-worker exit | Typed error, verified release and subsequent native RX; worker reaches `RUNNING` and no health action runs |
| Actual calendar cancellation | Active owner revoked; typed reservation refusal |
| Actual calendar expiry | Unattended hardware/process release observed before client activity; expired session refused; next holder receives |
| Active-application systemd stop | 0.801 seconds; success, MainPID zero, absent unit cgroup, empty hardware and process journals |
| Restart with a new cgroup identity | Same clean config restarts; full generic/native route checks pass again |

For unattended expiry, at 17:34:02.935 Pacific the observer found zero hardware
owners, matching boot/root/scope identities and zero populated processes, with
no unknown cgroups. The reservation ended at 17:34:00; the first subsequent
client RPC was at 17:34:11.007. One empty scope record remained as bounded
bookkeeping. A valid next-session integration request retired it; both journals
were empty before final service shutdown. Do not equate this design with
immediate autonomous removal of every metadata row.

Direct access was blocked by the original dev firewall before the canary.
A temporary UDP 12321 rule restricted to the operator's source address made
both the old-service baseline and final canary direct checks pass. The rule was
removed during restoration. The final restored-policy check therefore verifies
relay receive; unrestricted direct reachability is not claimed.

## Restoration and retained failure evidence

The final canary was stopped with empty journals and no unit cgroup. Both
canary configurations are disabled. The original service is active again with
server `1.7.22`, core `0.1.6`, federation `0.22.0`, its original five simulated
radios and no integration configuration. Original relay/direct receive passed
before removing the temporary network rule; relay receive passed afterward.

The exact dev USRP v2 schema approval was withdrawn, the temporary firewall
rule removed, and the dedicated test account deleted after verifying zero
visible active reservations. After the last clone rollback, the temporary
account initially received `principal_unknown` because its enrollment existed
only in the cloned database. It was recreated through normal registration and
enrollment against the original service; restored receive checks then passed.
No original database was copied back or manually patched. Deletion used the admin tool's explicit force
option required for an activated disposable account. Saved test credentials
were deleted, all canary units are absent, and the dev lease was released with
an empty queue. No production check or mutation was needed for restoration.

The **failed pre-fix SIGTERM run** is retained separately: one synthetic hardware
record and one scope record remain fenced in its disabled disposable config,
although systemd removed that test unit and its cgroup. No record was deleted or
relabelled as recovered. Because the original cgroup identity disappeared,
automatic recovery correctly cannot certify that old state. Final qualification
used fresh isolated synthetic state; this is not a recovery procedure for real
hardware. The restored original inventory contains only SimSDRs and does not
include that fake USRP owner. Never apply this test-isolation procedure to
bypass a physical-radio ownership fence.

## Remaining activation gates

The [earlier UCLA receive-only evidence](phase1c-2026-11-09.md) remains valid for
its recorded source. It does **not** qualify these worker-monitoring and server
lifetime changes. Exact release-build physical-radio handoff, failure, expiry,
crash, descendant cleanup, service stop and next-holder checks remain required,
along with matching operator qualification and exact production schema approval.

Production aggregate memory/tasks/CPU budgets, native UHD responsiveness under
those budgets, extended RF load/soak, supported hardware/profile coverage and
production rollback remain separate gates. Changed/missing cgroup identity
still requires physical/operator reconciliation; no force-release was added.
TX RF behavior, OAI, Phaser application ownership, host applications and arbitrary
third-party executables are not qualified by this dev run. Production integrations
remain disabled.
