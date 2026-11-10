"""Failed server startup releases idle ownership without touching real radios."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from remoterf_device_core.hardware_access import AccessTable
from remoterf_device_core.integrations import IntegrationError
from remoterf_device_core.integrations.ownership import Journal


@pytest.mark.parametrize('stage', ['certificate', 'inventory', 'transport', 'direct_path'])
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_failed_startup_closes_real_ownership_lock_and_preserves_error(tmp_path, monkeypatch, stage, cleanup_fails):
    from remoteRF_server.server import grpc_server as server
    from remoteRF_server.server import device_manager, integration_controller, startup_check, remote_admin_ssh
    from remoteRF_server.server.federation import legacy_state, runtime as federation

    # Exercise real integration initialization and its OS writer lock, while
    # replacing every transport/hardware boundary with an inert test double.
    (tmp_path / 'integrations.json').write_text(json.dumps({'version': 1, 'enabled': False}))
    manager = SimpleNamespace(integrations=integration_controller.Controller(), resume_admission=Mock())
    events = []

    def close_all(*, stop_admission=False):
        events.append('sessions')
        assert stop_admission
        manager.integrations.shutdown()

    manager.close_all = Mock(side_effect=close_all)
    monkeypatch.setattr(server, 'session_manager', manager)
    monkeypatch.setattr(device_manager, 'hardware_access', AccessTable())
    monkeypatch.setattr(device_manager, '_load_device_records', lambda: {})
    monkeypatch.setattr(device_manager, 'get_remoterf_root', lambda: tmp_path)
    monkeypatch.setattr(federation, 'federation_enabled', lambda: False)
    writer = Mock()
    monkeypatch.setattr(legacy_state, 'acquire_runtime_writer_lock', lambda _: writer)
    monkeypatch.setattr(server, 'reservation_handler', SimpleNamespace())
    monkeypatch.setattr(server, '_load_server_credentials', lambda _: object())
    monkeypatch.setattr(remote_admin_ssh, 'register', Mock())
    for name in ('GenericRPCServicer', 'DynamicControlV2Servicer', 'SampleDataV1Servicer'):
        monkeypatch.setattr(server, name, Mock())
    transport = Mock()
    transport.add_secure_port.return_value = 54321
    monkeypatch.setattr(server.grpc, 'server', lambda *args, **kwargs: transport)
    host = Mock()
    monkeypatch.setattr(server, 'host_session_proxy', host)
    direct = Mock()
    monkeypatch.setattr(server, 'direct_path', direct)
    cert = Mock()
    monkeypatch.setattr(server, 'start_cert_provider', Mock(return_value=(cert, None)))
    monkeypatch.setattr(server, 'stop_cert_provider', Mock())
    monkeypatch.setattr(startup_check, 'run', Mock())
    original = KeyboardInterrupt('startup interrupted') if stage == 'inventory' else RuntimeError('startup failed')

    def fail(*args, **kwargs):
        with pytest.raises(IntegrationError) as caught:
            Journal(tmp_path / 'db' / 'integrations' / 'ownership')
        assert caught.value.code == 'journal_busy'
        raise original

    {'certificate': server.start_cert_provider, 'inventory': startup_check.run,
     'transport': transport.start, 'direct_path': direct.start_listener}[stage].side_effect = fail

    def close_inventory():
        events.append('inventory')
        if cleanup_fails:
            raise RuntimeError('native cleanup failed')

    monkeypatch.setattr(server, 'close_local_devices', Mock(side_effect=close_inventory))
    if cleanup_fails:
        transport.stop.side_effect = RuntimeError('transport cleanup failed')
        host.close_all.side_effect = RuntimeError('host cleanup failed')
        direct.shutdown.side_effect = RuntimeError('listener cleanup failed')
        server.stop_cert_provider.side_effect = RuntimeError('certificate cleanup failed')
    permissions = Mock()
    try:
        with pytest.raises(type(original)) as caught:
            server.start_server_runtime(bind_host='127.0.0.1', grpc_port=54321, certificate_port=54322,
                cert_dir=tmp_path, include_host_tunnel=False, run_reservation_updates=False,
                home_federation_permissions=permissions)
        assert caught.value is original
        manager.close_all.assert_called_once_with(stop_admission=True)
        assert events == ['sessions', 'inventory']
        assert manager.integrations._stopping
        # A fresh operator can obtain the real ownership lock immediately;
        # retaining it until garbage collection/process exit is insufficient.
        recovered = Journal(tmp_path / 'db' / 'integrations' / 'ownership')
        recovered.close()
        host.close_all.assert_called_once()
        direct.shutdown.assert_called_once()
        permissions.close.assert_called_once()
        writer.close.assert_called_once()
        if stage != 'certificate':
            server.stop_cert_provider.assert_called_once_with(cert)
    finally:
        manager.integrations.close()
