"""Binary serial packet parser for ESP32 dummy ECG frames."""

from __future__ import annotations

from dataclasses import dataclass
import struct

from crc16 import compute as crc16_compute

SYNC = b"\xAA\x55"
VERSION = 0x01
SAMPLE_COUNT = 1
PAYLOAD_SIZE = 25
HEADER_SIZE = 5
CRC_SIZE = 2
PACKET_SIZE = HEADER_SIZE + PAYLOAD_SIZE + CRC_SIZE

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


@dataclass
class ParserStats:
    packets_ok: int = 0
    crc_errors: int = 0
    header_errors: int = 0
    bytes_discarded: int = 0
    packet_loss_count: int = 0
    out_of_order_count: int = 0
    last_sequence: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "packets_ok": self.packets_ok,
            "crc_errors": self.crc_errors,
            "header_errors": self.header_errors,
            "bytes_discarded": self.bytes_discarded,
            "packet_loss_count": self.packet_loss_count,
            "out_of_order_count": self.out_of_order_count,
            "last_sequence": self.last_sequence,
        }


class PacketParser:
    """Incremental parser with sync recovery, CRC validation, and loss stats."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.stats = ParserStats()

    def feed(self, data: bytes | bytearray | memoryview) -> list[ECGPacket]:
        self._buffer.extend(data)
        packets: list[ECGPacket] = []

        while True:
            if not self._align_to_sync():
                break

            if len(self._buffer) < PACKET_SIZE:
                break

            candidate = bytes(self._buffer[:PACKET_SIZE])
            if not self._header_is_valid(candidate):
                self.stats.header_errors += 1
                self._discard(1)
                continue

            expected_crc = struct.unpack_from("<H", candidate, PACKET_SIZE - CRC_SIZE)[0]
            actual_crc = crc16_compute(candidate[:-CRC_SIZE])
            if actual_crc != expected_crc:
                self.stats.crc_errors += 1
                self._discard(1)
                continue

            packet = self._parse_packet(candidate)
            self._update_sequence_stats(packet.sequence)
            self.stats.packets_ok += 1
            packets.append(packet)
            self._consume(PACKET_SIZE)

        return packets

    def _align_to_sync(self) -> bool:
        sync_index = self._buffer.find(SYNC)

        if sync_index == 0:
            return True

        if sync_index > 0:
            self._discard(sync_index)
            return True

        keep_last = len(self._buffer) > 0 and self._buffer[-1] == SYNC[0]
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
    def _header_is_valid(packet: bytes) -> bool:
        if packet[:2] != SYNC:
            return False

        version = packet[2]
        payload_size = struct.unpack_from("<H", packet, 3)[0]
        return version == VERSION and payload_size == PAYLOAD_SIZE

    @staticmethod
    def _parse_packet(packet: bytes) -> ECGPacket:
        (
            sequence,
            timestamp_us,
            sample_count,
            sampling_rate_hz,
            flags,
            ch1,
            ch2,
            ch3,
        ) = struct.unpack_from(
            "<IIHHBiii", packet, HEADER_SIZE
        )
        return ECGPacket(
            sequence=sequence,
            timestamp_us=timestamp_us,
            sample_count=sample_count,
            sampling_rate_hz=sampling_rate_hz,
            ch1=ch1,
            ch2=ch2,
            ch3=ch3,
            flags=flags,
        )

    def _update_sequence_stats(self, sequence: int) -> None:
        previous = self.stats.last_sequence
        self.stats.last_sequence = sequence

        # The first valid packet after startup establishes the sequence baseline.
        if previous is None:
            return

        expected = (previous + 1) & 0xFFFFFFFF
        if sequence == expected:
            return

        forward_gap = (sequence - expected) & 0xFFFFFFFF
        if 0 < forward_gap <= MAX_FORWARD_SEQUENCE_GAP:
            self.stats.packet_loss_count += forward_gap
        else:
            self.stats.out_of_order_count += 1
