import math
import struct
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

# TI mmWave SDK standard magic word
MAGIC_WORD = b"\x02\x01\x04\x03\x06\x05\x08\x07"

# TLV Message Types (TI mmWave SDK 3.x)
MMWDEMO_OUTPUT_MSG_DETECTED_POINTS = 1
MMWDEMO_OUTPUT_MSG_RANGE_PROFILE = 2
MMWDEMO_OUTPUT_MSG_NOISE_PROFILE = 3
MMWDEMO_OUTPUT_MSG_AZIMUT_STATIC_HEAT_MAP = 4
MMWDEMO_OUTPUT_MSG_RANGE_DOPPLER_HEAT_MAP = 5
MMWDEMO_OUTPUT_MSG_STATS = 6
MMWDEMO_OUTPUT_MSG_DETECTED_POINTS_SIDE_INFO = 7
MMWDEMO_OUTPUT_MSG_AZIMUT_ELEVATION_STATIC_HEAT_MAP = 8
MMWDEMO_OUTPUT_MSG_TEMPERATURE_STATS = 9


@dataclass
class RadarPoint:
    """Represents a single detected 3D radar point."""
    x: float          # Lateral position (meters, +Right, -Left)
    y: float          # Distance from radar (meters, +Forward)
    z: float          # Vertical height (meters, +Up, -Down)
    velocity: float   # Radial velocity (m/s, +Away, -Towards)
    range: float      # Euclidean range (meters)
    azimuth_deg: float    # Azimuth angle in degrees
    elevation_deg: float  # Elevation angle in degrees
    snr_db: float     # Signal-to-Noise Ratio (dB)
    noise_db: float   # Noise level (dB)

    def __repr__(self) -> str:
        return (
            f"RadarPoint(x={self.x:+.2f}m, y={self.y:+.2f}m, z={self.z:+.2f}m, "
            f"range={self.range:.2f}m, vel={self.velocity:+.2f}m/s, "
            f"az={self.azimuth_deg:+.1f}°, el={self.elevation_deg:+.1f}°, "
            f"snr={self.snr_db:.1f}dB)"
        )


@dataclass
class RadarFrame:
    """Represents a complete frame received from the radar."""
    frame_number: int
    cpu_cycles: int
    subframe_number: int
    num_detected_objects: int
    points: List[RadarPoint] = field(default_factory=list)
    azimuth_static_heatmap: Optional[np.ndarray] = None
    timestamp: float = 0.0
    raw_packet: bytes = b""


class TLVParser:
    """
    Stateful stream parser for TI mmWave radar serial packets (SDK 3.x).
    Accumulates raw incoming bytes, finds packet boundaries, and decodes TLVs.
    """

    def __init__(self):
        self._buffer = bytearray()
        self.header_len = 40  # SDK 3.x header length in bytes

    def feed(self, chunk: bytes, timestamp: float = 0.0) -> List[RadarFrame]:
        """
        Feeds raw UART data into the parser buffer and returns any completed frames.
        """
        if chunk:
            self._buffer.extend(chunk)

        frames: List[RadarFrame] = []

        while True:
            # Look for magic word
            idx = self._buffer.find(MAGIC_WORD)
            if idx == -1:
                # Keep only last 7 bytes in case magic word was split across chunks
                if len(self._buffer) > 7:
                    self._buffer = self._buffer[-7:]
                break

            # Discard any junk before the magic word
            if idx > 0:
                self._buffer = self._buffer[idx:]

            # Check if we have at least the full 40-byte header
            if len(self._buffer) < self.header_len:
                break

            # Unpack SDK 3.x header:
            # 8s: magic (8 bytes)
            #  I: version (4 bytes)
            #  I: totalPacketLen (4 bytes)
            #  I: platform (4 bytes)
            #  I: frameNumber (4 bytes)
            #  I: timeCpuCycles (4 bytes)
            #  I: numDetectedObj (4 bytes)
            #  I: numTLVs (4 bytes)
            #  I: subFrameNumber (4 bytes)
            header_bytes = self._buffer[: self.header_len]
            (
                magic,
                version,
                total_len,
                platform,
                frame_number,
                cpu_cycles,
                num_detected_obj,
                num_tlvs,
                subframe_number,
            ) = struct.unpack("<8sIIIIIIII", header_bytes)

            # Sanity checks on header values
            if total_len < self.header_len or total_len > 65536 or num_tlvs > 20:
                # Corrupted header; advance by 1 byte to find next potential magic word
                self._buffer = self._buffer[1:]
                continue

            # Check if complete packet has arrived
            if len(self._buffer) < total_len:
                break

            # Extract packet and advance buffer
            packet = bytes(self._buffer[:total_len])
            self._buffer = self._buffer[total_len:]

            # Parse TLVs
            frame = self._parse_packet_tlvs(
                packet=packet,
                frame_number=frame_number,
                cpu_cycles=cpu_cycles,
                subframe_number=subframe_number,
                num_detected_obj=num_detected_obj,
                num_tlvs=num_tlvs,
                timestamp=timestamp,
            )
            frames.append(frame)

        return frames

    def _parse_packet_tlvs(
        self,
        packet: bytes,
        frame_number: int,
        cpu_cycles: int,
        subframe_number: int,
        num_detected_obj: int,
        num_tlvs: int,
        timestamp: float,
    ) -> RadarFrame:
        """Parses individual TLV blocks from an extracted frame packet."""
        frame = RadarFrame(
            frame_number=frame_number,
            cpu_cycles=cpu_cycles,
            subframe_number=subframe_number,
            num_detected_objects=num_detected_obj,
            timestamp=timestamp,
            raw_packet=packet,
        )

        detected_points: List[dict] = []
        side_info: List[dict] = []

        offset = self.header_len
        packet_len = len(packet)

        for _ in range(num_tlvs):
            if offset + 8 > packet_len:
                break

            tlv_type, tlv_len = struct.unpack("<II", packet[offset : offset + 8])
            payload_start = offset + 8
            payload_end = payload_start + tlv_len

            if payload_end > packet_len:
                break

            payload = packet[payload_start:payload_end]

            if tlv_type == MMWDEMO_OUTPUT_MSG_DETECTED_POINTS:
                # Each point: 16 bytes (float32 x, y, z, v)
                num_points = tlv_len // 16
                for p_idx in range(num_points):
                    p_slice = payload[p_idx * 16 : (p_idx + 1) * 16]
                    if len(p_slice) == 16:
                        x, y, z, v = struct.unpack("<ffff", p_slice)
                        detected_points.append({"x": x, "y": y, "z": z, "v": v})

            elif tlv_type == MMWDEMO_OUTPUT_MSG_DETECTED_POINTS_SIDE_INFO:
                # Each side info: 4 bytes (int16 snr, int16 noise in 0.1 dB units)
                num_sides = tlv_len // 4
                for s_idx in range(num_sides):
                    s_slice = payload[s_idx * 4 : (s_idx + 1) * 4]
                    if len(s_slice) == 4:
                        snr, noise = struct.unpack("<hh", s_slice)
                        side_info.append({
                            "snr_db": snr / 10.0,
                            "noise_db": noise / 10.0,
                        })

            elif tlv_type == MMWDEMO_OUTPUT_MSG_AZIMUT_STATIC_HEAT_MAP:
                # Array of complex16 numbers: [Im0, Re0, Im1, Re1, ...]
                # Each sample is 4 bytes (int16 imaginary, int16 real)
                raw_samples = np.frombuffer(payload, dtype=np.int16)
                if len(raw_samples) >= 2:
                    imag = raw_samples[0::2].astype(np.float32)
                    real = raw_samples[1::2].astype(np.float32)
                    c_data = real + 1j * imag
                    # Reshape to (num_range_bins, 8 virtual azimuth antennas)
                    if len(c_data) % 8 == 0:
                        frame.azimuth_static_heatmap = c_data.reshape(-1, 8)
                    else:
                        frame.azimuth_static_heatmap = c_data

            offset = payload_end

        # Combine coordinates with side info
        points: List[RadarPoint] = []
        for i, pt in enumerate(detected_points):
            x = pt["x"]
            y = pt["y"]
            z = pt["z"]
            v = pt["v"]

            r = math.sqrt(x * x + y * y + z * z)
            # Azimuth angle in degrees: atan2(x, y) (0 is boresight, + is right)
            azimuth = math.degrees(math.atan2(x, y)) if (x != 0 or y != 0) else 0.0

            # Elevation angle in degrees: atan2(z, ground_range) (+ is up)
            ground_r = math.sqrt(x * x + y * y)
            elevation = math.degrees(math.atan2(z, ground_r)) if (z != 0 or ground_r != 0) else 0.0

            snr_val = side_info[i]["snr_db"] if i < len(side_info) else 0.0
            noise_val = side_info[i]["noise_db"] if i < len(side_info) else 0.0

            points.append(
                RadarPoint(
                    x=x,
                    y=y,
                    z=z,
                    velocity=v,
                    range=r,
                    azimuth_deg=azimuth,
                    elevation_deg=elevation,
                    snr_db=snr_val,
                    noise_db=noise_val,
                )
            )

        frame.points = points
        return frame
