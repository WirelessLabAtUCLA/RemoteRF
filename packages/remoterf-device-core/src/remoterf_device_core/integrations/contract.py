"""Experimental adapter contract; no reservation or hardware ownership policy.

Only a future authenticated runtime may supply Context and invoke hooks after
acquiring a device lease. Hooks are synchronous: the runtime, not the adapter,
will provide bounded asynchronous execution, operation IDs and cancellation.
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
    work directory must be provisioned/owned by the runtime. Device selectors
    and resource supervision will be added with the hardware lease phase.
    """

    device_id: str
    device_type: str
    generation: int
    work_dir: Path
    cancelled: Event


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
