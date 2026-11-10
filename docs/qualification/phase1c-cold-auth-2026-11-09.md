# Phase 1c cold-process checks with normal authorization — November 9, 2026

**Passed: 12 of 12 fresh-process real-radio checks on the exact build, using
unmodified local authorization and no test-only native warmup.** Existing deployed
UHD receive also passed before and afterward. CI passed nine cold-auth tests and
the required UHD socket regression. Production integrations remain disabled;
no production package, service, inventory or schema approval changed. Nothing was
pushed or published. Reservation 1022 was released after verified cleanup at
approximately 21:04 Pacific, and the account's reservation list was empty.

## Why this check was needed

The [earlier service supplement](phase1c-ucla-exact-build-2026-11-09.md) had one
failed follow-up native open/receive despite a valid external booking. That
harness replaced the server's token resolver with a four-second cached external
proof. Its original failure record lacks the phase/trace needed to distinguish
post-open rejection from receive-admission rejection, or identify whether a
refresh timeout, delayed import or GIL stall caused stale proof.

The production resolver has no corresponding four-second deadline. The new
[qualification harness](../../tools/qualification/usrp_cold_start.md) therefore
uses ordinary account enrollment, reservation policy, SQLite calendar updates,
token validation and the server’s standard SessionManager singleton from the
exact server wheel.
Neither `resolve_gid_for_token` nor `token_matches_device` is patched. A private
local reservation supplies normal server authorization; the separately held
Global booking authorizes physical use. The external verifier runs independently
and never substitutes for the server's native authorization checks.

These passes close the narrow concern that this build rejects valid local
authorization merely because a fresh-process native open exceeds four seconds
on the tested UCLA configuration. They do **not** retrospectively prove the
original failure's precise cause or establish every cold-start/load scenario.
The failed attempt remains preserved in the earlier report.

## Exact build and safe setup

USRP_00@ucla is device 40, serial `32C78EE`, profile `usrp2901`/B200, native UHD
`4.10.0.main-release`. The user approved November 9, 21:00–22:00 Pacific and early
release. The physical booking remained active throughout every hardware test.
All receives used channel 0, 100 MHz, 1 MS/s, gain zero and 4,096 finite samples.
No transmission or stored I/Q capture occurred.

| Artifact | SHA-256 |
|---|---|
| Core `0.1.7.dev0` | `9d3b88258c7a6298c460f50468a6385bc59d64c72e763dcb21719d230e331d97` |
| Server `1.7.23.dev0` | `f9b382782d457bb7197c22b1bc77f310075d103ea5a688f9f4923d1249397d4e` |
| Probe `0.1.0` | `e6894b585afcf6a769ec8f9112f60a509eb18521568820fa21907780b152c784` |

Framework digest remains
`8697958f27a1b1289cc45093258cb3eeed84bdb631cacc122a055f21a3a7d41a`.
All **209 packaged files** matched before and after hardware testing and on CI.
This work changes qualification tools/evidence only, not framework code.
The cold harness SHA is
`3ca929656759b75b5a2b0c4b5ab2fd80910c69fe558f29d68d01dd46bd986e18`.

Each attempt had a new Python process, private configuration, one-radio inventory,
private account and real local calendar reservation created through normal APIs.
It rejected an unrelated token before valid native acquisition. The ordinary
calendar updater ran every second. Private bookings were cancelled during cleanup.
No production database or account was copied into the test configuration.

“Cold” means fresh process/driver state with NumPy/UHD initially absent and no
extra harness warmup. The normal imports and image-preload optimization stayed
enabled, and the driver preload bookkeeping began empty in every attempt. We did
not power-cycle the radio, reload FPGA images deliberately, flush OS caches or
disable the existing driver's preload behavior. These are not physical cold-boot
or firmware-load results.

The independent guardian held the shared serial lock, admitted each process into
an identity-verified cgroup, checked the real external booking every second and
retained its root until independent cleanup verification. The payload verified
its actual bounded cgroup membership before server imports. The 240-second outer
containment deadline accommodates the stock image-preload allowance; it is not an
acceptable RPC latency target. A separate 120-second native-open ceiling matches
the local client's default OpenSession budget. Measured opens were all below five
seconds. Public Global/relay deadlines are not qualified by this local path.

## Hardware results

[Hardware evidence](phase1c-ucla-cold-auth-2026-11-09.json) retains every child
record, resource limits, source hashes, guardian source and independent cleanup.

| Test budget | Attempts | Native-open minimum / median / maximum | Run duration |
|---|---:|---|---:|
| CPU ceiling equivalent to two CPUs | 6 / 6 passed | 4.709 / 4.741 / 4.763 s | 36.544 s |
| CPU ceiling equivalent to one CPU | 6 / 6 passed | 4.857 / 4.872 / 4.933 s | 37.869 s |

Runs began at 21:01:18 and 21:02:25 Pacific. Both used the same parent limits of
1 GiB/192 tasks and child limits of 512 MiB/128 tasks. This compares CPU ceilings;
it is not a broad concurrent-load or production-capacity test. Every first native
open exceeded the earlier harness's four-second window while ordinary authorization
remained valid. Every attempt received 4,096 samples and exited normally.

All attempts recorded zero admitted native calls, live/closing/opening sessions,
native inventory owners, deferred native hooks, hardware owners and pending
cleanup. Their private calendars were empty. Each scope released normally without
forced termination; its identity-verified registry entry was retired and USB
ownership was checked before the next attempt. Independent post-run checks found
empty process registries, matching delegated-root identities, no owned scopes
and no USB owner before releasing the guardians.

## CI and regression results

[CI evidence](phase1c-ci-cold-auth-2026-11-09.json) uses the same installed wheels
on ci-box, with only the native driver replaced by an explicit fake. Enrollment,
reservation policy, real private-calendar updates, auth, SessionManager and cleanup
remain unchanged. No physical device was used on CI.

- Valid authorization survived a **5.005-second** delayed native open.
- Actual private-calendar expiry during a 2.008-second open rejected the stale
  session at the post-open check, cleaned it and allowed the next holder to receive.
- Ordinary reservation cancellation during a 0.208-second open did the same.
- Diagnostics recorded the ended token as invalid, advanced reservation epoch and
  a non-stopping runtime; the refusal is intentional and correctly cleans up.
- Nine focused tests passed without skips, including build/authority/guard checks.
- The required existing UHD localhost socket test passed without skips, using real
  gRPC/codecs and fake radio resources. This is separate from hardware receive.
- The combined changed/related harness group passed **36 tests** locally.

The delayed fake open uses `time.sleep`, so it does not simulate a GIL-holding
native call. CI's first socket-test collection encountered a missing dependency
path in the new isolated environment. Correcting that environment path resolved
collection; no package source changed. That setup failure and the intermediate
seven-test result were retained separately; the nine-test result is authoritative.
No dev service redeployment was necessary for this focused check.

## Existing UHD and final state

[Compatibility and release evidence](phase1c-ucla-cold-restoration-2026-11-09.json)
records deployed Global/UCLA receive before and after, using client `2.13.13.dev0`
in relay mode and the unchanged production schema. Both received 4,096 samples
and closed the streamer/root; native opens took 2.702 and 2.707 seconds. These are
old-server/new-client compatibility checks, separate from the exact new build's
local authorization tests above.

Production remained PID 359991, server `1.7.21`, core `0.1.6`, with the same
inventory digest and no `integrations.json`. Both test units exited successfully,
with MainPID zero and no remaining cgroup. The USB device was free before booking
cancellation. Reservation 1022 was then cancelled and the account's reservation
list was empty. Inactive private test state/evidence remains for audit.

## Remaining production gates

The exact-build fresh-process/normal-local-auth check is now covered for this
serial, profile, UHD version and tested limits. Still outstanding are the combined
real-radio generic API/receipt/public-startup path, actual Global calendar changes
on hardware, applicable descendant-failure scenarios, extended RF soak, deployment
resource budgets, recovery/rollback rehearsal, supported-version/profile coverage
and release/schema/receipt approvals. Native crash/GIL-stall containment remains
an explicit limitation. TX, OAI and host application execution are not qualified.
Production integrations stay disabled until a later explicitly authorized rollout.
