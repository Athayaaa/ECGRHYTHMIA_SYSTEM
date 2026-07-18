"""Strict session and raw-integrity tests for the ProSim tools."""

from __future__ import annotations

from types import SimpleNamespace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import auto_calibrate_prosim
import validate_prosim_calibration
from ads1293_session import (
    ADCMAX,
    CONFIGURATION_PROFILE,
    FirmwareCompatibilityError,
    TARGET_STATIC_REGISTERS,
    build_session_metadata,
)
from calibration_utils import (
    CORRECTED_PIPELINE_PROFILE,
    EXPECTED_SHAPE,
    LEGACY_PIPELINE_PROFILE,
    CalibrationError,
    load_calibration_config,
    pipeline_profile_parameters,
)
from validate_prosim_calibration import load_validation_samples


class ProSimSessionProvenanceTests(unittest.TestCase):
    SESSION_ID = "session-prosim-corrected-001"
    FIRMWARE_COMMIT = "0123456789abcdef"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_dir = self.root / "raw"
        self.nominal_config_path = (
            Path(__file__).resolve().parent
            / "calibration_config_ads1293_266p667_nominal.yaml"
        )
        self.corrected_calibration = load_calibration_config(
            self.nominal_config_path,
            allow_legacy=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def full_session(self, session_id: str | None = None) -> dict:
        firmware_config = {
            "config_verified": True,
            "device_id": 1,
            "protocol_version": 2,
            "configuration_profile": CONFIGURATION_PROFILE,
            "selftest_mode": "off",
            "afe_res_hz": 102400,
            "r1": 4,
            "r2": 6,
            "r3_ch1": 16,
            "r3_ch2": 16,
            "native_sample_rate_numerator": 800,
            "native_sample_rate_denominator": 3,
            "native_sample_rate_hz": 800 / 3,
            "adcmax": ADCMAX,
            "zero_code": ADCMAX // 2,
            "adc_encoding": "offset_binary_uint24",
            "packet_size": 32,
            "crc_type": "CRC16-CCITT-FALSE",
            "vref_nominal_v": 2.4,
            "analog_gain": 3.5,
            "firmware_commit": self.FIRMWARE_COMMIT,
            "firmware_dirty": False,
            "firmware_build_time_utc": "2026-07-18T00:00:00Z",
            "firmware_build_environment": "esp32dev_ads1293_usb",
            "registers": {
                f"0x{address:02X}": {
                    "address": address,
                    "written": value,
                    "readback": value,
                    "match": True,
                }
                for address, value in TARGET_STATIC_REGISTERS.items()
            },
        }
        metadata = build_session_metadata(
            firmware_config=firmware_config,
            serial_port="/dev/ttyUSB-test",
            baudrate=921600,
            calibration_config_path=self.nominal_config_path,
            calibration_compatible=True,
            calibration_status="preliminary",
            calibration_source="datasheet_nominal",
        )
        metadata["session_id"] = session_id or self.SESSION_ID
        metadata["acquisition_status"] = "PASS"
        metadata["acquisition_reasons"] = []
        return metadata

    def corrected_samples(self) -> np.ndarray:
        count = EXPECTED_SHAPE[0]
        phase = np.arange(count, dtype=np.float64) * (2.0 * np.pi / 250.0)
        lead_i = ADCMAX // 2 + 600.0 * np.sin(phase)
        lead_ii = ADCMAX // 2 + 1200.0 * np.sin(phase)
        lead_iii = lead_ii - lead_i
        return np.stack((lead_i, lead_ii, lead_iii), axis=1).astype(np.float32)

    def write_frame(
        self,
        *,
        profile: str = CORRECTED_PIPELINE_PROFILE,
        session_id: str | None = None,
        firmware_commit: str | None = None,
    ) -> Path:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        stem = "frame_000001"
        npy_path = self.input_dir / f"{stem}.npy"
        bin_path = self.input_dir / f"{stem}.bin"
        samples = self.corrected_samples()
        np.save(npy_path, samples)
        bin_path.write_bytes(np.ascontiguousarray(samples, dtype="<f4").tobytes())
        (self.input_dir / f"{stem}.csv").write_text(
            "time,ch1,ch2,ch3\n",
            encoding="utf-8",
        )

        metadata = pipeline_profile_parameters(profile)
        metadata.update(
            {
                "created_at_utc": "2026-07-18T00:00:00Z",
                "sample_rate_hz": 250,
                "sequence_start": 1,
                "sequence_end": 2500,
            }
        )
        if profile == CORRECTED_PIPELINE_PROFILE:
            metadata.update(
                {
                    "protocol_version": 2,
                    "session_id": session_id or self.SESSION_ID,
                    "firmware_commit": firmware_commit or self.FIRMWARE_COMMIT,
                    "acquisition_status": "PASS",
                    "acquisition_reasons": [],
                    "signal_quality_status": "PASS",
                    "packet_loss_count_frame": 0,
                    "sequence_gap_events_frame": 0,
                    "resampler_reset_count_frame": 0,
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
                    "npy_sha256": hashlib.sha256(npy_path.read_bytes()).hexdigest(),
                    "bin_sha256": hashlib.sha256(bin_path.read_bytes()).hexdigest(),
                }
            )

        (self.input_dir / f"{stem}.json").write_text(
            json.dumps(metadata) + "\n",
            encoding="utf-8",
        )
        return npy_path

    @staticmethod
    def robust_args(profile: str) -> SimpleNamespace:
        return SimpleNamespace(
            method="robust_pp",
            pipeline_profile=profile,
            expected_mv=1.0,
            expected_bpm=None,
            frames=1,
            lead="lead_ii",
            lower_percentile=1.0,
            upper_percentile=99.0,
            min_amplitude_count=100.0,
            warn_amplitude_count=1000.0,
            point=[],
        )

    def test_corrected_auto_calibration_requires_external_session(self) -> None:
        self.write_frame()
        with self.assertRaisesRegex(CalibrationError, "session-metadata"):
            auto_calibrate_prosim.build_calibration(
                self.robust_args(CORRECTED_PIPELINE_PROFILE),
                self.input_dir,
            )

    def test_corrected_validation_requires_external_session(self) -> None:
        self.write_frame()
        with self.assertRaisesRegex(CalibrationError, "session-metadata"):
            load_validation_samples(self.input_dir, self.corrected_calibration)

    def test_unrelated_session_is_rejected_by_both_prosim_tools(self) -> None:
        self.write_frame()
        unrelated = self.full_session("session-unrelated-999")
        with self.assertRaisesRegex(CalibrationError, "provenance mismatch"):
            auto_calibrate_prosim.build_calibration(
                self.robust_args(CORRECTED_PIPELINE_PROFILE),
                self.input_dir,
                unrelated,
            )
        with self.assertRaisesRegex(CalibrationError, "provenance mismatch"):
            load_validation_samples(
                self.input_dir,
                self.corrected_calibration,
                unrelated,
            )

    def test_unverified_register_session_is_rejected_before_calibration(self) -> None:
        self.write_frame()
        invalid = self.full_session()
        invalid["registers"] = dict(invalid["registers"])
        invalid["registers"].pop("0x29")
        with self.assertRaises(FirmwareCompatibilityError):
            auto_calibrate_prosim.build_calibration(
                self.robust_args(CORRECTED_PIPELINE_PROFILE),
                self.input_dir,
                invalid,
            )
        with self.assertRaises(FirmwareCompatibilityError):
            load_validation_samples(
                self.input_dir,
                self.corrected_calibration,
                invalid,
            )

    def test_tampered_npy_is_rejected_by_both_prosim_tools(self) -> None:
        npy_path = self.write_frame()
        tampered = np.load(npy_path, allow_pickle=False)
        tampered[10, 0] += np.float32(1.0)
        np.save(npy_path, tampered)
        session = self.full_session()

        with self.assertRaisesRegex(CalibrationError, "SHA-256"):
            auto_calibrate_prosim.build_calibration(
                self.robust_args(CORRECTED_PIPELINE_PROFILE),
                self.input_dir,
                session,
            )
        with self.assertRaisesRegex(CalibrationError, "SHA-256"):
            load_validation_samples(
                self.input_dir,
                self.corrected_calibration,
                session,
            )

    def test_valid_full_session_flows_through_both_prosim_loaders(self) -> None:
        self.write_frame()
        session = self.full_session()
        result = auto_calibrate_prosim.build_calibration(
            self.robust_args(CORRECTED_PIPELINE_PROFILE),
            self.input_dir,
            session,
        )
        self.assertEqual(
            result.config["metadata"]["acquisition_session"]["session_id"],
            self.SESSION_ID,
        )
        self.assertEqual(
            result.config["metadata"]["acquisition_session"]["firmware_commit"],
            self.FIRMWARE_COMMIT,
        )

        samples, sample_rate_hz, frames = load_validation_samples(
            self.input_dir,
            self.corrected_calibration,
            session,
        )
        self.assertEqual(samples.shape, EXPECTED_SHAPE)
        self.assertEqual(sample_rate_hz, 250)
        self.assertEqual(frames, ["frame_000001"])

    def test_frame_firmware_commit_must_match_external_session(self) -> None:
        self.write_frame(firmware_commit="fedcba9876543210")
        session = self.full_session()
        with self.assertRaisesRegex(CalibrationError, "firmware_commit mismatch"):
            auto_calibrate_prosim.build_calibration(
                self.robust_args(CORRECTED_PIPELINE_PROFILE),
                self.input_dir,
                session,
            )

    def test_explicit_legacy_profile_remains_session_free(self) -> None:
        self.write_frame(profile=LEGACY_PIPELINE_PROFILE)
        result = auto_calibrate_prosim.build_calibration(
            self.robust_args(LEGACY_PIPELINE_PROFILE),
            self.input_dir,
        )
        self.assertEqual(result.config["pipeline_profile"], LEGACY_PIPELINE_PROFILE)

        legacy_calibration = {
            **pipeline_profile_parameters(LEGACY_PIPELINE_PROFILE),
            "calibration_source": "unit_test_legacy",
            "calibration_status": "legacy",
        }
        samples, sample_rate_hz, frames = load_validation_samples(
            self.input_dir,
            legacy_calibration,
        )
        self.assertEqual(samples.shape, EXPECTED_SHAPE)
        self.assertEqual(sample_rate_hz, 250)
        self.assertEqual(frames, ["frame_000001"])

    def test_corrected_auto_cli_rejects_missing_session_before_output(self) -> None:
        self.write_frame()
        output_path = self.root / "must-not-exist.yaml"
        with patch(
            "sys.argv",
            [
                "auto_calibrate_prosim.py",
                "--input-dir",
                str(self.input_dir),
                "--pipeline-profile",
                CORRECTED_PIPELINE_PROFILE,
                "--method",
                "robust_pp",
                "--output",
                str(output_path),
            ],
        ):
            self.assertEqual(auto_calibrate_prosim.main(), 1)
        self.assertFalse(output_path.exists())

    def test_corrected_validation_cli_rejects_missing_session(self) -> None:
        self.write_frame()
        with patch(
            "sys.argv",
            [
                "validate_prosim_calibration.py",
                "--input-dir",
                str(self.input_dir),
                "--config",
                str(self.nominal_config_path),
                "--expected-mv",
                "1.0",
                "--expected-bpm",
                "60",
            ],
        ):
            self.assertEqual(validate_prosim_calibration.main(), 1)


if __name__ == "__main__":
    unittest.main()
