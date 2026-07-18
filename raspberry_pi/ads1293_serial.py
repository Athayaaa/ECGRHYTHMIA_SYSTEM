"""Text-mode handshake and binary-stream helpers for ADS1293 firmware V2.

The firmware deliberately multiplexes human-readable commands and binary ECG
packets on one serial port.  These helpers keep those modes separated: every
configuration transaction is completed while streaming is stopped, remaining
text is drained, and only then is protocol-V2 packet detection enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import serial

from ads1293_session import (
    FirmwareCompatibilityError,
    parse_config_dump_response,
    verify_firmware_config,
)
from crc16 import compute as crc16_compute
from packet_parser import (
    CRC_SIZE,
    HEADER_SIZE,
    SYNC,
    VERSION_V2,
    PacketParser,
    packet_size_for_version,
    payload_size_for_version,
)


@dataclass(frozen=True)
class FirmwareHandshake:
    config: dict[str, Any]
    compatibility_failures: tuple[str, ...]
    verified: bool
    raw_response: str


def send_command(serial_port: serial.Serial, command: str) -> None:
    """Send one CRLF-terminated firmware command."""

    serial_port.write((command.strip() + "\r\n").encode("ascii"))
    serial_port.flush()


def read_until_idle(
    serial_port: serial.Serial,
    *,
    timeout_sec: float,
    idle_sec: float = 0.08,
    read_size: int = 1024,
) -> bytes:
    """Read through a response until the line has been idle or times out."""

    deadline = time.monotonic() + max(0.0, timeout_sec)
    last_data_at: float | None = None
    result = bytearray()
    while time.monotonic() < deadline:
        data = serial_port.read(read_size)
        now = time.monotonic()
        if data:
            result.extend(data)
            last_data_at = now
            continue
        if last_data_at is not None and now - last_data_at >= idle_sec:
            break
    return bytes(result)


def command_response(
    serial_port: serial.Serial,
    command: str,
    *,
    timeout_sec: float,
    read_size: int = 1024,
) -> bytes:
    send_command(serial_port, command)
    return read_until_idle(
        serial_port,
        timeout_sec=timeout_sec,
        read_size=read_size,
    )


def reset_input_safely(serial_port: serial.Serial) -> None:
    try:
        serial_port.reset_input_buffer()
    except (AttributeError, OSError, ValueError, serial.SerialException):
        pass


def stop_text_and_binary_output(
    serial_port: serial.Serial,
    *,
    response_timeout_sec: float = 0.8,
    read_size: int = 1024,
) -> None:
    """Enter quiet text-command mode and discard all earlier binary bytes."""

    command_response(
        serial_port,
        "ads stream stop",
        timeout_sec=response_timeout_sec,
        read_size=read_size,
    )
    command_response(
        serial_port,
        "ads debug off",
        timeout_sec=response_timeout_sec,
        read_size=read_size,
    )
    reset_input_safely(serial_port)


def verified_config_handshake(
    serial_port: serial.Serial,
    *,
    allow_unverified: bool = False,
    response_timeout_sec: float = 1.5,
    read_size: int = 1024,
) -> FirmwareHandshake:
    """Query one JSON config dump and validate the corrected acquisition ABI."""

    stop_text_and_binary_output(
        serial_port,
        response_timeout_sec=response_timeout_sec,
        read_size=read_size,
    )
    raw = command_response(
        serial_port,
        "ads config dump",
        timeout_sec=response_timeout_sec,
        read_size=read_size,
    )
    text = raw.decode("utf-8", errors="replace")
    config = parse_config_dump_response(text)
    failures = tuple(
        verify_firmware_config(config, allow_unverified=allow_unverified)
    )
    reset_input_safely(serial_port)
    return FirmwareHandshake(
        config=config,
        compatibility_failures=failures,
        verified=not failures,
        raw_response=text,
    )


def find_v2_header(buffer: bytearray) -> int | None:
    """Find a structurally valid V2 header, leaving CRC validation to parser."""

    search_from = 0
    expected_payload = payload_size_for_version(VERSION_V2)
    while True:
        index = buffer.find(SYNC, search_from)
        if index < 0:
            return None
        if len(buffer) - index < HEADER_SIZE:
            return None
        version = buffer[index + 2]
        payload_size = int.from_bytes(buffer[index + 3 : index + 5], "little")
        if version == VERSION_V2 and payload_size == expected_payload:
            return index
        search_from = index + 1


def find_complete_crc_valid_v2_packet(buffer: bytearray) -> int | None:
    """Return the offset of a complete CRC-valid V2 packet, if available.

    A five-byte structural header is not sufficient evidence that binary
    streaming started: startup text, a truncated transfer, or corrupt bytes can
    contain the same byte pattern.  This scanner leaves incomplete data in the
    caller's buffer and skips corrupt candidates until a complete V2 packet is
    available.
    """

    expected_payload = payload_size_for_version(VERSION_V2)
    packet_size = packet_size_for_version(VERSION_V2)
    assert expected_payload is not None
    assert packet_size is not None

    search_from = 0
    while True:
        index = buffer.find(SYNC, search_from)
        if index < 0:
            return None
        if len(buffer) - index < HEADER_SIZE:
            return None

        version = buffer[index + 2]
        payload_size = int.from_bytes(buffer[index + 3 : index + 5], "little")
        if version != VERSION_V2 or payload_size != expected_payload:
            search_from = index + 1
            continue
        if len(buffer) - index < packet_size:
            return None

        candidate = bytes(buffer[index : index + packet_size])
        expected_crc = int.from_bytes(candidate[-CRC_SIZE:], "little")
        if crc16_compute(candidate[:-CRC_SIZE]) == expected_crc:
            return index
        search_from = index + 1


def _trim_to_possible_sync(buffer: bytearray) -> None:
    """Bound startup memory without damaging any incomplete V2 candidate.

    After the CRC scanner has rejected every complete candidate, only the last
    ``packet_size - 1`` bytes can still be the prefix of a future complete
    packet.  Keeping that full suffix is important: selecting the last 0xAA
    byte alone can mistake a payload byte for sync and delete the real header.
    """

    packet_size = packet_size_for_version(VERSION_V2)
    assert packet_size is not None
    retain = packet_size - 1
    if len(buffer) > retain:
        del buffer[: len(buffer) - retain]


def start_v2_binary_stream(
    serial_port: serial.Serial,
    *,
    response_timeout_sec: float = 0.8,
    detect_timeout_sec: float = 5.0,
    read_size: int = 1024,
) -> bytes:
    """Start streaming and return bytes beginning at a CRC-valid V2 packet."""

    command_response(
        serial_port,
        "ads start",
        timeout_sec=response_timeout_sec,
        read_size=read_size,
    )
    reset_input_safely(serial_port)
    send_command(serial_port, "ads stream start")

    deadline = time.monotonic() + max(0.0, detect_timeout_sec)
    buffer = bytearray()
    while time.monotonic() < deadline:
        data = serial_port.read(read_size)
        if not data:
            continue
        buffer.extend(data)
        index = find_complete_crc_valid_v2_packet(buffer)
        if index is not None:
            if index:
                del buffer[:index]
            return bytes(buffer)
        _trim_to_possible_sync(buffer)
    return b""


def collect_v2_packets(
    serial_port: serial.Serial,
    *,
    sample_count: int,
    timeout_sec: float,
    initial_data: bytes = b"",
    read_size: int = 1024,
) -> list:
    """Collect exactly ``sample_count`` CRC-valid V2 packets or fail."""

    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    parser = PacketParser(allowed_versions={VERSION_V2})
    packets = parser.feed(initial_data) if initial_data else []
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while len(packets) < sample_count and time.monotonic() < deadline:
        data = serial_port.read(read_size)
        if data:
            packets.extend(parser.feed(data))
    if len(packets) < sample_count:
        raise TimeoutError(
            f"received {len(packets)}/{sample_count} valid ADS1293 V2 samples; "
            f"parser={parser.stats.as_dict()}"
        )
    return packets[:sample_count]


__all__ = [
    "FirmwareCompatibilityError",
    "FirmwareHandshake",
    "collect_v2_packets",
    "command_response",
    "find_complete_crc_valid_v2_packet",
    "find_v2_header",
    "read_until_idle",
    "reset_input_safely",
    "send_command",
    "start_v2_binary_stream",
    "stop_text_and_binary_output",
    "verified_config_handshake",
]
