"""Installed metadata/contract smoke; deliberately never opens a real radio."""
from importlib.metadata import distribution
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event

from remoterf_device_core.integrations import Context, IntegrationError, Manifest, Registry


def main():
    definition = Manifest.from_file(distribution('remoterf-example-usrp-probe').locate_file(
        'rrf_example_usrp_probe/integration.json'))
    registry = Registry([definition])
    assert not registry.discover(device_id='7', device_type='usrp')[0]['enabled']
    assert 'rrf_example_usrp_probe' not in sys.modules
    with TemporaryDirectory() as directory:
        context = Context('7', 'usrp', 1, Path(directory), Event(), selector={'serial': 'EXAMPLE'})
        try:
            registry.create(definition.integration_id, context)
        except IntegrationError as exc:
            assert exc.code == 'integration_disabled'
        else:
            raise AssertionError('disabled probe was constructed')
        registry = Registry([definition], enabled={'7': [definition.integration_id]})
        adapter = registry.create(definition.integration_id, context)
        assert adapter.validate('receive', {})['samples'] == 4096
        try:
            adapter.start('receive', {})
        except RuntimeError as exc:
            assert 'supervision' in str(exc)
        else:
            raise AssertionError('probe started without owned process supervision')
        adapter.stop()
        adapter.stop()
    assert 'uhd' not in sys.modules and 'numpy' not in sys.modules
    print('Installed USRP probe metadata, disabled default and supervision guard passed; no radio used')


if __name__ == '__main__':
    main()
