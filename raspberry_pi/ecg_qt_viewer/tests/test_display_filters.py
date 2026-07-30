"""Tests for non-destructive ECG viewer display filters."""

from __future__ import annotations

import unittest

import numpy as np

from ecg_qt_viewer.display_filters import apply_notch_filter_50hz, start_each_lead_at_zero


class DisplayFilterTests(unittest.TestCase):
    def test_notch_attenuates_50hz_without_mutating_source(self) -> None:
        sample_rate_hz = 250.0
        time = np.arange(2500, dtype=np.float64) / sample_rate_hz
        target = np.sin(2.0 * np.pi * 10.0 * time)
        interference = 0.7 * np.sin(2.0 * np.pi * 50.0 * time)
        lead_i = (target + interference).astype(np.float32)
        source = np.column_stack((lead_i, lead_i, lead_i)).astype(np.float32)
        original = source.copy()

        filtered = apply_notch_filter_50hz(source, sample_rate_hz)

        np.testing.assert_array_equal(source, original)
        self.assertEqual(filtered.dtype, np.float32)
        self.assertFalse(np.shares_memory(filtered, source))
        spectrum = np.abs(np.fft.rfft(filtered[:, 0]))
        frequencies = np.fft.rfftfreq(filtered.shape[0], 1.0 / sample_rate_hz)
        amplitude_10hz = spectrum[np.argmin(np.abs(frequencies - 10.0))]
        amplitude_50hz = spectrum[np.argmin(np.abs(frequencies - 50.0))]
        self.assertLess(amplitude_50hz, amplitude_10hz * 0.05)

    def test_requires_sample_rate_above_100hz(self) -> None:
        samples = np.zeros((2500, 3), dtype=np.float32)
        with self.assertRaisesRegex(ValueError, "Nyquist frequency"):
            apply_notch_filter_50hz(samples, 100.0)

    def test_start_each_lead_at_zero_is_non_destructive(self) -> None:
        samples = np.array(
            [[-0.4, 3.8, 4.2], [-0.3, 4.0, 4.4]], dtype=np.float32
        )
        original = samples.copy()

        shifted = start_each_lead_at_zero(samples)

        np.testing.assert_array_equal(samples, original)
        np.testing.assert_array_equal(shifted[0], np.zeros(3, dtype=np.float32))
        np.testing.assert_allclose(
            shifted[1], np.array([0.1, 0.2, 0.2], dtype=np.float32), atol=1e-6
        )


if __name__ == "__main__":
    unittest.main()
