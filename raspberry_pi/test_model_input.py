"""Unit tests for Phase 3G model input preparation."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np

from calibration_utils import (
    CHANNEL_ORDER,
    CORRECTED_PIPELINE_PROFILE,
    pipeline_profile_parameters,
)
from model_input_utils import (
    MODEL_INPUT_SHAPE,
    ModelInputError,
    load_model_metadata,
    prepare_model_input_file,
)


class ModelInputPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "model_ready"
        self.input_path = self.root / "frame_000001_mv.npy"
        self.metadata_path = self.root / "frame_000001_mv.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def valid_samples(self, dtype=np.float32) -> np.ndarray:
        time = np.arange(MODEL_INPUT_SHAPE[0], dtype=np.float32) / 250.0
        lead_i = 0.35 * np.sin(2.0 * np.pi * 1.0 * time)
        lead_ii = 0.70 * np.sin(2.0 * np.pi * 1.0 * time)
        lead_iii = lead_ii - lead_i
        return np.stack((lead_i, lead_ii, lead_iii), axis=1).astype(dtype)

    def write_metadata(self) -> None:
        fingerprint = pipeline_profile_parameters(CORRECTED_PIPELINE_PROFILE)
        npy_sha256 = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        metadata = {
            "unit": "mV",
            "sample_rate_hz": 250,
            "channel_order": CHANNEL_ORDER,
            "npy_sha256": npy_sha256,
            "raw_range_validation": {
                "status": "PASS",
                "out_of_range_count": 0,
                "near_rail_count": 0,
            },
            "source_metadata": {
                **fingerprint,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "protocol_version": 2,
                "firmware_commit": "0123456789abcdef",
                "npy_sha256": npy_sha256,
                "bin_sha256": "0" * 64,
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
            },
            "calibration": {
                "calibration_source": "datasheet_nominal",
                "calibration_status": "preliminary",
                "pipeline_profile": CORRECTED_PIPELINE_PROFILE,
                "method": "ads1293_datasheet_equation_13_inverse",
                "scale_mV_per_count": fingerprint[
                    "datasheet_nominal_scale_mV_per_count"
                ],
                "acquisition_fingerprint": fingerprint,
                "compatibility": {
                    "compatible": True,
                    "reasons": [],
                    "calibration_fingerprint": fingerprint,
                    "runtime_fingerprint": fingerprint,
                },
            },
        }
        self.metadata_path.write_text(
            json.dumps(metadata) + "\n",
            encoding="utf-8",
        )

    def write_samples(self, samples: np.ndarray) -> None:
        np.save(self.input_path, samples)
        self.write_metadata()

    def prepare(self, **kwargs):
        return prepare_model_input_file(
            input_path=self.input_path,
            metadata_path=self.metadata_path,
            output_dir=self.output_dir,
            overwrite=True,
            **kwargs,
        )

    def test_valid_frame(self) -> None:
        self.write_samples(self.valid_samples())

        prepared = self.prepare()

        output = np.load(prepared.paths.npy_path, allow_pickle=False)
        self.assertEqual(output.shape, MODEL_INPUT_SHAPE)
        self.assertEqual(output.dtype, np.dtype("float32"))
        self.assertEqual(prepared.validation.status, "PASS")
        self.assertEqual(prepared.metadata["acquisition_status"], "PASS")
        self.assertEqual(prepared.metadata["signal_quality_status"], "PASS")
        self.assertEqual(
            hashlib.sha256(prepared.paths.npy_path.read_bytes()).hexdigest(),
            prepared.metadata["npy_sha256"],
        )
        loaded_metadata = load_model_metadata(
            prepared.paths.metadata_path,
            input_path=prepared.paths.npy_path,
        )
        self.assertEqual(loaded_metadata["session_id"], prepared.metadata["session_id"])

        tampered_output = output.copy()
        tampered_output[0, 0] += np.float32(0.001)
        tampered_output[0, 2] = tampered_output[0, 1] - tampered_output[0, 0]
        np.save(prepared.paths.npy_path, tampered_output)
        with self.assertRaises(ModelInputError):
            load_model_metadata(
                prepared.paths.metadata_path,
                input_path=prepared.paths.npy_path,
            )

    def test_wrong_shape(self) -> None:
        self.write_samples(np.zeros((100, 3), dtype=np.float32))

        with self.assertRaises(ModelInputError):
            self.prepare()

    def test_nan_inf(self) -> None:
        samples = self.valid_samples()
        samples[10, 0] = np.nan
        self.write_samples(samples)

        with self.assertRaises(ModelInputError):
            self.prepare()

    def test_wrong_lead_iii(self) -> None:
        samples = self.valid_samples()
        samples[100, 2] += 1.0
        self.write_samples(samples)

        with self.assertRaises(ModelInputError):
            self.prepare()

    def test_excessive_amplitude(self) -> None:
        samples = self.valid_samples()
        samples[200, 1] = 20.0
        samples[200, 2] = samples[200, 1] - samples[200, 0]
        self.write_samples(samples)

        with self.assertRaises(ModelInputError):
            self.prepare(max_abs_mV=10.0)

    def test_missing_metadata(self) -> None:
        self.write_samples(self.valid_samples())
        self.metadata_path.unlink()

        with self.assertRaises(ModelInputError):
            self.prepare()

    def test_empty_metadata_is_rejected(self) -> None:
        self.write_samples(self.valid_samples())
        self.metadata_path.write_text("{}\n", encoding="utf-8")

        with self.assertRaises(ModelInputError):
            self.prepare()

    def test_calibrated_npy_hash_mismatch_is_rejected(self) -> None:
        self.write_samples(self.valid_samples())
        changed = self.valid_samples()
        changed[10, 0] += np.float32(0.001)
        changed[10, 2] = changed[10, 1] - changed[10, 0]
        np.save(self.input_path, changed)

        with self.assertRaises(ModelInputError):
            self.prepare()

    def test_corrected_frame_without_session_provenance_is_rejected(self) -> None:
        self.write_samples(self.valid_samples())
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        del metadata["source_metadata"]["session_id"]
        self.metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")

        with self.assertRaises(ModelInputError):
            self.prepare()

    def test_mismatched_calibration_fingerprint_is_rejected(self) -> None:
        self.write_samples(self.valid_samples())
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        metadata["calibration"]["acquisition_fingerprint"]["r2"] = 5
        self.metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")

        with self.assertRaises(ModelInputError):
            self.prepare()

    def test_dtype_conversion(self) -> None:
        self.write_samples(self.valid_samples(dtype=np.float64))

        prepared = self.prepare()
        output = np.load(prepared.paths.npy_path, allow_pickle=False)

        self.assertEqual(output.dtype, np.dtype("float32"))
        self.assertEqual(prepared.metadata["dtype"], "float32")


if __name__ == "__main__":
    unittest.main()
