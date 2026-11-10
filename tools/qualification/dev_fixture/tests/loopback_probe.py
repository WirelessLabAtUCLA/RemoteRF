"""Child-process unit probe; fake authority here is never installed in the wheel."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import sys
from unittest.mock import patch

import grpc
import numpy as np

from rrf_dev_integration_fixture import backend, server

server.guard = lambda: (Path(sys.argv[1]), Path(sys.argv[2]))
schema_type = backend.install_backend()

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'client' / 'src'))
from remoterf.common.grpc import grpc_pb2_grpc as client_grpc
from remoterf.core.dynamic_v2_transport import DynamicV2Transport
from remoterf.drivers.dynamic_v2 import build_uhd_bindings
from remoteRF_server.common.grpc import grpc_pb2_grpc as server_grpc
from remoteRF_server.drivers.usrp.policy import USRPPolicy
from remoteRF_server.server import dynamic_v2 as v2
from remoteRF_server.server import device_manager
from remoterf_device_core.idl.schema import get_driver_class

# Exercise the startup seeding/loading path, including copied driver classes.
device_manager._load_all_drivers()
loaded_type = get_driver_class('usrp')
assert loaded_type is not schema_type
inventory = loaded_type()
inventory.bind_inventory({'serial': 'DEV_INTEGRATION_01', 'profile': 'usrp2901'})
context = {'schema': inventory, 'dtype': 'usrp', 'ident': 'DEV_INTEGRATION_01', 'name': 'usrp2901_dev'}
manager = v2.SessionManager()
service = grpc.server(ThreadPoolExecutor(max_workers=8))
server_grpc.add_DynamicControlV2Servicer_to_server(v2.DynamicControlV2Servicer(), service)
server_grpc.add_SampleDataV1Servicer_to_server(v2.SampleDataV1Servicer(), service)
port = service.add_insecure_port('127.0.0.1:0')
assert port
with ExitStack() as stack:
    for name, value in [('resolve_gid_for_token', lambda token: 401),
                        ('token_matches_device', lambda *args: True),
                        ('get_local_device_context', lambda gid: context),
                        ('session_manager', manager)]:
        stack.enter_context(patch.object(v2, name, value))
    stack.enter_context(patch.object(v2.USRPPolicy, 'load', return_value=USRPPolicy({})))
    service.start()
    try:
        with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
            grpc.channel_ready_future(channel).result(timeout=3)
            transport = DynamicV2Transport(control_stub=client_grpc.DynamicControlV2Stub(channel),
                sample_stub=client_grpc.SampleDataV1Stub(channel))
            description = transport.get_schema('unit-only-token')
            uhd, MultiUSRP = build_uhd_bindings(description, transport_factory=lambda: transport)
            with MultiUSRP('unit-only-token') as radio:
                assert radio.get_usrp_rx_info()['mboard_serial'] == 'DEV_INTEGRATION_01'
                assert radio.get_time_now().get_real_secs() > 0
                radio.set_rx_rate(1e6)
                radio.set_rx_gain(0)
                tuned = radio.set_rx_freq(uhd.types.TuneRequest(100e6))
                assert tuned.actual_rf_freq == radio.get_rx_freq() == 100e6
                assert radio.integration_list({})['enabled'] is False
                args = uhd.usrp.StreamArgs('fc32', 'sc16')
                args.channels = [0]
                with radio.get_rx_stream(args) as rx:
                    command = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
                    command.num_samps, command.stream_now = 4096, True
                    rx.issue_stream_cmd(command)
                    samples = np.zeros(4096, dtype=np.complex64)
                    metadata = uhd.types.RXMetadata()
                    assert rx.recv(samples, metadata, 2) == 4096
                    assert np.isfinite(samples).all() and np.any(samples)
                    assert np.argmax(np.abs(np.fft.fft(samples))) == 512
                    assert metadata.error_code.value == 'none'
                with radio.get_tx_stream(args) as tx:
                    metadata = uhd.types.TXMetadata()
                    metadata.start_of_burst = metadata.end_of_burst = True
                    assert tx.send(samples, metadata, 2) == 4096
            assert manager.metrics()['sessions'] == 0
    finally:
        manager.close_all()
        service.stop(0).wait(timeout=3)
