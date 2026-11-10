# Phase 1c final CI runtime soak — November 9, 2026

Result: **PASS** for the measured core/controller/runtime. A subsequent service-wrapper-only fix is validated separately below. Mixed load ran 600.001 seconds after a 30.001-second raw-only baseline.

This is isolated software scheduling evidence using real SessionManager, Controller, Runtime and durable ownership journals with four explicitly fake radios, authority, adapter qualification and process scopes. It is not physical UHD throughput, real-radio handoff, Global route or production deployment qualification. No deployment activation or service setting changed.

Python 3.12.16; installed core wheel SHA-256 `9d3b88258c7a6298c460f50468a6385bc59d64c72e763dcb21719d230e331d97`. Exact source hashes and package origins are included in the linked raw JSON below.

Thresholds were declared before measurement: mixed status p99 ≤50ms; each independent raw-method p99 ≤max(5ms,4×baseline); raw throughput ≥90% baseline; RSS growth ≤64MiB; threads ≤initial+4; fds ≤initial+8; owner/action records within 16/64. Percentiles use bounded uniform reservoirs of up to 8192 samples; counts and maximum latency are exact.

| Call | Baseline calls/s | Mixed calls/s | Baseline p99 ms | Mixed p50/p95/p99 ms | Throughput ratio |
|---|---:|---:|---:|---:|---:|
| get_time_now | 195.10 | 195.32 | 0.286 | 0.101/0.199/0.414 | 1.0011 |
| recv_num_samps | 195.10 | 195.32 | 0.239 | 0.110/0.166/0.409 | 1.0011 |
| send_waveform | 195.10 | 195.32 | 0.196 | 0.104/0.132/0.161 | 1.0011 |

Status polling: 699,601 calls (1166.00/s), p50/p95/p99 0.183/0.295/0.593ms, maximum 22.547ms.

| Resource | Initial | Observed peak | Final before runtime shutdown |
|---|---:|---:|---:|
| rss_bytes | 76.273 MiB | 81.215 MiB | 81.160 MiB |
| threads | 21 | 21 | 15 |
| fds | 5 | 6 | 5 |
| bindings | 0 | 2 | 0 |
| operations | 0 | 2 | 0 |
| actions | 0 | 2 | 0 |
| work_queue | 0 | 2 | 0 |
| cleanup_queue | 0 | 2 | 0 |

Completed lifecycle cycles: 22,856. 9 warmup cycles are excluded. Unexpected errors: 0. All four fake radios passed post-soak native control/RX/TX checks; ownership journals, scopes and retained operation/action records were empty before final shutdown.

| Lifecycle scenario | Count |
|---|---:|
| normal | 2,540 |
| cancel_start | 2,540 |
| hold_action | 2,540 |
| refused | 2,540 |
| action_error | 2,540 |
| hold_stop | 2,539 |
| expiry | 2,539 |
| worker_exit | 2,539 |
| monitor_failure | 2,539 |

Expected typed refusals/races (including warmup): `{"closed_session_poll_race:RemoteRFSessionExpiredError": 1, "device_owned": 15244, "operation_busy": 5081, "unsupported_action": 2541}`.

Separate preflights proved injected journal ENOSPC retains the native fence, bounded operation/action admission rejects before side effects, duplicate retries still work at capacity, and retirement restores admission. ENOSPC was injected; no disk was filled. The separate [process stress run](phase1c-ci-process-soak-2026-11-09.md) covers actual Linux cgroups and descendants.

Regression on the timed run's wheels: core 248 passed/4 explicit live-cgroup skips (+9 subtests); full nonhardware server 1013 passed/5 historical legacy opt-in skips (+97 subtests); host 176 passed (+39 subtests); probe+harness 30 passed. The required localhost UHD gRPC regression separately passed 1/1 with zero skips. Both fresh no-native installed-adapter wheel smokes passed.

The four core skips require an explicitly delegated isolated cgroup root; the separate delegated process qualification ran those 4 kernel tests. The five server skips are legacy/test_legacy_pipeline.py:75(1), :86(3), :91(1), requiring REMOTERF_LEGACY_CHECK=1.

Earlier interrupted and completed preliminary runs are retained separately. They do not substitute for this final 600-second measurement of the runtime build identified here.

Server wheel SHA-256: `523b28e108dd5e699313e4a5edac9ef22a453ec861c0a45edd043aeadaa6cb1d`. Both core and server were imported from installed wheel site-packages; host/federation used the isolated source snapshot.

[Raw measurements, source/build identities and regression counts](phase1c-ci-runtime-soak-2026-11-09.json). Full logs are retained in `/tmp/remoterf-phase1c-rollout-results` locally and matching `/tmp/remoterf-phase1c-rollout-*.log` files on CI-box.

## Final service-wrapper follow-up

The timed run loaded server wheel `523b28e108dd5e699313e4a5edac9ef22a453ec861c0a45edd043aeadaa6cb1d`. The final server wheel is `f9b382782d457bb7197c22b1bc77f310075d103ea5a688f9f4923d1249397d4e`. A byte comparison found only `remoteRF_server/server/grpc_server.py` changed among packaged Python files. Core, controller and session implementation under measurement remain unchanged.

The final wrapper latches SIGTERM during cleanup so an operator restart cannot re-execute after termination. The soak does not execute the service entrypoints or signal wrapper; its original source identities are preserved. The final wheel separately passed the complete nonhardware server suite (1017 passed, 5 historical skips) and the focused real-signal/required-localhost checks (9 passed, zero skips). The raw JSON records both identities and this coverage boundary.

The follow-up logs are retained locally beside the other results and on CI-box at `/tmp/remoterf-phase1c-service-followup/full-server.log` and `/tmp/remoterf-phase1c-service-followup/signals-loopback.log`.
