"""Attach server-owned policy only to authenticated host tunnel requests."""

from remoterf_device_core.tx_limits import POLICY_KEY, rule_wire

from ..common.utils import map_arg, unmap_arg
from ..host import host_tunnel_server as hts
from .device_manager import get_tx_freq_limit


def forward(
    registry, *, gid, host_id, device_id, function_name, args, timeout_sec=30.0
):
    rule = get_tx_freq_limit(gid)
    args = dict(args)
    args.pop(POLICY_KEY, None)
    # An old host must be refused BEFORE any operation can tune or transmit.
    session = (
        registry.session_for_host(host_id)
        if hasattr(registry, "session_for_host")
        else None
    )
    supported = session is not None and "tx_limits_v1" in session.capabilities
    if rule is not None and not supported:
        caps = hts.handle_host_device(
            registry,
            host_id=host_id,
            device_id=device_id,
            function_name="TXLimits:capabilities",
            args={},
            timeout_sec=timeout_sec,
        )
        if "tx_limits_version" not in caps or unmap_arg(caps["tx_limits_version"]) != 1:
            raise ValueError(
                "This device has TX limits; upgrade its Host before using it"
            )
    args[POLICY_KEY] = map_arg(rule_wire(rule))
    return hts.handle_host_device(
        registry,
        host_id=host_id,
        device_id=device_id,
        function_name=function_name,
        args=args,
        timeout_sec=timeout_sec,
    )
