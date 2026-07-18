"""Unit tests for Phase 3H replay and validation behavior."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import convert_dataset_to_mv
from ads1293_session import (
    ADCMAX,
    CONFIGURATION_PROFILE,
    FirmwareCompatibilityError,
    TARGET_STATIC_REGISTERS,
    build_session_metadata,
)
from calibration_utils import (
    CHANNEL_ORDER,
    CORRECTED_PIPELINE_PROFILE,
    CORRECTED_ZERO_CODE,
    CalibrationError,
    EXPECTED_SHAPE,
    pipeline_profile_parameters,
)
from replay_dataset import replay_dataset


class Phase3HPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_dir = self.root / "raw"
        self.output_dir = self.root / "replay"
        self.config_path = self.root / "calibration_config.yaml"
        self.write_calibration_config()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_calibration_config(self, scale: float = 1.0) -> None:
        self.config_path.write_text(
            "\n".join(
                [
                    "calibration_source: unit_test",
                    "method: synthetic_identity",
                    "unit: mV",
                    "scale_mV_per_count: " + str(scale),
                    "offset_count:",
                    "  lead_i: 6400000.0",
                    "  lead_ii: 6400000.0",
                    "  lead_iii: 0.0",
                    "channel_order:",
                    "  - Lead I",
                    "  - Lead II",
                    "  - Lead III",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def valid_prosim_like_frame(
        self,
        amplitude_mV: float = 1.0,
        bpm: float = 60.0,
        dtype=np.float32,
    ) -> np.ndarray:
        sample_rate_hz = 250.0
        sample_count = EXPECTED_SHAPE[0]
        t = np.arange(sample_count, dtype=np.float32) / sample_rate_hz
        lead_i = 0.08 * np.sin(2.0 * np.pi * 1.0 * t)
        lead_ii = 0.04 * np.sin(2.0 * np.pi * 1.0 * t)
        rr_samples = int(round(sample_rate_hz * 60.0 / bpm))
        indexes = np.arange(sample_count, dtype=np.float32)

        for peak in range(rr_samples // 2, sample_count, rr_samples):
            lead_ii += amplitude_mV * np.exp(
                -0.5 * ((indexes - float(peak)) / 2.5) ** 2
            )

        lead_iii = lead_ii - lead_i
        # Legacy ADS1293 Lead I/II files are unsigned offset-code, while Lead III
        # remains the signed derived difference.
        lead_i += 6_400_000.0
        lead_ii += 6_400_000.0
        return np.stack((lead_i, lead_ii, lead_iii), axis=1).astype(dtype)

    def write_raw_frame(
        self,
        samples: np.ndarray,
        index: int = 1,
        corrupt_metadata: bool = False,
    ) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        stem = f"frame_{index:06d}"
        np.save(self.input_dir / f"{stem}.npy", samples)
        (self.input_dir / f"{stem}.csv").write_text(
            "time,ch1,ch2,ch3\n",
            encoding="utf-8",
        )
        (self.input_dir / f"{stem}.bin").write_bytes(b"unit-test")

        metadata_path = self.input_dir / f"{stem}.json"
        if corrupt_metadata:
            metadata_path.write_text("{not-json", encoding="utf-8")
            return

        metadata = {
            "created_at_utc": f"2026-07-15T00:00:{index:02d}Z",
            "sample_rate_hz": 250,
            "channel_order": CHANNEL_ORDER,
            "sequence_start": (index - 1) * EXPECTED_SHAPE[0],
            "sequence_end": index * EXPECTED_SHAPE[0] - 1,
        }
        metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")

    def replay(self):
        return replay_dataset(
            input_dir=self.input_dir,
            config_path=self.config_path,
            output_dir=self.output_dir,
            realtime=False,
        )

    def test_valid_prosim_like_frame(self) -> None:
        self.write_raw_frame(self.valid_prosim_like_frame())

        summary = self.replay()

        self.assertEqual(summary.frames_converted, 1)
        self.assertEqual(summary.model_ready_frames, 1)
        self.assertEqual(summary.failed_frames, 0)
        metadata = json.loads(
            Path(summary.frame_results[0].model_ready_json_path).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(metadata["signal_quality"]["status"], "PASS")
        output = np.load(summary.frame_results[0].model_ready_npy_path)
        self.assertEqual(output.shape, EXPECTED_SHAPE)
        self.assertEqual(output.dtype, np.dtype("float32"))

    def test_wrong_shape_is_rejected(self) -> None:
        self.write_raw_frame(np.zeros((100, 3), dtype=np.float32))

        summary = self.replay()

        self.assertEqual(summary.frames_converted, 0)
        self.assertEqual(summary.model_ready_frames, 0)
        self.assertEqual(summary.failed_frames, 1)
        self.assertTrue(Path(summary.frame_results[0].rejected_metadata_path).exists())

    def test_nan_is_rejected(self) -> None:
        samples = self.valid_prosim_like_frame()
        samples[10, 0] = np.nan
        self.write_raw_frame(samples)

        summary = self.replay()

        self.assertEqual(summary.failed_frames, 1)
        self.assertIn("NaN/Inf", summary.frame_results[0].error)

    def test_inf_is_rejected(self) -> None:
        samples = self.valid_prosim_like_frame()
        samples[10, 0] = np.inf
        self.write_raw_frame(samples)

        summary = self.replay()

        self.assertEqual(summary.failed_frames, 1)
        self.assertIn("NaN/Inf", summary.frame_results[0].error)

    def test_lead_iii_inconsistency_is_rejected(self) -> None:
        samples = self.valid_prosim_like_frame()
        samples[100, 2] += 2.0
        self.write_raw_frame(samples)

        summary = self.replay()

        self.assertEqual(summary.failed_frames, 1)
        self.assertIn("ch3 consistency", summary.frame_results[0].error)

    def test_excessive_amplitude_is_rejected_from_model_ready(self) -> None:
        self.write_raw_frame(self.valid_prosim_like_frame(amplitude_mV=20.0))

        summary = self.replay()

        self.assertEqual(summary.frames_converted, 1)
        self.assertEqual(summary.model_ready_frames, 0)
        self.assertEqual(summary.failed_frames, 1)
        result = summary.frame_results[0]
        self.assertEqual(result.signal_quality["status"], "FAIL")
        self.assertFalse(list((self.output_dir / "model_ready").glob("*.npy")))

    def test_missing_calibration_config(self) -> None:
        self.write_raw_frame(self.valid_prosim_like_frame())

        with self.assertRaises(CalibrationError):
            replay_dataset(
                input_dir=self.input_dir,
                config_path=self.root / "missing_calibration.yaml",
                output_dir=self.output_dir,
            )

    def test_corrupt_metadata_is_rejected(self) -> None:
        self.write_raw_frame(self.valid_prosim_like_frame(), corrupt_metadata=True)

        summary = self.replay()

        self.assertEqual(summary.frames_converted, 0)
        self.assertEqual(summary.model_ready_frames, 0)
        self.assertEqual(summary.failed_frames, 1)
        self.assertTrue(Path(summary.frame_results[0].rejected_metadata_path).exists())

    def test_synthetic_1850_mv_frame_is_rejected(self) -> None:
        self.write_raw_frame(self.valid_prosim_like_frame(amplitude_mV=1850.0))

        summary = self.replay()

        self.assertEqual(summary.frames_converted, 1)
        self.assertEqual(summary.model_ready_frames, 0)
        result = summary.frame_results[0]
        reasons = " ".join(result.signal_quality["reasons"])
        self.assertEqual(result.signal_quality["status"], "FAIL")
        self.assertIn("exceeds 10", reasons)
        self.assertIn("1850", reasons)

    def test_successful_end_to_end_replay(self) -> None:
        self.write_raw_frame(self.valid_prosim_like_frame(), index=1)
        self.write_raw_frame(self.valid_prosim_like_frame(amplitude_mV=0.8), index=2)

        summary = self.replay()

        self.assertEqual(summary.total_raw_frames, 2)
        self.assertEqual(summary.frames_converted, 2)
        self.assertEqual(summary.model_ready_frames, 2)
        self.assertEqual(summary.failed_frames, 0)
        self.assertEqual(len(list((self.output_dir / "calibrated").glob("*.csv"))), 2)
        self.assertEqual(len(list((self.output_dir / "calibrated").glob("*.npy"))), 2)
        self.assertEqual(len(list((self.output_dir / "calibrated").glob("*.json"))), 2)
        self.assertEqual(len(list((self.output_dir / "model_ready").glob("*.npy"))), 2)
        self.assertTrue((self.output_dir / "replay_summary.json").exists())


class CorrectedReplayProvenanceTests(unittest.TestCase):
    SESSION_ID = "session-corrected-001"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_dir = self.root / "corrected_raw"
        self.output_dir = self.root / "corrected_replay"
        self.nominal_config = (
            Path(__file__).resolve().parent
            / "calibration_config_ads1293_266p667_nominal.yaml"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_corrected_frame(self, *, acquisition: str, signal: str) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        stem = "frame_000001"
        samples = np.empty(EXPECTED_SHAPE, dtype=np.float32)
        samples[:, 0] = CORRECTED_ZERO_CODE
        samples[:, 1] = CORRECTED_ZERO_CODE
        samples[:, 2] = 0.0
        npy_path = self.input_dir / f"{stem}.npy"
        bin_path = self.input_dir / f"{stem}.bin"
        np.save(npy_path, samples)
        (self.input_dir / f"{stem}.csv").write_text(
            "time,ch1,ch2,ch3\n",
            encoding="utf-8",
        )
        bin_path.write_bytes(
            np.ascontiguousarray(samples, dtype="<f4").tobytes(order="C")
        )
        metadata = {
            **pipeline_profile_parameters(CORRECTED_PIPELINE_PROFILE),
            "session_id": self.SESSION_ID,
            "protocol_version": 2,
            "firmware_commit": "0123456789abcdef",
            "created_at_utc": "2026-07-18T00:00:00Z",
            "sample_rate_hz": 250,
            "channel_order": CHANNEL_ORDER,
            "sequence_start": 0,
            "sequence_end": 2666,
            "acquisition_status": acquisition,
            "acquisition_reasons": (
                [] if acquisition == "PASS" else ["synthetic_acquisition_failure"]
            ),
            "packet_loss_count_frame": 0,
            "sequence_gap_events_frame": 0,
            "resampler_reset_count_frame": 0,
            "signal_quality_status": signal,
            "signal_quality": {
                "status": signal,
                "reasons": (
                    [] if signal == "PASS" else ["synthetic near-rail warning"]
                ),
                "below_range_count": 0,
                "above_range_count": 0,
                "near_low_rail_count": 1 if signal == "WARNING" else 0,
                "near_high_rail_count": 0,
                "out_of_range_count": 0,
                "near_rail_count": 1 if signal == "WARNING" else 0,
                "native_below_range_count": 0,
                "native_above_range_count": 0,
                "native_near_low_rail_count": (
                    1 if signal == "WARNING" else 0
                ),
                "native_near_high_rail_count": 0,
                "measured_leads": {},
                "derived_lead_iii": {},
            },
            "bin_sha256": hashlib.sha256(bin_path.read_bytes()).hexdigest(),
            "npy_sha256": hashlib.sha256(npy_path.read_bytes()).hexdigest(),
        }
        (self.input_dir / f"{stem}.json").write_text(
            json.dumps(metadata) + "\n",
            encoding="utf-8",
        )

    def replay(self):
        return replay_dataset(
            input_dir=self.input_dir,
            config_path=self.nominal_config,
            output_dir=self.output_dir,
            session_metadata=self.session_metadata(self.SESSION_ID),
        )

    def session_metadata(self, session_id: str) -> dict:
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
            "firmware_commit": "0123456789abcdef",
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
            baudrate=115200,
            calibration_config_path=self.nominal_config,
            calibration_compatible=True,
            calibration_status="preliminary",
            calibration_source="datasheet_nominal",
        )
        metadata["session_id"] = session_id
        metadata["acquisition_status"] = "PASS"
        metadata["acquisition_reasons"] = []
        return metadata

    def write_session_file(self, session_id: str | None = None) -> Path:
        path = self.root / f"session_{session_id or self.SESSION_ID}.json"
        path.write_text(
            json.dumps(self.session_metadata(session_id or self.SESSION_ID)) + "\n",
            encoding="utf-8",
        )
        return path

    def test_failed_acquisition_is_never_converted_or_model_ready(self) -> None:
        self.write_corrected_frame(acquisition="FAIL", signal="PASS")
        summary = self.replay()
        self.assertEqual(summary.frames_converted, 0)
        self.assertEqual(summary.model_ready_frames, 0)
        self.assertEqual(summary.failed_frames, 1)
        self.assertIn("acquisition_status", summary.frame_results[0].error)

    def test_corrected_converter_rejects_failed_acquisition(self) -> None:
        self.write_corrected_frame(acquisition="FAIL", signal="PASS")
        converted_dir = self.root / "converted"
        session_path = self.write_session_file()
        with patch(
            "sys.argv",
            [
                "convert_dataset_to_mv.py",
                "--input-dir",
                str(self.input_dir),
                "--config",
                str(self.nominal_config),
                "--session-metadata",
                str(session_path),
                "--output-dir",
                str(converted_dir),
            ],
        ):
            return_code = convert_dataset_to_mv.main()
        self.assertEqual(return_code, 1)
        self.assertFalse(list(converted_dir.glob("*_mv.npy")))

    def test_warning_frame_is_calibrated_but_not_model_ready(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="WARNING")
        summary = self.replay()
        self.assertEqual(summary.frames_converted, 1)
        self.assertEqual(summary.model_ready_frames, 0)
        self.assertEqual(summary.failed_frames, 0)
        self.assertEqual(summary.frame_results[0].status, "WARNING")

    def test_pass_frame_reaches_model_ready(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        summary = self.replay()
        self.assertEqual(summary.frames_converted, 1)
        self.assertEqual(summary.model_ready_frames, 1)
        self.assertEqual(summary.failed_frames, 0)

    def test_replay_rejects_unrelated_external_session(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        summary = replay_dataset(
            input_dir=self.input_dir,
            config_path=self.nominal_config,
            output_dir=self.output_dir,
            session_metadata=self.session_metadata("session-unrelated-999"),
        )
        self.assertEqual(summary.frames_converted, 0)
        self.assertEqual(summary.model_ready_frames, 0)
        self.assertEqual(summary.failed_frames, 1)
        self.assertIn("provenance mismatch", summary.frame_results[0].error)

    def test_replay_rejects_frame_fingerprint_mismatching_linked_session(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        metadata_path = self.input_dir / "frame_000001.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["r2"] = 5
        metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")

        summary = self.replay()
        self.assertEqual(summary.frames_converted, 0)
        self.assertEqual(summary.model_ready_frames, 0)
        self.assertEqual(summary.failed_frames, 1)
        self.assertIn("fingerprint mismatch r2", summary.frame_results[0].error)

    def test_converter_rejects_unrelated_external_session(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        converted_dir = self.root / "converted-with-session"
        session_path = self.root / "unrelated_session.json"
        session_path.write_text(
            json.dumps(self.session_metadata("session-unrelated-999")) + "\n",
            encoding="utf-8",
        )
        with patch(
            "sys.argv",
            [
                "convert_dataset_to_mv.py",
                "--input-dir",
                str(self.input_dir),
                "--config",
                str(self.nominal_config),
                "--session-metadata",
                str(session_path),
                "--output-dir",
                str(converted_dir),
            ],
        ):
            return_code = convert_dataset_to_mv.main()
        self.assertEqual(return_code, 1)
        self.assertFalse(list(converted_dir.glob("*_mv.npy")))

    def test_corrected_replay_requires_external_session(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        with self.assertRaises(CalibrationError):
            replay_dataset(
                input_dir=self.input_dir,
                config_path=self.nominal_config,
                output_dir=self.output_dir,
            )
        self.assertFalse(self.output_dir.exists())

    def test_corrected_converter_requires_external_session(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        converted_dir = self.root / "converted-no-session"
        with patch(
            "sys.argv",
            [
                "convert_dataset_to_mv.py",
                "--input-dir",
                str(self.input_dir),
                "--config",
                str(self.nominal_config),
                "--output-dir",
                str(converted_dir),
            ],
        ):
            return_code = convert_dataset_to_mv.main()
        self.assertEqual(return_code, 1)
        self.assertFalse(converted_dir.exists())

    def test_corrected_converter_accepts_full_verified_linked_session(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        converted_dir = self.root / "converted-with-verified-session"
        session_path = self.write_session_file()
        with patch(
            "sys.argv",
            [
                "convert_dataset_to_mv.py",
                "--input-dir",
                str(self.input_dir),
                "--config",
                str(self.nominal_config),
                "--session-metadata",
                str(session_path),
                "--output-dir",
                str(converted_dir),
            ],
        ):
            return_code = convert_dataset_to_mv.main()
        self.assertEqual(return_code, 0)
        self.assertTrue((converted_dir / "frame_000001_mv.npy").exists())

    def test_corrected_replay_rejects_incomplete_register_session(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        session = self.session_metadata(self.SESSION_ID)
        session["registers"] = dict(session["registers"])
        session["registers"].pop("0x29")
        with self.assertRaises(FirmwareCompatibilityError):
            replay_dataset(
                input_dir=self.input_dir,
                config_path=self.nominal_config,
                output_dir=self.output_dir,
                session_metadata=session,
            )
        self.assertFalse(self.output_dir.exists())

    def test_existing_replay_output_is_refused_without_modification(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        self.output_dir.mkdir(parents=True)
        sentinel = self.output_dir / "prior_result.txt"
        sentinel.write_text("do-not-replace\n", encoding="utf-8")
        with self.assertRaises(CalibrationError):
            self.replay()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-replace\n")

    def test_existing_converter_output_is_refused_without_modification(self) -> None:
        self.write_corrected_frame(acquisition="PASS", signal="PASS")
        converted_dir = self.root / "converted-existing"
        converted_dir.mkdir()
        sentinel = converted_dir / "prior_result.txt"
        sentinel.write_text("do-not-replace\n", encoding="utf-8")
        session_path = self.write_session_file()
        with patch(
            "sys.argv",
            [
                "convert_dataset_to_mv.py",
                "--input-dir",
                str(self.input_dir),
                "--config",
                str(self.nominal_config),
                "--session-metadata",
                str(session_path),
                "--output-dir",
                str(converted_dir),
            ],
        ):
            return_code = convert_dataset_to_mv.main()
        self.assertEqual(return_code, 1)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-replace\n")


if __name__ == "__main__":
    unittest.main()
