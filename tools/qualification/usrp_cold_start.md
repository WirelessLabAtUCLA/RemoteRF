# Normal-authorization fresh-process UHD check

`usrp_cold_start.py` tests one first native open in one new Python process and a
fresh private configuration. It does not patch `resolve_gid_for_token`,
`token_matches_device`, the session manager, or hardware ownership gates. It
creates a disposable local account through the ordinary enrollment/group APIs,
creates a reservation through `ReservationHandler.reserve_device`, and applies
it through `update_devices`. Native opens and receive admission use the normal
salt/hash token resolver. The separate external booking verifier is never used
as that resolver and has no four-second freshness cache.

The command uses the exact installed core/server/probe wheels and records their
source hashes. The state directory must not exist. It sets private XDG/config locations before
server imports; no production database, account, inventory or integration
configuration is copied or modified. No public listener or federation agent runs.
The private calendar updater calls the normal method every **one second**, matching
the server updater's cadence; exceptions fail qualification.

```sh
python -I /absolute/usrp_cold_start.py \
  --backend usrp --case valid --serial EXACT_SERIAL --device-id EXACT_GID \
  --state-dir /absolute/private/NEW_STATE \
  --evidence /absolute/private/NEW_STATE/evidence.json \
  --authority-command /absolute/operator/external-booking-check \
  --reservation-id EXACT_EXTERNAL_BOOKING \
  --guarded-scope /sys/fs/cgroup/EXACT_DELEGATED_ROOT/EXACT_OWNED_SCOPE
```

Keep `usrp_service_stop.py` and `usrp_handoff.py` alongside it. The coordinator
must hold the actual radio booking, run this payload in an owned delegated
scope, independently recheck the external booking while it runs, and stop the
owned scope if permission is lost or its bounded deadline expires. The payload
refuses hardware access outside the declared supervisor-owned `rrf-*` kernel
domain or without finite memory/process limits. This membership check does not
replace the independent live guardian. The payload
itself checks the booking before importing server modules and after cleanup.
The guardian must remain until independent cgroup/USB release proof is collected,
including on failure. The private local reservation does not grant permission to
use a physical radio; it is only the isolated server's normal authorization state.

Hardware execution performs finite receive only: 4,096 samples at 1 MS/s,
100 MHz, gain zero, channel zero. Hardware fault cases are deliberately refused
by this command. Each invocation must get a fresh process; repeatedly calling
`run()` in one interpreter does not qualify as a cold process.

“Cold” means no test-only native-library preloading and a fresh Python/driver
state. Ordinary driver imports and `prepare_for_holder` image preloading remain
enabled exactly as in normal startup (`REMOTERF_USRP_PRELOAD=1`). This does not
power-cycle hardware, reset FPGA images, flush operating-system caches or disable
the existing preload optimization. Evidence distinguishes process start, normal
imports, calendar authorization, first native open and first receive, and records
that initial driver preload bookkeeping was empty. The first open may wait for
normal reservation preparation; that is part of the measured path.

Each attempt rejects an unrelated token, records the exact build and authorization
function identity, and requires all sessions, closing/opening state, deferred
native hooks, admitted native calls and hardware ownership to be clear afterward. Any private booking
is cancelled during cleanup. An ownership timeout is a failure, never permission
to delete state or call the radio released. Root-level process/USB observation is
a separate coordinator obligation.

For CI, `--backend fake` replaces only the native driver. It retains real private
account creation, reservation policy, SQLite calendar updates, token resolution,
SessionManager and cleanup. It never touches a physical radio. The supported cases:

- `valid --fake-open-delay 5`: valid authorization survives a native open longer
  than the old harness's four-second cache window.
- `expire_during_open --fake-open-delay 2`: a real private-calendar expiry occurs
  during a delayed open, which is refused and cleaned before the next holder opens.
- `cancel_during_open --fake-open-delay 0.2`: normal reservation removal/update
  while opening refuses the stale session and allows a fresh holder after cleanup.

The minimum reservation interval is preserved: expiry cases use an interval
starting 600 seconds in the past and ending shortly ahead. No test clock, database
row, authority callback or authorization timeout is changed. Evidence records
sanitized phase/exception/trace information; passwords and raw tokens are omitted.

This qualifies local normal authorization and native lifecycle. It does not
qualify public server startup, deployed Global routing/calendar propagation,
generic integrations, real-radio cancellation/expiry, native crash/GIL-stall
containment, TX, or another hardware profile. No result enables integrations.
