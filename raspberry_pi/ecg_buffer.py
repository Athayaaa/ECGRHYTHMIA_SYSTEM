"""Fixed-size 10-second ECG ring buffer."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from packet_parser import ECGPacket


@dataclass(frozen=True)
class ECGFrame:
    samples: np.ndarray
    sequences: np.ndarray
    timestamp_us: np.ndarray


class ECGRingBuffer:
    def __init__(self, sample_rate_hz: int, frame_seconds: int) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.frame_seconds = frame_seconds
        self.capacity = sample_rate_hz * frame_seconds
        self._samples = np.zeros((self.capacity, 3), dtype=np.float32)
        self._sequences = np.zeros(self.capacity, dtype=np.uint32)
        self._timestamp_us = np.zeros(self.capacity, dtype=np.uint32)
        self._write_index = 0
        self._count = 0
        self.total_samples = 0

    @property
    def is_full(self) -> bool:
        return self._count == self.capacity

    def append(self, packet: ECGPacket) -> None:
        self._samples[self._write_index] = (packet.ch1, packet.ch2, packet.ch3)
        self._sequences[self._write_index] = packet.sequence
        self._timestamp_us[self._write_index] = packet.timestamp_us

        self._write_index = (self._write_index + 1) % self.capacity
        self._count = min(self._count + 1, self.capacity)
        self.total_samples += 1

    def snapshot(self) -> ECGFrame:
        if not self.is_full:
            raise ValueError(
                f"ring buffer needs {self.capacity} samples, has {self._count}"
            )

        if self._write_index == 0:
            samples = self._samples.copy()
            sequences = self._sequences.copy()
            timestamp_us = self._timestamp_us.copy()
        else:
            samples = np.concatenate(
                (self._samples[self._write_index :], self._samples[: self._write_index])
            )
            sequences = np.concatenate(
                (
                    self._sequences[self._write_index :],
                    self._sequences[: self._write_index],
                )
            )
            timestamp_us = np.concatenate(
                (
                    self._timestamp_us[self._write_index :],
                    self._timestamp_us[: self._write_index],
                )
            )

        return ECGFrame(
            samples=samples.astype(np.float32, copy=False),
            sequences=sequences,
            timestamp_us=timestamp_us,
        )
