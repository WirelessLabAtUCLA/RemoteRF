"""Verify the installed example and core wheels without radio dependencies."""
from importlib.metadata import distribution
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time

from remoterf_device_core.integrations import Context, IntegrationError, Manifest, Registry, object_data
from remoterf_device_core.integrations.ownership import Gate, Journal
from remoterf_device_core.integrations.runtime import Binding, Runtime


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("installed lifecycle did not drain")


def main():
    package = distribution("remoterf-example-counter")
    manifest_path = package.locate_file("rrf_example_counter/integration.json")
    definition = Manifest.from_file(manifest_path)
    integration_id = definition.integration_id
    disabled = Registry([definition])
    assert not disabled.discover(device_id="sample", device_type="sim_sdr")[0]["enabled"]
    assert "rrf_example_counter" not in sys.modules, "metadata discovery imported adapter code"

    with TemporaryDirectory() as work_dir:
        context = Context("sample", "sim_sdr", 1, Path(work_dir), Event())
        try:
            disabled.create(integration_id, context)
        except IntegrationError as exc:
            assert exc.code == "integration_disabled"
        else:
            raise AssertionError("disabled integration was loaded")
        assert "rrf_example_counter" not in sys.modules
        registry = Registry([definition], enabled={"sample": [integration_id]})
        configuration = registry.validate_start(
            integration_id, device_id="sample", device_type="sim_sdr",
            profile="counter", configuration={"initial": 10},
        )
        adapter = registry.create(integration_id, context)
        try:
            adapter.start("counter", object_data(adapter.validate("counter", configuration)))
            assert object_data(adapter.status()) == {"running": True, "value": 10}
            parameters = registry.validate_call(
                integration_id, device_id="sample", device_type="sim_sdr",
                action="increment", parameters={"delta": 2},
            )
            assert object_data(adapter.call("increment", parameters)) == {"value": 12}
        finally:
            context.cancelled.set()
            adapter.stop()
            adapter.stop()
        assert not object_data(adapter.status())["running"]

        # Exercise the installed ownership/runtime modules as well. This sample
        # has no external resources: native callbacks model a simulated owner,
        # and the resource verifier has nothing external to reconcile.
        with TemporaryDirectory() as journal_dir:
            journal = Journal(Path(journal_dir))
            runtime = Runtime(registry, capacity=1, workers=1)
            state = {"native": True}
            binding = Binding(Gate("simulated:sample", journal), "sample", "sim_sdr", "wheel-test", 1,
                Path(work_dir), lambda: True, lambda: state.update(native=False),
                lambda: not state["native"], lambda: state.update(native=True), lambda: True)
            try:
                op = runtime.start(binding, integration_id, "counter", {"initial": 10}, request_key="one")
                operation_id = op["operation_id"]
                wait_for(lambda: runtime.status(binding, operation_id)["state"] == "RUNNING")
                assert not state["native"] and journal.records()
                assert runtime.start(binding, integration_id, "counter", {"initial": 10},
                                     request_key="one")["operation_id"] == operation_id
                runtime.stop(binding, operation_id)
                wait_for(lambda: runtime.status(binding, operation_id)["released"])
                assert state["native"] and journal.records() == {}
            finally:
                runtime.shutdown()
                # close() refuses unresolved work instead of waiving ownership.
                def close_when_drained():
                    try:
                        runtime.close()
                        return True
                    except IntegrationError as exc:
                        if exc.code != "recovery_required":
                            raise
                        return False
                wait_for(close_when_drained)
                journal.close()
    assert "uhd" not in sys.modules
    assert "numpy" not in sys.modules
    assert "remoterf_device_core.idl.schema" not in sys.modules
    print("Installed adapter discovery, opt-in, validation, durable lifecycle and cleanup passed")


if __name__ == "__main__":
    main()
