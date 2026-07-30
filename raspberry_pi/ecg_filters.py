"""Reusable, non-destructive ECG signal filters for stored mV frames."""

from __future__ import annotations

import math

import numpy as np


NOTCH_50HZ_FREQUENCY_HZ = 50.0
DEFAULT_NOTCH_QUALITY_FACTOR = 30.0


def apply_notch_filter(
    samples: np.ndarray,
    sample_rate_hz: float,
    *,
    frequency_hz: float = NOTCH_50HZ_FREQUENCY_HZ,
    quality_factor: float = DEFAULT_NOTCH_QUALITY_FACTOR,
) -> np.ndarray:
    """Return a float32 copy filtered with a zero-phase biquad notch.

    Lead III is recomputed from the filtered Lead I and Lead II values so the
    stored frame continues to satisfy the project's Einthoven derivation rule.
    """

    if samples.ndim != 2 or samples.shape[1] != 3:
        raise ValueError("ECG samples must have shape (sample_count, 3)")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise ValueError("sample_rate_hz must be finite and positive")
    if not math.isfinite(frequency_hz) or frequency_hz <= 0.0:
        raise ValueError("frequency_hz must be finite and positive")
    if not math.isfinite(quality_factor) or quality_factor <= 0.0:
        raise ValueError("quality_factor must be finite and positive")
    if frequency_hz >= sample_rate_hz / 2.0:
        raise ValueError("notch frequency must be below the Nyquist frequency")

    values = np.asarray(samples, dtype=np.float64)
    if values.shape[0] < 4:
        filtered = np.array(values, dtype=np.float32, copy=True)
        filtered[:, 2] = filtered[:, 1] - filtered[:, 0]
        return filtered

    omega = 2.0 * math.pi * frequency_hz / sample_rate_hz
    alpha = math.sin(omega) / (2.0 * quality_factor)
    cosine = math.cos(omega)
    b0, b1, b2 = 1.0, -2.0 * cosine, 1.0
    a0, a1, a2 = 1.0 + alpha, -2.0 * cosine, 1.0 - alpha
    coefficients = (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)

    # Reflection padding reduces ringing at the beginning and end of each
    # independent ten-second frame before the forward/backward filter pass.
    pad_length = min(250, values.shape[0] - 1)
    padded = np.pad(values[:, :2], ((pad_length, pad_length), (0, 0)), mode="reflect")
    filtered_primary = _apply_biquad(padded, coefficients)
    filtered_primary = _apply_biquad(filtered_primary[::-1], coefficients)[::-1]
    filtered = np.empty_like(values, dtype=np.float32)
    filtered[:, :2] = filtered_primary[pad_length:-pad_length]
    filtered[:, 2] = filtered[:, 1] - filtered[:, 0]
    return filtered


def _apply_biquad(
    values: np.ndarray,
    coefficients: tuple[float, float, float, float, float],
) -> np.ndarray:
    """Apply one biquad to every supplied channel without altering input."""

    b0, b1, b2, a1, a2 = coefficients
    output = np.empty_like(values, dtype=np.float64)
    x1 = np.zeros(values.shape[1], dtype=np.float64)
    x2 = np.zeros(values.shape[1], dtype=np.float64)
    y1 = np.zeros(values.shape[1], dtype=np.float64)
    y2 = np.zeros(values.shape[1], dtype=np.float64)

    for index, current in enumerate(values):
        result = b0 * current + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        output[index] = result
        x2, x1 = x1, current
        y2, y1 = y1, result

    return output
