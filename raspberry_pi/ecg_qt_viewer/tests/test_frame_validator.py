"""Tests for calibrated sample and metadata validation."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ecg_qt_viewer.frame_repository import FrameRecord
from ecg_qt_viewer.frame_validator import (
    EXPECTED_CHANNEL_ORDER,
    EXPECTED_SHAPE,
    FrameValidator,
    ValidationStatus,
    validate_frame,
)


def make_valid_samples(dtype: np.dtype | type = np.float32) -> np.ndarray:
    """Create deterministic data that satisfies the three-lead equation."""

    lead_i = np.linspace(-0.45, 0.55, EXPECTED_SHAPE[0], dtype=dtype)
    lead_ii = np.linspace(-0.2, 0.8, EXPECTED_SHAPE[0], dtype=dtype)
    lead_iii = lead_ii - lead_i
    return np.column_stack((lead_i, lead_ii, lead_iii)).astype(dtype, copy=False)


def valid_metadata() -> dict[str, object]:
    return {
        "sample_rate_hz": 250,
        "unit": "mV",
        "channel_order": list(EXPECTED_CHANNEL_ORDER),
        "sample_count": 2500,
        "shape": [2500, 3],
        "dtype": "float32",
        "duration_seconds": 10.0,
    }


class FrameValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = FrameValidator()

    def test_valid_frame_passes(self) -> None:
        samples = make_valid_samples()

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.PASS)
        self.assertTrue(result.can_plot)
        self.assertTrue(result.is_pass)
        self.assertEqual(result.samples.dtype, np.float32)  # type: ignore[union-attr]
        self.assertEqual(result.source_shape, (2500, 3))
        self.assertEqual(result.source_dtype, "float32")
        self.assertLessEqual(result.max_abs_mv, 1.0)  # type: ignore[operator]
        self.assertLessEqual(result.lead_iii_max_error_mv, 1e-5)  # type: ignore[operator]
        self.assertEqual(len(result.per_lead_stats), 3)
        self.assertEqual(result.per_lead_stats[1].name, "Lead II")

    def test_wrong_shape_fails_and_blocks_plotting(self) -> None:
        samples = np.zeros((2499, 3), dtype=np.float32)

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.FAIL)
        self.assertFalse(result.can_plot)
        self.assertTrue(any("Shape" in reason for reason in result.errors))

    def test_nan_fails_and_blocks_plotting(self) -> None:
        samples = make_valid_samples()
        samples[20, 0] = np.nan

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.FAIL)
        self.assertFalse(result.can_plot)
        self.assertTrue(any("NaN" in reason for reason in result.errors))

    def test_inf_fails_and_blocks_plotting(self) -> None:
        samples = make_valid_samples()
        samples[20, 1] = np.inf

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.FAIL)
        self.assertFalse(result.can_plot)
        self.assertTrue(any("Inf" in reason for reason in result.errors))

    def test_excessive_amplitude_fails_but_remains_plotable(self) -> None:
        """Centered amplitude > 10 mV should FAIL (dynamic range too large)."""
        samples = np.zeros(EXPECTED_SHAPE, dtype=np.float32)
        # Baseline 0, so centered == absolute. +11 mV exceeds limit.
        samples[100, 0] = np.float32(11.0)
        samples[100, 2] = samples[100, 1] - samples[100, 0]

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.FAIL)
        self.assertTrue(result.can_plot)
        self.assertIsNotNone(result.centered_max_abs_mv)
        self.assertGreater(result.centered_max_abs_mv, 10.0)
        self.assertTrue(any("Centered" in reason for reason in result.errors))

    def test_exactly_ten_mv_is_not_excessive(self) -> None:
        samples = make_valid_samples()
        samples[100, 0] = np.float32(10.0)
        samples[100, 1] = np.float32(9.9)
        samples[100, 2] = samples[100, 1] - samples[100, 0]

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.PASS)
        self.assertTrue(result.can_plot)

    def test_large_baseline_small_variation_is_warning(self) -> None:
        """Frame with baseline -10 mV and variation ±1 mV → WARNING not FAIL."""
        samples = np.zeros(EXPECTED_SHAPE, dtype=np.float32)
        lead_i = np.linspace(-1.5, -0.5, EXPECTED_SHAPE[0], dtype=np.float32)
        lead_ii = np.linspace(-10.5, -9.5, EXPECTED_SHAPE[0], dtype=np.float32)
        lead_iii = lead_ii - lead_i
        samples[:, 0] = lead_i
        samples[:, 1] = lead_ii
        samples[:, 2] = lead_iii

        result = self.validator.validate(samples, valid_metadata())

        # Must be WARNING, not FAIL
        self.assertEqual(result.status, ValidationStatus.WARNING)
        self.assertTrue(result.can_plot)
        self.assertIsNotNone(result.centered_max_abs_mv)
        self.assertLess(result.centered_max_abs_mv, 2.0)
        self.assertGreater(result.max_abs_mv, 10.0)
        self.assertTrue(any("Large DC baseline" in w for w in result.warnings))
        self.assertEqual(len(result.errors), 0)

    def test_baseline_zero_with_large_centered_amplitude_fails(self) -> None:
        """Baseline 0 mV with variation ±11 mV → FAIL."""
        samples = np.zeros(EXPECTED_SHAPE, dtype=np.float32)
        samples[:, 0] = np.sin(
            np.linspace(0, 4 * np.pi, EXPECTED_SHAPE[0])
        ).astype(np.float32) * 11.0
        samples[:, 1] = np.sin(
            np.linspace(0, 4 * np.pi, EXPECTED_SHAPE[0])
        ).astype(np.float32) * 11.0
        samples[:, 2] = samples[:, 1] - samples[:, 0]

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.FAIL)
        self.assertTrue(result.can_plot)
        self.assertGreater(result.centered_max_abs_mv, 10.0)

    def test_source_array_unchanged_after_validation(self) -> None:
        """Validation must not modify the source array."""
        samples = make_valid_samples()
        original = samples.copy()

        self.validator.validate(samples, valid_metadata())

        np.testing.assert_array_equal(samples, original)

    def test_large_baseline_source_unchanged(self) -> None:
        """Validation with large baseline must not modify source array."""
        samples = np.zeros(EXPECTED_SHAPE, dtype=np.float32)
        samples[:, 0] = np.float32(-1.0)
        samples[:, 1] = np.float32(-10.0)
        samples[:, 2] = samples[:, 1] - samples[:, 0]
        original = samples.copy()

        self.validator.validate(samples, valid_metadata())

        np.testing.assert_array_equal(samples, original)

    def test_new_fields_populated_for_valid_frame(self) -> None:
        """New fields baseline_median_mv, centered_max_abs_mv, peak_to_peak_mv must be populated."""
        samples = make_valid_samples()
        result = self.validator.validate(samples, valid_metadata())

        self.assertIsNotNone(result.baseline_median_mv)
        self.assertEqual(len(result.baseline_median_mv), 3)
        self.assertIsNotNone(result.centered_max_abs_mv)
        self.assertIsInstance(result.centered_max_abs_mv, float)
        self.assertIsNotNone(result.peak_to_peak_mv)
        self.assertEqual(len(result.peak_to_peak_mv), 3)

    def test_metadata_records_baseline_and_centered(self) -> None:
        """ValidationResult for large baseline includes correct centered metrics."""
        samples = np.zeros(EXPECTED_SHAPE, dtype=np.float32)
        samples[:, 0] = np.float32(-2.0)
        samples[:, 1] = np.float32(-10.0)
        samples[:, 2] = samples[:, 1] - samples[:, 0]

        result = self.validator.validate(samples, valid_metadata())

        self.assertAlmostEqual(result.baseline_median_mv[0], -2.0, places=1)
        self.assertAlmostEqual(result.baseline_median_mv[1], -10.0, places=1)
        self.assertLess(result.centered_max_abs_mv, 0.1)
        for ptp in result.peak_to_peak_mv:
            self.assertLess(ptp, 0.1)

    def test_lead_iii_inconsistency_warns_but_does_not_block(self) -> None:
        samples = make_valid_samples()
        samples[500, 2] += np.float32(2e-5)

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.WARNING)
        self.assertTrue(result.can_plot)
        self.assertGreater(result.lead_iii_max_error_mv, 1e-5)  # type: ignore[operator]
        self.assertTrue(any("Lead III" in reason for reason in result.warnings))

    def test_lead_iii_error_at_tolerance_is_allowed(self) -> None:
        samples = np.zeros(EXPECTED_SHAPE, dtype=np.float32)
        samples[0, 2] = np.float32(1e-5)

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.PASS)
        self.assertTrue(result.can_plot)

    def test_missing_metadata_warns_but_does_not_block(self) -> None:
        result = self.validator.validate(make_valid_samples())

        self.assertEqual(result.status, ValidationStatus.WARNING)
        self.assertTrue(result.can_plot)
        self.assertTrue(any("metadata is missing" in reason for reason in result.warnings))

    def test_metadata_mismatch_warns_but_does_not_block(self) -> None:
        metadata = valid_metadata()
        metadata.update(
            sample_rate_hz=500,
            unit="uV",
            channel_order=["Lead II", "Lead I", "Lead III"],
        )

        result = self.validator.validate(make_valid_samples(), metadata)

        self.assertEqual(result.status, ValidationStatus.WARNING)
        self.assertTrue(result.can_plot)
        self.assertGreaterEqual(len(result.warnings), 3)

    def test_float64_is_converted_only_in_memory_with_warning(self) -> None:
        samples = make_valid_samples(np.float64)
        original = samples.copy()

        result = self.validator.validate(samples, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.WARNING)
        self.assertTrue(result.can_plot)
        self.assertEqual(result.samples.dtype, np.float32)  # type: ignore[union-attr]
        self.assertEqual(samples.dtype, np.float64)
        np.testing.assert_array_equal(samples, original)
        self.assertTrue(any("in memory only" in warning for warning in result.warnings))

    def test_npy_only_record_produces_metadata_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame_000001_mv.npy"
            np.save(path, make_valid_samples(), allow_pickle=False)
            record = FrameRecord(frame_id=1, npy_path=path, metadata_path=None)

            result = self.validator.validate(record)

        self.assertEqual(result.status, ValidationStatus.WARNING)
        self.assertTrue(result.can_plot)

    def test_corrupt_metadata_is_warning_while_npy_remains_plotable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame_000001_mv.npy"
            metadata_path = path.with_suffix(".json")
            np.save(path, make_valid_samples(), allow_pickle=False)
            metadata_path.write_text("{incomplete", encoding="utf-8")
            record = FrameRecord(1, path, metadata_path)

            result = self.validator.validate(record)

        self.assertEqual(result.status, ValidationStatus.WARNING)
        self.assertTrue(result.can_plot)
        self.assertTrue(any("could not be read" in item for item in result.warnings))

    def test_corrupt_npy_fails_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame_000001_mv.npy"
            path.write_bytes(b"not an npy file")

            result = validate_frame(path, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.FAIL)
        self.assertFalse(result.can_plot)
        self.assertTrue(any("could not be read" in item for item in result.errors))

    def test_huge_declared_shape_fails_without_materializing_array(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame_000001_mv.npy"
            with path.open("wb") as handle:
                handle.write(np.lib.format.magic(1, 0))
                np.lib.format.write_array_header_1_0(
                    handle,
                    {
                        "descr": np.lib.format.dtype_to_descr(np.dtype(np.float32)),
                        "fortran_order": False,
                        "shape": (10**10, 3),
                    },
                )

            result = validate_frame(path, valid_metadata())

        self.assertEqual(result.status, ValidationStatus.FAIL)
        self.assertFalse(result.can_plot)
        self.assertIsNone(result.samples)
        self.assertTrue(any("could not be read" in item for item in result.errors))



if __name__ == "__main__":
    unittest.main()
