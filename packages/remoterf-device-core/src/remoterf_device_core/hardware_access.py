"""Process-local routing of native admission to physical ownership gates.

Default inventory gates have no journal and cannot start applications. Phase
1c must drain devices and supply qualified physical identities/alias mapping
before attaching durable gates. Server and Host use separate tables.
"""
from threading import RLock
from contextlib import contextmanager, ExitStack
from .integrations.ownership import Gate


class AccessTable:
    def __init__(self):
        self._lock = RLock()
        self._gates = {}

    def gate(self, gid):
        with self._lock:
            gid = int(gid)
            if gid not in self._gates:
                if len(self._gates) >= 4096:
                    raise RuntimeError("hardware admission table is full")
                self._gates[gid] = Gate(f"inventory:{gid}")
            return self._gates[gid]

    def native(self, gid):
        return self.gate(gid).native()

    def blocked(self, gid):
        return self.gate(gid).snapshot()["blocked"]

    @contextmanager
    def maintenance(self, gids):
        with ExitStack() as stack:
            gates = {id(gate): gate for gate in (self.gate(gid) for gid in sorted(gids))}
            for gate in gates.values():
                stack.enter_context(gate.maintenance())
            yield
