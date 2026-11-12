"""Contract boundaries before integration code can enter a device session."""
from pathlib import Path
import subprocess
import sys
from threading import Event
from unittest.mock import Mock

import pytest

from remoterf_device_core.integrations import Context, IntegrationError, Manifest, Registry, plain_data
from remoterf_device_core.integrations.payload import RESERVED_KEYS


SAMPLE = Path(__file__).resolve().parents[3] / "examples/integrations/counter/src"


def manifest(**changes):
    return Manifest.from_dict({
        "id": "org.example.counter", "version": "1.0.0", "contract_version": "0.1",
        "factory": "not_installed:create_adapter", "device_types": ["usrp"],
        "profiles": ["counter"], "actions": ["increment"], **changes,
    })


def context(tmp_path, device_id="7", device_type="usrp"):
    return Context(device_id, device_type, 1, tmp_path, Event())


def assert_code(code, fn):
    with pytest.raises(IntegrationError) as caught:
        fn()
    assert caught.value.code == code


def test_discovery_and_disabled_create_do_not_import_adapter(monkeypatch, tmp_path):
    load = Mock(side_effect=AssertionError("adapter import must be lazy"))
    monkeypatch.setattr("remoterf_device_core.integrations.registry.importlib.import_module", load)
    registry = Registry([manifest()])
    row, = registry.discover(device_id="7", device_type="usrp")
    assert row["compatible"] and not row["enabled"]
    assert "factory" not in row
    assert_code("integration_disabled", lambda: registry.create(row["id"], context(tmp_path)))
    load.assert_not_called()


@pytest.mark.parametrize("definition,device_type,code", [
    (manifest(contract_version="2.0"), "usrp", "contract_mismatch"),
    (manifest(), "rtl_sdr", "unsupported_device"),
])
def test_compatibility_refusal_precedes_import(definition, device_type, code, monkeypatch, tmp_path):
    load = Mock(side_effect=AssertionError("incompatible adapter imported"))
    monkeypatch.setattr("remoterf_device_core.integrations.registry.importlib.import_module", load)
    registry = Registry([definition], enabled={"7": [definition.integration_id]})
    assert_code(code, lambda: registry.create(definition.integration_id, context(tmp_path, device_type=device_type)))
    load.assert_not_called()


def test_enablement_is_device_specific_and_copied(tmp_path):
    ids = ["org.example.counter"]
    enabled = {"7": ids}
    registry = Registry([manifest()], enabled=enabled)
    ids.clear()
    enabled["8"] = ["org.example.counter"]
    assert registry.discover(device_id="7", device_type="usrp")[0]["enabled"]
    assert_code("integration_disabled", lambda: registry.create("org.example.counter", context(tmp_path, "8")))
    assert_code("invalid_configuration", lambda: Registry([manifest()], enabled={"7": "org.example.counter"}))


def test_duplicate_and_unknown_registration_fail_closed():
    assert_code("duplicate_integration", lambda: Registry([manifest(), manifest()]))
    assert_code("unknown_integration", lambda: Registry([], enabled={"7": ["org.missing.adapter"]}))
    assert_code("registry_limit", lambda: Registry(manifest(id=f"org.example.adapter_{i}") for i in range(65)))


@pytest.mark.parametrize("enabled", [[], {"7": None}, {1: []}])
def test_invalid_operator_enablement_is_typed(enabled):
    assert_code("invalid_configuration", lambda: Registry([manifest()], enabled=enabled))


def test_enablement_is_bounded_even_for_an_unending_iterator():
    from itertools import repeat
    assert_code("registry_limit", lambda: Registry([manifest()], enabled={"7": repeat("org.example.counter")}))


def test_manifest_is_immutable_and_description_is_a_copy():
    definition = manifest()
    registry = Registry([definition])
    description = registry.describe(definition.integration_id)
    description["profiles"].append("injected")
    assert registry.describe(definition.integration_id)["profiles"] == ["counter"]


@pytest.mark.parametrize("changes", [
    {"id": "usrp"}, {"id": "../module"}, {"factory": "module:__dict__"},
    {"profiles": []}, {"actions": ["start", "start"]}, {"device_types": "usrp"},
    {"actions": [False]}, {"contract_version": 0.1}, {"unknown": 1},
])
def test_invalid_metadata(changes):
    assert_code("invalid_manifest", lambda: manifest(**changes))


def test_manifest_file_does_not_accept_duplicate_keys_or_unbounded_input(tmp_path):
    path = tmp_path / "integration.json"
    path.write_text('{"id":"a.b", "id":"c.d"}')
    assert_code("invalid_manifest", lambda: Manifest.from_file(path))
    path.write_bytes(b" " * (16 * 1024 + 1))
    assert_code("payload_limit", lambda: Manifest.from_file(path))


@pytest.mark.parametrize("failure", [ImportError("secret=/private/key"), SystemExit("private details")])
def test_optional_load_failure_is_typed_and_does_not_change_registry(failure, monkeypatch, tmp_path):
    registry = Registry([manifest()], enabled={"7": ["org.example.counter"]})
    monkeypatch.setattr("remoterf_device_core.integrations.registry.importlib.import_module", Mock(side_effect=failure))
    with pytest.raises(IntegrationError) as caught:
        registry.create("org.example.counter", context(tmp_path))
    assert caught.value.code == "adapter_load_failed"
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is failure
    assert len(registry.discover(device_id="7", device_type="usrp")) == 1


def test_cancelled_context_does_not_import(monkeypatch, tmp_path):
    ctx = context(tmp_path)
    ctx.cancelled.set()
    load = Mock(side_effect=AssertionError("cancelled context imported"))
    monkeypatch.setattr("remoterf_device_core.integrations.registry.importlib.import_module", load)
    registry = Registry([manifest()], enabled={"7": ["org.example.counter"]})
    assert_code("cancelled", lambda: registry.create("org.example.counter", ctx))
    load.assert_not_called()


def test_only_declared_actions_accept_bounded_plain_data():
    registry = Registry([manifest()], enabled={"7": ["org.example.counter"]})
    def validate(action, parameters):
        return registry.validate_call("org.example.counter", device_id="7", device_type="usrp",
                                      action=action, parameters=parameters)
    assert_code("unsupported_action", lambda: validate("__getattribute__", {}))
    parameters = {"nested": [1, 2]}
    normalized = validate("increment", parameters)
    parameters["nested"].append(3)
    assert normalized == {"nested": [1, 2]}


def test_only_declared_profiles_accept_bounded_configuration():
    registry = Registry([manifest()], enabled={"7": ["org.example.counter"]})
    def validate(profile, configuration):
        return registry.validate_start("org.example.counter", device_id="7", device_type="usrp",
                                       profile=profile, configuration=configuration)
    assert_code("unsupported_profile", lambda: validate("unknown", {}))
    assert_code("invalid_payload", lambda: validate("counter", {"__uhd_type__": "TimeSpec"}))
    assert validate("counter", {"initial": 5}) == {"initial": 5}


@pytest.mark.parametrize("key", sorted(RESERVED_KEYS))
def test_protocol_tags_are_refused_recursively(key):
    assert_code("invalid_payload", lambda: plain_data({"nested": [{key: "value"}]}))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1 << 64, b"blob", (1, 2), {1: "key"}, object(), "\ud800"])
def test_values_that_cannot_round_trip_safely_are_refused(value):
    assert_code("invalid_payload", lambda: plain_data(value))


def test_payload_limits_cover_depth_items_encoded_unicode_and_cycles():
    nested = {}
    for _ in range(18):
        nested = {"next": nested}
    for value in (nested, [0] * 4097, "x" * 65537, "\U0001f600" * 6000):
        assert_code("payload_limit", lambda: plain_data(value))
    cycle = []
    cycle.append(cycle)
    assert_code("payload_limit", lambda: plain_data(cycle))
    assert plain_data({"valid": [-(1 << 63), (1 << 63) - 1, True, None, 1.5]})["valid"][2] is True


def test_core_contract_import_requires_no_native_or_driver_modules():
    code = """
import sys
from remoterf_device_core.integrations import Registry
assert Registry([]).discover(device_id='7', device_type='usrp') == []
assert 'uhd' not in sys.modules
assert 'numpy' not in sys.modules
assert 'remoterf_device_core.idl.schema' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_separate_sample_adapter_uses_public_contract(monkeypatch, tmp_path):
    definition = Manifest.from_file(SAMPLE / "rrf_example_counter/integration.json")
    monkeypatch.syspath_prepend(str(SAMPLE))
    registry = Registry([definition], enabled={"sample": [definition.integration_id]})
    ctx = context(tmp_path, "sample", "sim_sdr")
    adapter = registry.create(definition.integration_id, ctx)
    assert adapter.status() == {"running": False, "value": 0}
    configuration = registry.validate_start(definition.integration_id, device_id="sample", device_type="sim_sdr",
                                            profile="counter", configuration={"initial": 10})
    configuration = plain_data(adapter.validate("counter", configuration))
    adapter.start("counter", configuration)
    parameters = registry.validate_call(definition.integration_id, device_id="sample", device_type="sim_sdr",
                                        action="increment", parameters={"delta": 3})
    assert plain_data(adapter.call("increment", parameters)) == {"value": 13}
    ctx.cancelled.set()
    with pytest.raises(RuntimeError):
        adapter.call("increment", {"delta": 1})
    adapter.stop()
    adapter.stop()
    assert not adapter.status()["running"]
    # A new session generation gets a new instance; no shared running state.
    fresh = registry.create(definition.integration_id, context(tmp_path, "sample", "sim_sdr"))
    assert fresh.status() == {"running": False, "value": 0}
