"""CRC16-CCITT-FALSE used by the ESP32 ECG binary serial packet format."""

from __future__ import annotations

INITIAL_VALUE = 0xFFFF
POLYNOMIAL = 0x1021


def update(crc: int, value: int) -> int:
    """Update a CRC16-CCITT-FALSE value with one byte."""
    crc ^= (value & 0xFF) << 8

    for _ in range(8):
        if crc & 0x8000:
            crc = ((crc << 1) ^ POLYNOMIAL) & 0xFFFF
        else:
            crc = (crc << 1) & 0xFFFF

    return crc


def compute(data: bytes | bytearray | memoryview) -> int:
    """Compute CRC16-CCITT-FALSE over the supplied bytes."""
    crc = INITIAL_VALUE
    for value in data:
        crc = update(crc, value)
    return crc
