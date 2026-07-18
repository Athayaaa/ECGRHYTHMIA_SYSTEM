from __future__ import annotations

import struct
import unittest

from crc16 import compute as crc16_compute
from packet_parser import (
    HEADER_SIZE,
    PACKET_SIZE_V2,
    SYNC,
    VERSION_V1,
    VERSION_V2,
    PacketParser,
)


def make_v2_packet(
    sequence: int,
    lead_i: int,
    lead_ii: int,
    *,
    timestamp_us: int = 1234,
    flags: int = 1,
    diagnostic: int = 0x04030201,
) -> bytes:
    packet = bytearray(SYNC)
    packet.extend(struct.pack("<BH", VERSION_V2, 25))
    packet.extend(
        struct.pack(
            "<IIHHBIII",
            sequence,
            timestamp_us,
            1,
            800,
            flags,
            lead_i,
            lead_ii,
            diagnostic,
        )
    )
    packet.extend(struct.pack("<H", crc16_compute(packet)))
    assert len(packet) == PACKET_SIZE_V2
    return bytes(packet)


class PacketParserV2Tests(unittest.TestCase):
    def test_v2_uint24_codes_remain_positive(self) -> None:
        for code in (0x000000, 0x798000, 0x800000, 0xF30000):
            with self.subTest(code=code):
                parsed = PacketParser(allowed_versions={VERSION_V2}).feed(
                    make_v2_packet(7, code, code)
                )
                self.assertEqual(len(parsed), 1)
                self.assertEqual(parsed[0].lead_i_code, code)
                self.assertEqual(parsed[0].lead_ii_code, code)
                self.assertEqual(parsed[0].ch3, 0)
                self.assertEqual(parsed[0].diagnostic, 0x04030201)
                self.assertEqual(parsed[0].error_status, 1)
                self.assertEqual(parsed[0].pending_after_claim, 2)
                self.assertEqual(parsed[0].maximum_pending, 3)
                self.assertEqual(parsed[0].overrun_saturated, 4)
                self.assertEqual(parsed[0].overrun_low8, 4)

    def test_v2_diagnostic_counters_have_saturated_semantics(self) -> None:
        packet = PacketParser(allowed_versions={VERSION_V2}).feed(
            make_v2_packet(1, 0x798000, 0x798000, diagnostic=0xFFFFFF00)
        )[0]
        self.assertEqual(packet.pending_after_claim, 255)
        self.assertEqual(packet.maximum_pending, 255)
        self.assertEqual(packet.overrun_saturated, 255)

    def test_v2_crc_failure_is_rejected(self) -> None:
        damaged = bytearray(make_v2_packet(1, 0x798000, 0x798000))
        damaged[HEADER_SIZE + 8] ^= 0x01
        parser = PacketParser(allowed_versions={VERSION_V2})
        self.assertEqual(parser.feed(damaged), [])
        self.assertEqual(parser.stats.crc_errors, 1)

    def test_v1_is_not_silently_accepted_by_strict_v2_parser(self) -> None:
        legacy = bytearray(SYNC)
        legacy.extend(struct.pack("<BH", VERSION_V1, 25))
        legacy.extend(struct.pack("<IIHHBiii", 1, 2, 1, 250, 0, -1, 2, 3))
        legacy.extend(struct.pack("<H", crc16_compute(legacy)))
        parser = PacketParser(allowed_versions={VERSION_V2})
        self.assertEqual(parser.feed(legacy), [])
        self.assertGreaterEqual(parser.stats.unsupported_version_errors, 1)

    def test_sequence_gap_is_attached_to_following_packet(self) -> None:
        parser = PacketParser(allowed_versions={VERSION_V2})
        packets = parser.feed(
            make_v2_packet(10, 1, 2) + make_v2_packet(13, 3, 4)
        )
        self.assertEqual([packet.missing_before for packet in packets], [0, 2])
        self.assertEqual(parser.stats.packet_loss_count, 2)
        self.assertEqual(parser.stats.sequence_gap_events, 1)

    def test_duplicate_does_not_replace_last_good_sequence_baseline(self) -> None:
        parser = PacketParser(allowed_versions={VERSION_V2})
        packets = parser.feed(
            make_v2_packet(10, 1, 2)
            + make_v2_packet(10, 1, 2)
            + make_v2_packet(11, 1, 2)
        )
        self.assertEqual(
            [packet.out_of_order_before for packet in packets],
            [False, True, False],
        )
        self.assertEqual(parser.stats.out_of_order_count, 1)
        self.assertEqual(parser.stats.packet_loss_count, 0)


if __name__ == "__main__":
    unittest.main()
