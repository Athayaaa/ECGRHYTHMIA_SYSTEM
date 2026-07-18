"""Incremental parsers for ECGRHYTHMIA serial protocols V1 and V2.

Protocol V1 is retained for dummy/legacy streams. Protocol V2 carries two
unsigned ADS1293 offset-code measurements at the native ADC cadence; its third
32-bit word is diagnostic data and is never interpreted as Lead III.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import struct
from typing import Iterable

from crc16 import compute as crc16_compute

SYNC = b"\xAA\x55"
VERSION_V1 = 0x01
VERSION_V2 = 0x02
# Backward-compatible name used by legacy callers.
VERSION = VERSION_V1
SUPPORTED_VERSIONS = frozenset({VERSION_V1, VERSION_V2})
SAMPLE_COUNT = 1
PAYLOAD_SIZE_V1 = 25
PAYLOAD_SIZE_V2 = 25
PAYLOAD_SIZE = PAYLOAD_SIZE_V1
HEADER_SIZE = 5
CRC_SIZE = 2
PACKET_SIZE_V1 = HEADER_SIZE + PAYLOAD_SIZE_V1 + CRC_SIZE
PACKET_SIZE_V2 = HEADER_SIZE + PAYLOAD_SIZE_V2 + CRC_SIZE
PACKET_SIZE = PACKET_SIZE_V1

V2_NATIVE_RATE_NUMERATOR = 800
V2_NATIVE_RATE_DENOMINATOR = 3
V2_FLAG_CONFIG_VERIFIED = 1 << 0
V2_FLAG_ADS_ERROR = 1 << 1
V2_FLAG_BACKLOG_OVERRUN = 1 << 2
V2_FLAG_CODE_OUT_OF_RANGE = 1 << 3
V2_FLAG_PREVIOUS_TX_FAILURE = 1 << 4
V2_FLAG_SELFTEST_ACTIVE = 1 << 5

MAX_FORWARD_SEQUENCE_GAP = 0x7FFFFFFF


@dataclass(frozen=True)
class ECGPacket:
    sequence: int
    timestamp_us: int
    sample_count: int
    sampling_rate_hz: int
    ch1: int
    ch2: int
    ch3: int
    flags: int
    protocol_version: int = VERSION_V1
    native_rate_numerator: int | None = None
    diagnostic: int = 0
    missing_before: int = 0
    out_of_order_before: bool = False

    @property
    def is_ads1293_native(self) -> bool:
        return self.protocol_version == VERSION_V2

    @property
    def lead_i_code(self) -> int:
        return self.ch1

    @property
    def lead_ii_code(self) -> int:
        return self.ch2

    @property
    def error_status(self) -> int:
        return self.diagnostic & 0xFF if self.is_ads1293_native else 0

    @property
    def pending_after_claim(self) -> int:
        """Saturated pending count at the firmware's atomic claim boundary."""

        return (self.diagnostic >> 8) & 0xFF if self.is_ads1293_native else 0

    @property
    def maximum_pending(self) -> int:
        """Saturated maximum pending count (255 means 255 or greater)."""

        return (self.diagnostic >> 16) & 0xFF if self.is_ads1293_native else 0

    @property
    def overrun_saturated(self) -> int:
        """Saturated cumulative overrun count; exact loss uses sequence gaps."""

        return (self.diagnostic >> 24) & 0xFF if self.is_ads1293_native else 0

    @property
    def overrun_low8(self) -> int:
        """Backward-compatible alias; firmware clamps rather than wraps."""

        return self.overrun_saturated


@dataclass
class ParserStats:
    packets_ok: int = 0
    packets_v1: int = 0
    packets_v2: int = 0
    crc_errors: int = 0
    header_errors: int = 0
    unsupported_version_errors: int = 0
    bytes_discarded: int = 0
    packet_loss_count: int = 0
    sequence_gap_events: int = 0
    out_of_order_count: int = 0
    last_sequence: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "packets_ok": self.packets_ok,
            "packets_v1": self.packets_v1,
            "packets_v2": self.packets_v2,
            "crc_errors": self.crc_errors,
            "header_errors": self.header_errors,
            "unsupported_version_errors": self.unsupported_version_errors,
            "bytes_discarded": self.bytes_discarded,
            "packet_loss_count": self.packet_loss_count,
            "sequence_gap_events": self.sequence_gap_events,
            "out_of_order_count": self.out_of_order_count,
            "last_sequence": self.last_sequence,
        }


def payload_size_for_version(version: int) -> int | None:
    if version == VERSION_V1:
        return PAYLOAD_SIZE_V1
    if version == VERSION_V2:
        return PAYLOAD_SIZE_V2
    return None


def packet_size_for_version(version: int) -> int | None:
    payload_size = payload_size_for_version(version)
    if payload_size is None:
        return None
    return HEADER_SIZE + payload_size + CRC_SIZE


class PacketParser:
    """Sync-recovering parser with CRC and native sequence-gap reporting."""

    def __init__(self, allowed_versions: Iterable[int] | None = None) -> None:
        self._buffer = bytearray()
        selected = SUPPORTED_VERSIONS if allowed_versions is None else allowed_versions
        self.allowed_versions = frozenset(int(version) for version in selected)
        if not self.allowed_versions:
            raise ValueError("allowed_versions must not be empty")
        unknown = self.allowed_versions - SUPPORTED_VERSIONS
        if unknown:
            raise ValueError(f"unsupported protocol versions requested: {sorted(unknown)}")
        self.stats = ParserStats()

    def feed(self, data: bytes | bytearray | memoryview) -> list[ECGPacket]:
        self._buffer.extend(data)
        packets: list[ECGPacket] = []

        while True:
            if not self._align_to_sync():
                break
            if len(self._buffer) < HEADER_SIZE:
                break

            version = self._buffer[2]
            expected_payload = payload_size_for_version(version)
            if expected_payload is None or version not in self.allowed_versions:
                self.stats.header_errors += 1
                self.stats.unsupported_version_errors += 1
                self._discard(1)
                continue

            payload_size = struct.unpack_from("<H", self._buffer, 3)[0]
            if payload_size != expected_payload:
                self.stats.header_errors += 1
                self._discard(1)
                continue

            packet_size = HEADER_SIZE + payload_size + CRC_SIZE
            if len(self._buffer) < packet_size:
                break

            candidate = bytes(self._buffer[:packet_size])
            expected_crc = struct.unpack_from("<H", candidate, packet_size - CRC_SIZE)[0]
            actual_crc = crc16_compute(candidate[:-CRC_SIZE])
            if actual_crc != expected_crc:
                self.stats.crc_errors += 1
                self._discard(1)
                continue

            packet = self._parse_packet(candidate, version)
            missing, out_of_order = self._update_sequence_stats(packet.sequence)
            packet = replace(
                packet,
                missing_before=missing,
                out_of_order_before=out_of_order,
            )
            self.stats.packets_ok += 1
            if version == VERSION_V1:
                self.stats.packets_v1 += 1
            else:
                self.stats.packets_v2 += 1
            packets.append(packet)
            self._consume(packet_size)

        return packets

    def reset_sequence_baseline(self) -> None:
        """Make the next valid packet establish a fresh sequence baseline."""

        self.stats.last_sequence = None

    def _align_to_sync(self) -> bool:
        sync_index = self._buffer.find(SYNC)
        if sync_index == 0:
            return True
        if sync_index > 0:
            self._discard(sync_index)
            return True

        keep_last = bool(self._buffer) and self._buffer[-1] == SYNC[0]
        discard_count = len(self._buffer) - 1 if keep_last else len(self._buffer)
        if discard_count > 0:
            self._discard(discard_count)
        return False

    def _discard(self, count: int) -> None:
        del self._buffer[:count]
        self.stats.bytes_discarded += count

    def _consume(self, count: int) -> None:
        del self._buffer[:count]

    @staticmethod
    def _parse_packet(packet: bytes, version: int) -> ECGPacket:
        if version == VERSION_V1:
            (
                sequence,
                timestamp_us,
                sample_count,
                sampling_rate_hz,
                flags,
                ch1,
                ch2,
                ch3,
            ) = struct.unpack_from("<IIHHBiii", packet, HEADER_SIZE)
            return ECGPacket(
                sequence=sequence,
                timestamp_us=timestamp_us,
                sample_count=sample_count,
                sampling_rate_hz=sampling_rate_hz,
                ch1=ch1,
                ch2=ch2,
                ch3=ch3,
                flags=flags,
                protocol_version=VERSION_V1,
            )

        (
            sequence,
            timestamp_us,
            sample_count,
            native_rate_numerator,
            flags,
            lead_i_code,
            lead_ii_code,
            diagnostic,
        ) = struct.unpack_from("<IIHHBIII", packet, HEADER_SIZE)
        return ECGPacket(
            sequence=sequence,
            timestamp_us=timestamp_us,
            sample_count=sample_count,
            sampling_rate_hz=0,
            ch1=lead_i_code,
            ch2=lead_ii_code,
            ch3=0,
            flags=flags,
            protocol_version=VERSION_V2,
            native_rate_numerator=native_rate_numerator,
            diagnostic=diagnostic,
        )

    def _update_sequence_stats(self, sequence: int) -> tuple[int, bool]:
        previous = self.stats.last_sequence
        if previous is None:
            self.stats.last_sequence = sequence
            return 0, False

        expected = (previous + 1) & 0xFFFFFFFF
        if sequence == expected:
            self.stats.last_sequence = sequence
            return 0, False

        forward_gap = (sequence - expected) & 0xFFFFFFFF
        if 0 < forward_gap <= MAX_FORWARD_SEQUENCE_GAP:
            self.stats.last_sequence = sequence
            self.stats.packet_loss_count += forward_gap
            self.stats.sequence_gap_events += 1
            return forward_gap, False

        # A stale/duplicate packet must not replace the last known-good
        # baseline, otherwise one bad packet cascades into false gap reports.
        self.stats.out_of_order_count += 1
        return 0, True
