"""Generic application control for an authenticated local device session.

No method in this module is a reservation/authorization bypass. Dynamic v2
validates the live session and root first. Configuration is operator-owned,
operator enablement is the activation gate, and recovery is local-only.
"""
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import RLock
import time

from remoterf_device_core.integrations import CONTRACT_VERSION, IntegrationError, object_data
from remoterf_device_core.integrations.payload import MAX_REQUEST_BYTES
from remoterf_device_core.integrations.artifacts import ARTIFACTS, MAX_FETCH_BYTES
from remoterf_device_core.integrations.deployment import Configuration, scope_id, read_object
from remoterf_device_core.integrations.ownership import Journal
from remoterf_device_core.integrations.runtime import Runtime
from remoterf_device_core.native_lifetime import submit_cleanup


class DeferredScope:
    """No filesystem/process work on RPC; prepare runs after durable ownership."""
    def __init__(self, supervisor, owner_id, work_dir, limits=None, stop_timeout=5.0):
        self.supervisor, self.owner_id, self.work_dir = supervisor, owner_id, work_dir
        self.limits = limits or {}
        self.stop_timeout = stop_timeout
        self.scope = None
        self.attempted = False

    def prepare(self):
        self.attempted = True
        self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        (self.work_dir / ARTIFACTS).mkdir(mode=0o700)
        self.scope = self.supervisor.create(self.owner_id, **self.limits)

    def resources_released(self):
        if not self.attempted:
            return True
        return self.scope is not None and self.scope.resources_released()

    def resource_failure(self):
        # A cache-only trusted watcher hook; preparation and cleanup may be
        # blocked on IO, so this must never acquire their locks or inspect files.
        return self.scope.resource_failure() if self.scope is not None else None

    def close(self):
        if self.scope is not None:
            self.scope.close()
        # Runtime discarded artifacts/ before lease release. Never recursively
        # delete other adapter-written paths: leave them for operator review.
        if self.work_dir.exists():
            try:
                self.work_dir.rmdir()
            except OSError:
                pass

    def stop_if_prepared(self):
        if self.scope is not None and not self.scope.stop(timeout=self.stop_timeout):
            raise IntegrationError('recovery_required', 'supervised processes did not finish cleanup')

    def __getattr__(self, name):
        if name not in {'launch', 'snapshot', 'stop', 'log_tail'} or self.scope is None:
            raise IntegrationError('resources_unavailable', 'owned process supervision is not ready')
        return getattr(self.scope, name)


def _fields(request, required=(), optional=()):
    request = object_data(request, max_bytes=MAX_REQUEST_BYTES)
    if not set(required) <= set(request) or set(request) - set(required) - set(optional):
        raise IntegrationError('invalid_request', 'integration request fields do not match the method')
    return request


def _text(request, key, limit=128):
    value = request.get(key)
    if type(value) is not str or not 1 <= len(value) <= limit:
        raise IntegrationError('invalid_request', f'{key} must be a bounded nonempty string')
    return value


class Controller:
    def __init__(self, configuration=None, *, state_dir=None, journal=None, supervisor=None,
                 unavailable=None, installed_mapping=None):
        self.config = configuration or Configuration()
        self.registry = self.config.registry
        self.state_dir = Path(state_dir) if state_dir else None
        self.journal, self.supervisor = journal, supervisor
        self.installed_mapping = dict(installed_mapping or {})
        self.unavailable = unavailable
        self._lock = RLock()
        self._runtime = None
        self._bindings = {}  # (session, original generation) -> (binding, scope)
        self._operations = {}  # opaque operation -> binding
        self._starts = {}  # (session, original generation, request key) -> binding
        self._pending_closes = {}  # binding key -> bounded deferred cleanup job
        self._stopping = False

    def capabilities(self):
        reason = (self.unavailable or (None if self.config.enabled else 'integration_disabled')
                  or (None if self.supervisor and self.journal else 'supervision_unavailable')
                  or ('overloaded' if self._stopping else None))
        return {'contract_version': CONTRACT_VERSION, 'enabled': reason is None, 'reason': reason}

    def validate_inventory(self, records):
        mapping = self.config.physical_mapping(records, unresolved=self.journal.records() if self.journal else ())
        if mapping != self.installed_mapping:
            raise IntegrationError('inventory_changed', 'physical alias mapping changed; drain and restart before reloading')
        return mapping

    def _reap(self):
        with self._lock:
            if self._runtime is None:
                return
            for key, (binding, scope) in list(self._bindings.items()):
                job = self._pending_closes.get(key)
                if job is None:
                    if binding.authority() or not self._runtime.drained(binding):
                        continue
                    if any(not op['released'] for op in self._runtime.operations(binding)):
                        continue
                    try:
                        job = submit_cleanup(lambda: True, scope.close)
                    except Exception:
                        # No cleanup was admitted. Retain ownership/bookkeeping
                        # for a later bounded retry; discovery remains usable.
                        continue
                    self._pending_closes[key] = job
                if not job.done.is_set() or job.error is not None:
                    # Failed cleanup stays inspectable. Never repeat destructive
                    # resource cleanup merely because a caller polls status.
                    continue
                self._runtime.retire(binding)
                self._pending_closes.pop(key, None)
                self._bindings.pop(key, None)
                self._operations = {k: v for k, v in self._operations.items() if v is not binding}
                self._starts = {k: v for k, v in self._starts.items() if v is not binding}

    def dispatch(self, manager, session, method, request):
        # Only bookkeeping under this lock: prepare/import/native/adapter work
        # goes through Runtime's bounded pools. Status never reads process logs.
        with self._lock:
            self._reap()
            manager.get(session.session_id)
            with session.lifecycle_lock:
                root = session.handles.get(session.root_handle)
                if session.closed or root is None:
                    raise IntegrationError('expired', 'the owning session ended')
                generation = root.generation
            if method == 'integration_list':
                _fields(request, optional=('cursor', 'limit'))
                limit = request.get('limit', 8)
                cursor = request.get('cursor', '')
                if type(limit) is not int or not 1 <= limit <= 8 or type(cursor) is not str or len(cursor) > 128:
                    raise IntegrationError('invalid_request', 'invalid discovery page')
                rows = self.registry.discover(device_id=str(session.gid), device_type='usrp')
                result = {**self.capabilities(), 'generation': generation, 'integrations': [], 'next_cursor': None}
                for row in rows:
                    row['configured'] = row['enabled']
                    row['enabled'] = (row['configured'] and row['compatible'] and result['enabled']
                                      and session.gid in self.config.devices)
                remaining = [row for row in rows if row['id'] > cursor]
                for row in remaining[:limit]:
                    candidate = {**result, 'integrations': result['integrations'] + [row]}
                    try:
                        object_data(candidate, max_bytes=48 * 1024)
                    except IntegrationError:
                        break
                    result = candidate
                if len(remaining) > len(result['integrations']) and result['integrations']:
                    result['next_cursor'] = result['integrations'][-1]['id']
                return result
            if method == 'integration_describe':
                _fields(request, ('integration_id',))
                return {'integration': self.registry.describe(_text(request, 'integration_id')),
                        'availability': self.capabilities()}
            if method == 'integration_start':
                _fields(request, ('integration_id', 'profile', 'configuration', 'request_key', 'generation'))
                integration_id, profile = _text(request, 'integration_id'), _text(request, 'profile')
                request_key = _text(request, 'request_key')
                requested_generation = request['generation']
                if type(requested_generation) is not int or requested_generation < 1:
                    raise IntegrationError('invalid_request', 'generation must be a positive integer')
                # The deployment gate precedes native release, import and any
                # process/workdir creation, even for broken installed adapters.
                self.config.require_enabled(session.gid)
                if self._stopping:
                    raise IntegrationError('overloaded', 'application starts are draining')
                if self.unavailable or self.supervisor is None or self.journal is None:
                    raise IntegrationError('supervision_unavailable', 'application process supervision is unavailable')
                self.registry.validate_start(integration_id, device_id=str(session.gid), device_type='usrp',
                                             profile=profile, configuration=request['configuration'])
                retry_key = (session.session_id, requested_generation, request_key)
                binding = self._starts.get(retry_key)
                binding_key = retry_key
                created_binding = False
                if binding is None:
                    if requested_generation != generation:
                        raise IntegrationError('stale_owner', 'native generation changed; refresh discovery')
                    existing = self._bindings.get(binding_key)
                    if existing:
                        binding = existing[0]
                    else:
                        if len(self._bindings) >= 16:
                            raise IntegrationError('overloaded', 'application owner capacity is full')
                        gate = manager.integration_gate(session.gid)
                        owner = scope_id(gate.physical_id, session.session_id, generation, request_key)
                        directory = self.state_dir / 'work' / owner
                        scope = DeferredScope(self.supervisor, owner, directory,
                                              self.config.limits.get(integration_id),
                                              self.config.timeouts.get(integration_id, {}).get('stop_timeout_s', 5.0))
                        binding = manager.integration_binding(session.session_id, work_dir=directory,
                                                              resources_released=scope.resources_released)
                        binding = replace(binding, selector={'serial': self.config.devices[session.gid]['serial']},
                                          processes=scope, prepare=scope.prepare, resource_owner=owner,
                                          finalize=scope.stop_if_prepared, resource_failure=scope.resource_failure)
                        self._bindings[binding_key] = (binding, scope)
                        created_binding = True
                if self._runtime is None:
                    self._runtime = Runtime(self.registry, timeouts=self.config.timeouts,
                                            user_code=self.config.user_code)
                try:
                    result = self._runtime.start(binding, integration_id, profile, request['configuration'], request_key=request_key)
                except Exception:
                    # Rejected admission never consumes retained-operation
                    # capacity. Keep records if a fence was actually acquired.
                    owner = binding.gate.snapshot()['owner']
                    if created_binding and not scope.attempted and (owner is None or owner.get('resources') != binding.resource_owner):
                        self._bindings.pop(binding_key, None)
                    raise
                self._starts[retry_key] = binding
                self._operations[result['operation_id']] = binding
                return result
            if method not in {'integration_status', 'integration_stop', 'integration_call', 'integration_fetch'}:
                raise IntegrationError('unsupported_action', 'unknown integration method')
            if method == 'integration_call':
                _fields(request, ('operation_id', 'action', 'parameters', 'request_key'))
            elif method == 'integration_fetch':
                _fields(request, ('operation_id', 'artifact', 'offset'), ('limit',))
            else:
                _fields(request, ('operation_id',), ('action_id',) if method == 'integration_status' else ())
            operation_id = _text(request, 'operation_id')
            binding = self._operations.get(operation_id)
            if binding is None or binding.session_id != session.session_id:
                raise IntegrationError('unknown_operation', 'operation does not belong to this session')
            if method == 'integration_stop':
                return self._runtime.stop(binding, operation_id)
            if method == 'integration_call':
                return self._runtime.call(binding, operation_id, _text(request, 'action'), request['parameters'],
                                          request_key=_text(request, 'request_key'))
            if method == 'integration_status':
                if 'action_id' in request:
                    return self._runtime.action_status(binding, operation_id, _text(request, 'action_id'))
                result = self._runtime.status(binding, operation_id)
                # A successful handoff refreshes native metadata through a plain
                # object on this explicit path; no native probing occurs here.
                result['native_generation'] = generation
                if result['released']:
                    try:
                        result['native_capabilities'] = object_data(session.capabilities, max_bytes=12 * 1024)
                    except IntegrationError:
                        result['native_capabilities_truncated'] = True
                return result
            # File IO runs after this lock is released.
            fetch = partial(self._runtime.fetch, binding, operation_id, request['artifact'], request['offset'],
                            request.get('limit', MAX_FETCH_BYTES))
        return fetch()

    def session_closed(self, session_id):
        with self._lock:
            if self._runtime is not None:
                for operation_id, binding in list(self._operations.items()):
                    if binding.session_id == session_id:
                        self._runtime.stop(binding, operation_id)

    def shutdown(self):
        with self._lock:
            self._stopping = True
            if self._runtime is not None:
                self._runtime.shutdown()

    def close(self):
        # Service/operator shutdown only. RPC discovery never waits for these
        # jobs, and an uncertain or failed cleanup retains its resource owner.
        with self._lock:
            self._stopping = True
        deadline = time.monotonic() + 0.5
        while True:
            self._reap()
            with self._lock:
                if not self._bindings and not self._pending_closes:
                    break
                pending = tuple(self._pending_closes.values())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # The same shutdown-only grace covers ordinary runtime stop work
            # followed by scope cleanup. RPC discovery still never waits here.
            # A hung hook cannot extend this total deadline or release a fence.
            incomplete = next((job for job in pending if not job.done.is_set()), None)
            if incomplete is None:
                time.sleep(min(0.01, remaining))
            else:
                incomplete.done.wait(min(0.01, remaining))
        with self._lock:
            if self._bindings or self._pending_closes:
                raise IntegrationError('recovery_required', 'application resource cleanup has not completed')
            if self._runtime is not None:
                self._runtime.close()
        if self.journal is not None and self.journal.records():
            raise IntegrationError('recovery_required', 'unresolved hardware ownership prevents shutdown completion')
        if self.supervisor is not None:
            self.supervisor.close()
        if self.journal is not None:
            self.journal.close()


def initialize(manager, access, records, root):
    """Run before startup inventory opens/preloads hardware; no auto recovery."""
    root = Path(root)
    manager.integrations.close()
    config_path, state = root / 'integrations.json', root / 'db' / 'integrations'
    if not config_path.exists() and not state.exists():
        manager.integrations = Controller()
        return manager.integrations
    config_error = None
    try:
        configuration = Configuration.load(config_path)
    except (OSError, IntegrationError):
        configuration = Configuration()
        config_error = 'invalid_configuration'
    journal = Journal(state / 'ownership')
    supervisor = None
    try:
        mapping = configuration.physical_mapping(records, unresolved=journal.records())
        access.configure(mapping, journal)
        process_file = state / 'processes' / 'scopes.json'
        persisted_processes = read_object(process_file).get('scopes') if process_file.exists() else {}
        if type(persisted_processes) is not dict:
            raise IntegrationError('recovery_required', 'invalid process registry prevents native acquisition')
        if configuration.cgroup_root is not None:
            from remoterf_device_core.integrations.supervision import CgroupSupervisor
            try:
                supervisor = CgroupSupervisor(configuration.cgroup_root, state / 'processes')
            except (OSError, IntegrationError) as exc:
                if persisted_processes or getattr(exc, 'code', '') in {'recovery_required', 'journal_busy'}:
                    raise IntegrationError('recovery_required', 'owned process supervision requires reconciliation') from None
                config_error = config_error or 'supervision_unavailable'
        elif persisted_processes:
            # Removed supervisor configuration cannot erase orphan identity.
            raise IntegrationError('recovery_required', 'process supervision must be reconciled before startup')
        if supervisor is not None:
            linked = {row.get('resources') for row in journal.records().values()}
            for owner in supervisor.describe()['owners']:
                if owner not in linked:
                    orphan = supervisor.reopen(owner)
                    if not orphan.resources_released():
                        raise IntegrationError('recovery_required', 'unlinked application processes prevent native acquisition')
                    # A crash after hardware release can leave a verified-empty
                    # scope. It is safe to finish removing that resource record.
                    orphan.close()
        manager.integrations = Controller(configuration, state_dir=state, journal=journal, supervisor=supervisor,
                                          unavailable=config_error, installed_mapping=mapping)
        return manager.integrations
    except BaseException:
        try:
            if supervisor is not None:
                supervisor.disconnect()
        finally:
            journal.close()
        raise
