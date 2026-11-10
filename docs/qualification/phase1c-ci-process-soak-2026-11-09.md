# Phase 1c process supervision stress — November 9, 2026

The final installed core wheel passed the isolated CI-box kernel tests and
100-cycle process stress run. This is process/resource verification with no
radio, production service, integration activation or schema approval.

Three exact-source runs are retained:

- [Baseline evidence](phase1c-ci-process-soak-2026-11-09.json), before required-worker
  monitoring, passed 100 cycles in 100.91 seconds.
- [Interim evidence](phase1c-ci-process-soak-interim-2026-11-09.json), with optional
  required-worker monitoring but before monitor-failure hardening, passed
  100 cycles in 100.96 seconds.
- [Final evidence](phase1c-ci-process-soak-final-2026-11-09.json), including the
  shared monitor-failure latch and launch refusal, passed 100 cycles in
  101.09 seconds, from 17:14:19 to 17:16:00 America/Los_Angeles. Every recorded
  core module hash and the qualification-script hash matched the final source.
  The installed wheel SHA256 is
  `9d3b88258c7a6298c460f50468a6385bc59d64c72e763dcb21719d230e331d97`.

The final run used Python 3.12.16, core `0.1.7.dev0` installed from a rebuilt
wheel in a separate environment, systemd 259, and a fresh delegated transient
user service. Its outer containment was `MemoryMax=512M`, `TasksMax=192`,
`RuntimeMaxSec=300`. No existing service configuration was changed.

## Predeclared workload and limits

The [script](../../tools/qualification/process_soak.py) writes its thresholds
and source identity before launching its first child. Each run includes:

- 60 normal parent/setsid-descendant cycles with 128 KiB on each output stream.
- 10 target-exec failures, 10 pre-spawn cwd failures and 10 injected pre-ack
  readiness refusals. The latter is an explicitly injected launch-race condition.
- 10 abrupt supervisor exits leaving a real child and a non-radio lease, each
  reconciled through the actual offline operator command and cgroup identity.
- A 16-owner concurrent wave, rejection of a 65th scope and a fifth launch in
  one scope, and an independent sentinel that must survive all other cleanup.

Normal and restart workers in the final run opt into `required_while_running`.
The process scopes keep the framework's 128-PID/512-MiB limits; retained log
tails are reduced to 4 KiB per stream to exercise truncation and continued
draining. This stress harness tests supervisor cleanup directly. Autonomous
Runtime cancellation is covered separately by the kernel tests below.

| Final measurement | Predeclared pass limit | Observed |
|---|---|---|
| Lifecycle cycles | All 100 succeed | 100 passed |
| Scenario stop/recovery operations | Each ≤5.5 s, with a 5 s stop budget | 117; maximum 64.33 ms, median 10.49 ms |
| Idle descriptors | Baseline +2 at most | 10 throughout |
| Idle OS threads | No growth | 2 throughout |
| Post-warmup parent RSS range | ≤32 MiB | 240 KiB |
| Latter-half median RSS increase | ≤16 MiB | 220 KiB |
| Unit memory peak | ≤256 MiB | 149.27 MiB |
| Final parent descriptors / threads | Back to original bounds | 4 / 1 |
| Final owned cgroups / hardware leases / process records | Zero | Zero |

The sentinel is stopped and verified separately during final shutdown. Ten
hardware journals and eleven process registries were read afterward and all
were empty. Both final transient units reported `LoadState=not-found`,
`ActiveState=inactive`, `MainPID=0` and an empty control-group path.

## Actual kernel required-worker tests

The final wheel passed **4 tests in 0.84 seconds**, with no skips, in a separate delegated
service. Besides existing descendant, isolation, output and restart/operator
recovery cases, two cases exercise required-worker exits with codes zero and
seven. The worker leaves a setsid descendant holding its inherited pipes. No
adapter health action runs: the cached supervisor signal causes Runtime to
cancel, kill the owned cgroup, prove descendant release and release a fake
native-device lease. No PID copied from a journal is signalled.

Focused local regression coverage for the final monitor change also passed
**117 tests**, with two existing platform/optional checks skipped on macOS.
This includes finite optional jobs, cached probe independence from cleanup locks,
startup/action serialization, intentional stop and timeout races, controller
shutdown grace and the required UHD client/server socket loopback. New cases
inject selector failure, refuse later launches before process creation, prevent
target execution when failure races bootstrap admission, and preserve normal
monitor shutdown. Monitor failure never substitutes for process-release proof.

## Limits of this evidence

The short run verifies declared cleanup and resource bounds, not production
throughput. The final kernel/process interval was coordinated to avoid overlap
with the other qualification agent's CI runs; measured stop times remain
observations, not latency guarantees. The longer mixed runtime soak starts only
after these process tests have ended.

The earlier UCLA radio evidence remains tied to its earlier framework hash and
used an explicit health action to detect forced worker exit. The new monitoring
and shutdown behavior require exact-build radio requalification before release.
Global authorization, real calendar expiry, supported released-client versions,
production aggregate resource budgets, RF response under CPU/memory pressure,
and rollback to code that predates ownership journals remain separate gates.
Production integrations remain disabled.
