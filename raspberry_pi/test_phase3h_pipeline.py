"""Unit tests for Phase 3H replay and validation behavior."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from calibration_utils import CHANNEL_ORDER, CalibrationError, EXPECTED_SHAPE
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
                    "  lead_i: 0.0",
                    "  lead_ii: 0.0",
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


if __name__ == "__main__":
    unittest.main()
