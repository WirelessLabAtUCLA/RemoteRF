"""Explicit operator manifests; discovery never imports adapter code.

Separate from DeviceSchema registration and stock schema hashes. No global
registry, environment scan, file execution, automatic enablement or RPC wiring.
"""
from __future__ import annotations

import importlib
from itertools import islice
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .contract import Adapter, Context, CONTRACT_VERSION
from .payload import IntegrationError, MAX_REQUEST_BYTES, object_data


_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_FACTORY = re.compile(r"[a-zA-Z]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z]\w*")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?")
_FIELDS = {"id", "version", "contract_version", "factory", "device_types", "profiles", "actions"}
MAX_MANIFEST_BYTES = 16 * 1024
MAX_ADAPTERS = 64


@dataclass(frozen=True)
class Manifest:
    """Canonical immutable metadata; construct with from_dict or from_file."""

    integration_id: str
    version: str
    contract_version: str
    factory: str
    device_types: tuple[str, ...]
    profiles: tuple[str, ...]
    actions: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict) -> Manifest:
        value = object_data(value, max_bytes=MAX_MANIFEST_BYTES)
        if set(value) != _FIELDS:
            raise IntegrationError("invalid_manifest", "integration manifest fields do not match the contract")
        for field, pattern, limit in (("id", _ID, 128), ("version", _VERSION, 128), ("factory", _FACTORY, 512)):
            if (type(value[field]) is not str or len(value[field]) > limit
                    or not pattern.fullmatch(value[field])):
                raise IntegrationError("invalid_manifest", f"invalid integration {field}")
        if (type(value["contract_version"]) is not str or len(value["contract_version"]) > 16
                or not re.fullmatch(r"\d+\.\d+", value["contract_version"])):
            raise IntegrationError("invalid_manifest", "invalid contract version")
        for field in ("device_types", "profiles", "actions"):
            names = value[field]
            if (type(names) is not list or len(names) > 64
                    or (not names and field != "actions")
                    or any(type(name) is not str or len(name) > 128 or not _NAME.fullmatch(name) for name in names)
                    or len(names) != len(set(names))):
                raise IntegrationError("invalid_manifest", f"invalid integration {field}")
        return cls(value["id"], value["version"], value["contract_version"], value["factory"],
                   tuple(value["device_types"]), tuple(value["profiles"]), tuple(value["actions"]))

    @classmethod
    def from_file(cls, path: str | Path) -> Manifest:
        # Read once with a hard limit. File paths are operator configuration,
        # not client input. Never evaluate code while reading metadata.
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise IntegrationError("payload_limit", "integration manifest is too large")

        def unique_fields(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise IntegrationError("invalid_manifest", "duplicate manifest field")
                result[key] = item
            return result

        try:
            return cls.from_dict(json.loads(raw, object_pairs_hook=unique_fields))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise IntegrationError("invalid_manifest", "invalid integration manifest JSON") from exc

    def describe(self) -> dict:
        # The module path is deployment-internal, not part of remote discovery.
        return {"id": self.integration_id, "version": self.version,
                "contract_version": self.contract_version, "device_types": list(self.device_types),
                "profiles": list(self.profiles), "actions": list(self.actions)}


class Registry:
    """Immutable configuration snapshot. Create a new one to reconfigure.

    enabled maps device IDs to integration IDs and must come from the operator,
    never from a reservation holder. Compatibility is checked before any import.
    """

    def __init__(self, manifests: Iterable[Manifest], *, enabled: Mapping[str, Iterable[str]] | None = None):
        definitions = {}
        for definition in manifests:
            if not isinstance(definition, Manifest):
                raise IntegrationError("invalid_manifest", "expected an integration manifest")
            # Revalidate even when a caller constructed the dataclass directly.
            definition = Manifest.from_dict({**definition.describe(), "factory": definition.factory})
            if definition.integration_id in definitions:
                raise IntegrationError("duplicate_integration", "duplicate integration ID")
            if len(definitions) >= MAX_ADAPTERS:
                raise IntegrationError("registry_limit", "too many installed integrations")
            definitions[definition.integration_id] = definition
        self._definitions = MappingProxyType(definitions)
        enabled_copy = {}
        if enabled is None:
            enabled = {}
        if not isinstance(enabled, Mapping):
            raise IntegrationError("invalid_configuration", "enablement must be a device mapping")
        for device, ids in enabled.items():
            if type(device) is not str or not device or isinstance(ids, (str, bytes)):
                raise IntegrationError("invalid_configuration", "invalid integration enablement")
            try:
                ids = tuple(islice(ids, MAX_ADAPTERS + 1))
            except TypeError as exc:
                raise IntegrationError("invalid_configuration", "enabled IDs must be iterable") from exc
            if len(ids) > MAX_ADAPTERS:
                raise IntegrationError("registry_limit", "too many enabled integrations for a device")
            if any(type(item) is not str or item not in definitions for item in ids):
                raise IntegrationError("unknown_integration", "enabled integration is not installed")
            enabled_copy[device] = frozenset(ids)
        self._enabled = MappingProxyType(enabled_copy)

    def describe(self, integration_id: str) -> dict:
        return self._definition(integration_id).describe()

    def discover(self, *, device_id: str, device_type: str) -> list[dict]:
        return [{**definition.describe(),
                 "enabled": definition.integration_id in self._enabled.get(device_id, ()),
                 "compatible": self._compatible(definition, device_type)}
                for definition in sorted(self._definitions.values(), key=lambda item: item.integration_id)]

    def _definition(self, integration_id: str) -> Manifest:
        if type(integration_id) is not str or integration_id not in self._definitions:
            raise IntegrationError("unknown_integration", "integration is not installed")
        return self._definitions[integration_id]

    @staticmethod
    def _compatible(definition: Manifest, device_type: str) -> bool:
        return definition.contract_version == CONTRACT_VERSION and device_type in definition.device_types

    def validate_call(self, integration_id: str, *, device_id: str, device_type: str,
                      action: str, parameters: dict) -> dict:
        definition = self._admit(integration_id, device_id, device_type)
        if type(action) is not str or action not in definition.actions:
            raise IntegrationError("unsupported_action", "integration action is not declared")
        return object_data(parameters, max_bytes=MAX_REQUEST_BYTES)

    def validate_start(self, integration_id: str, *, device_id: str, device_type: str,
                       profile: str, configuration: dict) -> dict:
        """Check metadata/payload only; adapter validation and leases come later."""
        definition = self._admit(integration_id, device_id, device_type)
        if type(profile) is not str or profile not in definition.profiles:
            raise IntegrationError("unsupported_profile", "integration profile is not declared")
        return object_data(configuration, max_bytes=MAX_REQUEST_BYTES)

    def _admit(self, integration_id: str, device_id: str, device_type: str) -> Manifest:
        definition = self._definition(integration_id)
        if integration_id not in self._enabled.get(device_id, ()):
            raise IntegrationError("integration_disabled", "integration is not enabled for this device")
        if definition.contract_version != CONTRACT_VERSION:
            raise IntegrationError("contract_mismatch", "integration contract version is unsupported")
        if device_type not in definition.device_types:
            raise IntegrationError("unsupported_device", "integration does not support this device type")
        return definition

    def create(self, integration_id: str, context: Context) -> Adapter:
        """Instantiate trusted adapter code. Does not start it or grant a lease.

        The caller must already authenticate/reserve the device and provide its
        own failure boundary for imports/factories. Python cannot interrupt a
        hung import. One instance belongs to one session generation; no cache.
        """
        definition = self._admit(integration_id, context.device_id, context.device_type)
        if context.cancelled.is_set():
            raise IntegrationError("cancelled", "integration context is cancelled")
        module, name = definition.factory.split(":")
        try:
            factory = getattr(importlib.import_module(module), name)
            if not callable(factory):
                raise TypeError("adapter factory is not callable")
            adapter = factory(context)
            if any(not callable(getattr(adapter, hook, None))
                   for hook in ("validate", "start", "status", "call", "stop")):
                raise TypeError("adapter is missing required lifecycle hooks")
        except (Exception, SystemExit) as exc:
            # Do not expose paths, environment or credentials from import errors.
            raise IntegrationError("adapter_load_failed", "integration adapter could not be loaded") from exc
        return adapter
