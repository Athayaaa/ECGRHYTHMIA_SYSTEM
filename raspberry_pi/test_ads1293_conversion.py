"""Deterministic ADS1293 offset-code conversion and calibration tests."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np

from calibration_utils import (
    ADS1293_ADC_ENCODING,
    ADS1293_CONVERSION_FORMULA_ID,
    CORRECTED_ADCMAX,
    CORRECTED_PIPELINE_PROFILE,
    CORRECTED_ZERO_CODE,
    CalibrationError,
    ads1293_nominal_scale_mV_per_count,
    ads1293_offset_code_to_mv,
    convert_raw_to_mv,
    load_calibration_config,
    offset_code_range_stats,
    pipeline_profile_parameters,
    require_frame_session_link,
    require_frame_output_sample_rate,
    require_corrected_frame_provenance,
    validate_calibration_compatibility,
    write_calibrated_frame,
)


SCRIPT_DIR = Path(__file__).resolve().parent
NOMINAL_CONFIG = SCRIPT_DIR / "calibration_config_ads1293_266p667_nominal.yaml"
LEGACY_CONFIG = SCRIPT_DIR / "calibration_config_legacy_640_to_250.yaml"
ORIGINAL_LEGACY_CONFIG = SCRIPT_DIR / "calibration_config.yaml"


class ADS1293ConversionTests(unittest.TestCase):
    def test_nominal_scale_and_selftest_codes(self) -> None:
        scale = ads1293_nominal_scale_mV_per_count(CORRECTED_ADCMAX)
        self.assertAlmostEqual(scale, 8.61166225749559e-05, places=17)

        codes = np.asarray(
            [CORRECTED_ZERO_CODE, 10_285_056, 5_640_192],
            dtype=np.float32,
        )
        converted = ads1293_offset_code_to_mv(codes, CORRECTED_ADCMAX)
        self.assertEqual(converted.dtype, np.dtype("float32"))
        np.testing.assert_allclose(converted, [0.0, 200.0, -200.0], atol=2e-5)

    def test_unsigned_codes_above_sign_bit_remain_positive(self) -> None:
        codes = np.asarray([0x800001, CORRECTED_ADCMAX], dtype=np.float32)
        converted = ads1293_offset_code_to_mv(codes, CORRECTED_ADCMAX)
        self.assertGreater(float(converted[0]), 0.0)
        self.assertGreater(float(converted[1]), float(converted[0]))

    def test_conversion_derives_lead_iii_after_measured_leads(self) -> None:
        calibration = load_calibration_config(NOMINAL_CONFIG)
        raw = np.asarray(
            [
                [CORRECTED_ZERO_CODE + 1000, CORRECTED_ZERO_CODE + 3000, 2000],
                [CORRECTED_ZERO_CODE - 4000, CORRECTED_ZERO_CODE + 2000, 6000],
            ],
            dtype=np.float32,
        )
        converted = convert_raw_to_mv(raw, calibration)
        self.assertEqual(converted.dtype, np.dtype("float32"))
        np.testing.assert_allclose(converted[:, 2], converted[:, 1] - converted[:, 0])

    def test_out_of_range_and_near_rail_are_distinct(self) -> None:
        margin = 8192.0
        raw = np.asarray(
            [
                [-1.0, CORRECTED_ADCMAX + 1.0, CORRECTED_ADCMAX + 2.0],
                [margin, CORRECTED_ADCMAX - margin, CORRECTED_ADCMAX - 2 * margin],
            ],
            dtype=np.float32,
        )
        stats = offset_code_range_stats(raw, CORRECTED_ADCMAX, margin)
        self.assertEqual(stats["below_range_count"], 1)
        self.assertEqual(stats["above_range_count"], 1)
        self.assertEqual(stats["near_low_rail_count"], 1)
        self.assertEqual(stats["near_high_rail_count"], 1)
        self.assertEqual(stats["status"], "FAIL")
        self.assertTrue(any("Lead I" in reason and "below" in reason for reason in stats["reasons"]))
        self.assertTrue(any("Lead II" in reason and "above" in reason for reason in stats["reasons"]))
        self.assertTrue(any("low ADC rail" in reason for reason in stats["reasons"]))
        self.assertTrue(any("high ADC rail" in reason for reason in stats["reasons"]))
        self.assertEqual(
            stats["derived_lead_iii"]["encoding"], "signed_derived_difference"
        )
        self.assertNotIn("below_range_count", stats["derived_lead_iii"])

        calibration = load_calibration_config(NOMINAL_CONFIG)
        with self.assertRaises(CalibrationError):
            convert_raw_to_mv(raw, calibration)

    def test_near_rail_only_is_warning(self) -> None:
        raw = np.asarray(
            [[8192.0, CORRECTED_ADCMAX - 8192.0, CORRECTED_ADCMAX - 16384.0]],
            dtype=np.float32,
        )
        stats = offset_code_range_stats(raw, CORRECTED_ADCMAX)
        self.assertEqual(stats["out_of_range_count"], 0)
        self.assertEqual(stats["near_rail_count"], 2)
        self.assertEqual(stats["status"], "WARNING")
        self.assertEqual(len(stats["reasons"]), 2)
        self.assertTrue(all("ADC rail" in reason for reason in stats["reasons"]))


class CalibrationMigrationTests(unittest.TestCase):
    def test_legacy_backup_is_byte_identical_and_explicit_only(self) -> None:
        self.assertEqual(ORIGINAL_LEGACY_CONFIG.read_bytes(), LEGACY_CONFIG.read_bytes())
        with self.assertRaises(CalibrationError):
            load_calibration_config(LEGACY_CONFIG)
        legacy = load_calibration_config(LEGACY_CONFIG, allow_legacy=True)
        self.assertTrue(legacy["legacy_explicitly_allowed"])

    def test_nominal_config_has_corrected_fingerprint(self) -> None:
        calibration = load_calibration_config(NOMINAL_CONFIG)
        self.assertEqual(calibration["pipeline_profile"], CORRECTED_PIPELINE_PROFILE)
        self.assertEqual(calibration["adc_encoding"], ADS1293_ADC_ENCODING)
        self.assertEqual(calibration["adcmax"], CORRECTED_ADCMAX)
        self.assertEqual(calibration["zero_code"], CORRECTED_ZERO_CODE)
        self.assertEqual(calibration["calibration_status"], "preliminary")
        self.assertTrue(calibration["prosim_recalibration_required"])

    def test_frame_rate_must_match_calibration_output_rate(self) -> None:
        calibration = load_calibration_config(NOMINAL_CONFIG)
        self.assertEqual(
            require_frame_output_sample_rate({"sample_rate_hz": 250}, calibration),
            250,
        )
        with self.assertRaises(CalibrationError):
            require_frame_output_sample_rate({"sample_rate_hz": 240}, calibration)

    def test_strict_fingerprint_match_mismatch_and_missing(self) -> None:
        calibration = load_calibration_config(NOMINAL_CONFIG)
        runtime = pipeline_profile_parameters(CORRECTED_PIPELINE_PROFILE)
        self.assertTrue(
            validate_calibration_compatibility(calibration, runtime).compatible
        )

        changed_register = dict(runtime, r2=5)
        result = validate_calibration_compatibility(calibration, changed_register)
        self.assertFalse(result.compatible)
        self.assertTrue(any("r2" in reason for reason in result.reasons))

        changed_adcmax = dict(runtime, adcmax=CORRECTED_ADCMAX - 1)
        result = validate_calibration_compatibility(calibration, changed_adcmax)
        self.assertFalse(result.compatible)
        self.assertTrue(any("adcmax" in reason for reason in result.reasons))

        for field, value in (
            ("zero_code", CORRECTED_ZERO_CODE + 1),
            ("vref_nominal_v", 2.5),
            ("analog_gain", 3.0),
        ):
            with self.subTest(field=field):
                changed_conversion_source = dict(runtime, **{field: value})
                result = validate_calibration_compatibility(
                    calibration,
                    changed_conversion_source,
                )
                self.assertFalse(result.compatible)
                self.assertTrue(any(field in reason for reason in result.reasons))

        missing = dict(runtime)
        missing.pop("r3_ch2")
        result = validate_calibration_compatibility(calibration, missing, strict=True)
        self.assertFalse(result.compatible)
        self.assertTrue(any("missing r3_ch2" in reason for reason in result.reasons))

    def test_corrected_config_cannot_omit_fingerprint_fields(self) -> None:
        incomplete = pipeline_profile_parameters(CORRECTED_PIPELINE_PROFILE)
        incomplete.pop("r2")
        incomplete["calibration_source"] = "unit_test"
        incomplete["scale_mV_per_count"] = 1.0
        incomplete["offset_count"] = {
            "lead_i": CORRECTED_ZERO_CODE,
            "lead_ii": CORRECTED_ZERO_CODE,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incomplete.yaml"
            lines = [f"{key}: {value}" for key, value in incomplete.items() if not isinstance(value, dict)]
            lines.extend(
                [
                    "offset_count:",
                    f"  lead_i: {CORRECTED_ZERO_CODE}",
                    f"  lead_ii: {CORRECTED_ZERO_CODE}",
                ]
            )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(CalibrationError):
                load_calibration_config(path)

    def test_datasheet_nominal_cannot_hide_a_measured_offset(self) -> None:
        config = pipeline_profile_parameters(CORRECTED_PIPELINE_PROFILE)
        config.update(
            {
                "calibration_source": "datasheet_nominal",
                "calibration_status": "preliminary",
                "offset_count": {
                    "lead_i": CORRECTED_ZERO_CODE + 100,
                    "lead_ii": CORRECTED_ZERO_CODE,
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shifted_nominal.yaml"
            import yaml

            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            with self.assertRaises(CalibrationError):
                load_calibration_config(path)

    def test_corrected_frame_provenance_is_strict_but_allows_warning_conversion(self) -> None:
        metadata = {
            "protocol_version": 2,
            "session_id": "session-corrected-001",
            "firmware_commit": "0123456789abcdef",
            "npy_sha256": "1" * 64,
            "bin_sha256": "2" * 64,
            "acquisition_status": "PASS",
            "acquisition_reasons": [],
            "packet_loss_count_frame": 0,
            "sequence_gap_events_frame": 0,
            "resampler_reset_count_frame": 0,
            "signal_quality_status": "PASS",
            "signal_quality": {
                "status": "PASS",
                "reasons": [],
                "below_range_count": 0,
                "above_range_count": 0,
                "near_low_rail_count": 0,
                "near_high_rail_count": 0,
                "out_of_range_count": 0,
                "near_rail_count": 0,
                "native_below_range_count": 0,
                "native_above_range_count": 0,
                "native_near_low_rail_count": 0,
                "native_near_high_rail_count": 0,
                "measured_leads": {},
                "derived_lead_iii": {},
            },
        }
        self.assertTrue(require_corrected_frame_provenance(metadata)["model_ready"])

        warning = dict(metadata)
        warning["signal_quality_status"] = "WARNING"
        warning["signal_quality"] = {
            "status": "WARNING",
            "reasons": ["synthetic near-rail warning"],
            "below_range_count": 0,
            "above_range_count": 0,
            "near_low_rail_count": 1,
            "near_high_rail_count": 0,
            "out_of_range_count": 0,
            "near_rail_count": 1,
            "native_below_range_count": 0,
            "native_above_range_count": 0,
            "native_near_low_rail_count": 1,
            "native_near_high_rail_count": 0,
            "measured_leads": {},
            "derived_lead_iii": {},
        }
        self.assertFalse(
            require_corrected_frame_provenance(warning)["model_ready"]
        )
        with self.assertRaises(CalibrationError):
            require_corrected_frame_provenance(
                warning,
                require_signal_quality_pass=True,
            )

        for changed in (
            {**metadata, "acquisition_status": "FAIL"},
            {key: value for key, value in metadata.items() if key != "acquisition_status"},
            {**metadata, "packet_loss_count_frame": 1},
            {
                **metadata,
                "signal_quality": {
                    **metadata["signal_quality"],
                    "near_low_rail_count": 1,
                    "near_rail_count": 1,
                },
            },
            {
                **metadata,
                "signal_quality": {
                    **metadata["signal_quality"],
                    "native_below_range_count": 0.5,
                },
            },
            {
                **metadata,
                "signal_quality": {
                    **metadata["signal_quality"],
                    "native_below_range_count": float("inf"),
                },
            },
            {
                **metadata,
                "signal_quality": {
                    **metadata["signal_quality"],
                    "out_of_range_count": 0,
                    "native_above_range_count": 1,
                },
            },
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(CalibrationError):
                    require_corrected_frame_provenance(changed)

    def test_external_session_must_be_linked_to_the_frame(self) -> None:
        fingerprint = pipeline_profile_parameters(CORRECTED_PIPELINE_PROFILE)
        linked = {
            **fingerprint,
            "protocol_version": 2,
            "session_id": "session-corrected-001",
            "firmware_commit": "0123456789abcdef",
        }
        self.assertEqual(
            require_frame_session_link(
                linked,
                linked,
            ),
            "session-corrected-001",
        )
        for frame_metadata, session_metadata in (
            ({}, {"session_id": "session-corrected-001"}),
            ({"session_id": "session-corrected-001"}, {}),
            (
                {"session_id": "session-corrected-001"},
                {"session_id": "session-unrelated-999"},
            ),
        ):
            with self.subTest(
                frame_metadata=frame_metadata,
                session_metadata=session_metadata,
            ):
                with self.assertRaises(CalibrationError):
                    require_frame_session_link(frame_metadata, session_metadata)

    def test_export_metadata_records_formula_parameters_and_compatibility(self) -> None:
        runtime = pipeline_profile_parameters(CORRECTED_PIPELINE_PROFILE)
        calibration = load_calibration_config(
            NOMINAL_CONFIG,
            runtime_metadata=runtime,
        )
        raw = np.empty((2500, 3), dtype=np.float32)
        raw[:, 0] = CORRECTED_ZERO_CODE
        raw[:, 1] = CORRECTED_ZERO_CODE
        raw[:, 2] = 0.0
        range_stats = offset_code_range_stats(raw, CORRECTED_ADCMAX)
        converted = convert_raw_to_mv(raw, calibration)

        with tempfile.TemporaryDirectory() as directory:
            paths = write_calibrated_frame(
                output_dir=Path(directory),
                stem="frame_000001",
                samples_mv=converted,
                sample_rate_hz=250,
                calibration=calibration,
                source_metadata=runtime,
                raw_range_stats=range_stats,
            )
            metadata = json.loads(paths.json_path.read_text(encoding="utf-8"))
            exported = np.load(paths.npy_path, allow_pickle=False)
            self.assertEqual(
                metadata["npy_sha256"],
                hashlib.sha256(paths.npy_path.read_bytes()).hexdigest(),
            )
            with self.assertRaises(CalibrationError):
                write_calibrated_frame(
                    output_dir=Path(directory),
                    stem="frame_000001",
                    samples_mv=converted,
                    sample_rate_hz=250,
                    calibration=calibration,
                    source_metadata=runtime,
                    raw_range_stats=range_stats,
                )

        formula = metadata["calibration"]["conversion_formula"]
        self.assertEqual(formula["formula_id"], ADS1293_CONVERSION_FORMULA_ID)
        self.assertEqual(formula["adcmax"], CORRECTED_ADCMAX)
        self.assertEqual(formula["zero_code"], CORRECTED_ZERO_CODE)
        self.assertEqual(formula["vref_nominal_v"], 2.4)
        self.assertEqual(formula["analog_gain"], 3.5)
        self.assertTrue(metadata["calibration"]["compatibility"]["compatible"])
        self.assertIsNotNone(metadata["calibration"]["calibration_config_sha256"])
        self.assertEqual(exported.dtype, np.dtype("float32"))
        self.assertEqual(exported.shape, (2500, 3))


if __name__ == "__main__":
    unittest.main()
