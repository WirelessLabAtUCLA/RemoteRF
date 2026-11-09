"""Verify the installed example and core wheels without radio dependencies."""
from importlib.metadata import distribution
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event

from remoterf_device_core.integrations import Context, IntegrationError, Manifest, Registry, object_data


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
    assert "uhd" not in sys.modules
    assert "numpy" not in sys.modules
    assert "remoterf_device_core.idl.schema" not in sys.modules
    print("Installed adapter discovery, opt-in, validation, lifecycle and cleanup passed")


if __name__ == "__main__":
    main()
