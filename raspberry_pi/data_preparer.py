"""Frame validation and NPY-ready array preparation."""

from __future__ import annotations

import numpy as np

CHANNEL_ORDER = ["Lead I", "Lead II", "Lead III"]


def expected_frame_size(sample_rate_hz: int, frame_seconds: int) -> int:
    return sample_rate_hz * frame_seconds


def prepare_frame_array(
    samples: np.ndarray, sample_rate_hz: int, frame_seconds: int
) -> np.ndarray:
    frame_size = expected_frame_size(sample_rate_hz, frame_seconds)
    if samples.shape != (frame_size, 3):
        raise ValueError(f"expected frame shape {(frame_size, 3)}, got {samples.shape}")

    return np.asarray(samples, dtype=np.float32)


def frame_time_axis(sample_rate_hz: int, frame_size: int) -> np.ndarray:
    return np.arange(frame_size, dtype=np.float32) / float(sample_rate_hz)
