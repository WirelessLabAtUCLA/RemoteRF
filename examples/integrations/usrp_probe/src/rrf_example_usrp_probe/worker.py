"""Owned receive-only worker. There is no transmit path or caller command hook."""
from __future__ import annotations

import argparse
import json
import time

from . import MARKER, configuration, serial_selector


def receive_burst(device, settings, *, uhd, np):
    """Configure one RX channel and receive a bounded, finite, nonempty burst."""
    channel = settings["channel"]
    device.set_rx_rate(settings["rate"], channel)
    device.set_rx_freq(uhd.types.TuneRequest(float(settings["frequency"])), channel)
    device.set_rx_gain(settings["gain"], channel)
    args = uhd.usrp.StreamArgs("fc32", "sc16")
    args.channels = [channel]
    streamer = device.get_rx_stream(args)
    command = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
    command.num_samps = settings["samples"]
    command.stream_now = True
    streamer.issue_stream_cmd(command)
    received = 0
    deadline = time.monotonic() + 5.0
    while received < settings["samples"] and time.monotonic() < deadline:
        samples = np.empty((1, settings["samples"] - received), dtype=np.complex64)
        metadata = uhd.types.RXMetadata()
        count = int(streamer.recv(samples, metadata, 0.5))
        error = str(metadata.error_code).rsplit(".", 1)[-1].lower()
        if error not in {"none", "error_code_none", "timeout", "error_code_timeout"}:
            raise RuntimeError("receive probe reported a native RX error")
        if not 0 <= count <= samples.shape[1] or (count and not np.isfinite(samples[:, :count]).all()):
            raise RuntimeError("receive probe returned invalid samples")
        received += count
    if received < settings["samples"]:
        raise RuntimeError("receive probe did not receive the requested finite burst")
    return {"received": received, "rate": float(device.get_rx_rate(channel)),
            "frequency": float(device.get_rx_freq(channel)), "gain": float(device.get_rx_gain(channel)),
            "channel": channel}


def run(serial, settings):
    import numpy as np
    import uhd
    serial = serial_selector({"serial": serial})
    settings = configuration("receive", settings)
    device = uhd.usrp.MultiUSRP(f"serial={serial}")
    actual = str(device.get_usrp_rx_info(settings["channel"]).get("mboard_serial", ""))
    if actual.casefold() != serial.casefold():
        raise RuntimeError("receive probe opened an unexpected physical serial")
    result = receive_burst(device, settings, uhd=uhd, np=np)
    version = getattr(uhd, "get_version_string", None)
    result.update(event="ready", serial=serial, uhd_version=str(version()) if callable(version) else "unknown")
    print(MARKER + json.dumps(result, separators=(",", ":")), flush=True)
    # Keep owning the actual native radio. Periodic finite RX bursts ensure a
    # dead/disconnected radio causes a process failure instead of false health.
    while True:
        time.sleep(0.2)
        receive_burst(device, settings, uhd=uhd, np=np)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--configuration", required=True)
    args = parser.parse_args()
    if len(args.configuration) > 4096:
        parser.error("configuration is too large")
    try:
        run(args.serial, json.loads(args.configuration))
    except (Exception, KeyboardInterrupt):
        # Native exception text can include deployment paths or device details.
        # The trusted supervisor retains bounded native logs for local review.
        print(MARKER + '{"event":"failed"}', flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
