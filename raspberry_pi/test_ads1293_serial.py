from __future__ import annotations

import struct
import unittest
from unittest.mock import patch

from ads1293_serial import (
    find_complete_crc_valid_v2_packet,
    start_v2_binary_stream,
)
from crc16 import compute as crc16_compute


def make_v2_packet(sequence: int = 0) -> bytes:
    packet = bytearray(b"\xAA\x55")
    packet.extend(struct.pack("<BH", 2, 25))
    packet.extend(
        struct.pack(
            "<IIHHBIII",
            sequence,
            sequence * 3750,
            1,
            800,
            1,
            0x798000,
            0x798000,
            0,
        )
    )
    packet.extend(struct.pack("<H", crc16_compute(packet)))
    return bytes(packet)


class ChunkSerial:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


class ADS1293SerialStartupTests(unittest.TestCase):
    def test_complete_crc_packet_required(self) -> None:
        packet = make_v2_packet()
        self.assertIsNone(
            find_complete_crc_valid_v2_packet(bytearray(packet[:5]))
        )
        self.assertIsNone(
            find_complete_crc_valid_v2_packet(bytearray(packet[:-1]))
        )

        corrupt = bytearray(packet)
        corrupt[-1] ^= 0x80
        prefix = b"startup text\n"
        buffer = bytearray(prefix + corrupt + packet)
        self.assertEqual(
            find_complete_crc_valid_v2_packet(buffer),
            len(prefix) + len(corrupt),
        )

    def test_stream_start_waits_for_packet_remainder(self) -> None:
        # Sequence 0xAA puts another 0xAA byte inside the partial payload. The
        # real header must remain buffered instead of trimming to that byte.
        packet = make_v2_packet(0xAA)
        port = ChunkSerial([b"text\n" + packet[:10], packet[10:]])
        with (
            patch("ads1293_serial.command_response", return_value=b""),
            patch("ads1293_serial.reset_input_safely"),
            patch("ads1293_serial.send_command"),
        ):
            detected = start_v2_binary_stream(
                port,
                response_timeout_sec=0.0,
                detect_timeout_sec=0.2,
                read_size=1024,
            )
        self.assertEqual(detected, packet)


if __name__ == "__main__":
    unittest.main()
