"""Experimental adapter contract; no reservation or hardware ownership policy.

Only the authenticated runtime supplies Context and invokes hooks after
acquiring a device lease. Hooks are synchronous: the runtime, not the adapter,
provides bounded asynchronous execution, operation IDs and cancellation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol


CONTRACT_VERSION = "0.1"


@dataclass(frozen=True)
class Context:
    """Session-scoped data supplied by the runtime, never by an RPC caller.

    No bearer/session token or native UHD object is passed to an adapter. The
    work directory is provisioned by the runtime before adapter construction.
    selector comes from operator inventory, never caller configuration.
    processes is an owner-bound supervision service; it grants no access to
    other owners, reservation credentials or the server's environment.
    Its launch(..., required_while_running=True) declares a long-lived worker
    whose exit must cancel the application. The default False permits finite
    jobs. Monitoring is cached and never calls an adapter hook from status RPCs.
    """

    device_id: str
    device_type: str
    generation: int
    work_dir: Path
    cancelled: Event
    selector: dict | None = None
    processes: object = None


class Adapter(Protocol):
    """Trusted operator-installed code. Construction must be side-effect free.

    validate returns normalized plain-data configuration without acquiring a
    resource. start returns after readiness or raises; the future runtime calls
    it off the RPC thread with cancellation set on stop/expiry. stop must cancel
    and release resources idempotently, including a partially failed start.
    A status or action result is a bounded plain-data object. No guarantee of
    process/crash isolation is implied by this Python interface.
    """

    def validate(self, profile: str, configuration: dict) -> dict: ...
    def start(self, profile: str, configuration: dict) -> None: ...
    def status(self) -> dict: ...
    def call(self, action: str, parameters: dict) -> dict: ...
    def stop(self) -> None: ...
