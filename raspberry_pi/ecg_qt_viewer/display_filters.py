"""Non-destructive DSP helpers used only by the ECG viewer display."""

from __future__ import annotations

import numpy as np

try:
    from ecg_filters import (
        DEFAULT_NOTCH_QUALITY_FACTOR,
        NOTCH_50HZ_FREQUENCY_HZ,
        apply_notch_filter,
    )
except ImportError:  # Support package imports from the repository root.
    from raspberry_pi.ecg_filters import (
        DEFAULT_NOTCH_QUALITY_FACTOR,
        NOTCH_50HZ_FREQUENCY_HZ,
        apply_notch_filter,
    )

NOTCH_FREQUENCY_HZ = NOTCH_50HZ_FREQUENCY_HZ
NOTCH_QUALITY_FACTOR = DEFAULT_NOTCH_QUALITY_FACTOR


def apply_notch_filter_50hz(
    samples: np.ndarray,
    sample_rate_hz: float,
    *,
    quality_factor: float = NOTCH_QUALITY_FACTOR,
) -> np.ndarray:
    """Return a float32 display copy with a zero-phase 50 Hz notch applied.

    This helper never modifies ``samples``. It intentionally lives in the viewer
    layer so recorded frames, conversion outputs, validation, and AI input remain
    untouched.
    """

    return apply_notch_filter(
        samples,
        sample_rate_hz,
        frequency_hz=NOTCH_FREQUENCY_HZ,
        quality_factor=quality_factor,
    )


def start_each_lead_at_zero(samples: np.ndarray) -> np.ndarray:
    """Return a display copy whose first sample is zero for every lead."""

    if samples.ndim != 2 or samples.shape[1] != 3:
        raise ValueError("ECG samples must have shape (sample_count, 3)")
    if samples.shape[0] == 0:
        return np.array(samples, dtype=np.float32, copy=True)
    return np.asarray(samples - samples[0:1], dtype=np.float32)
