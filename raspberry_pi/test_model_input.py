"""Unit tests for Phase 3G model input preparation."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from calibration_utils import CHANNEL_ORDER
from model_input_utils import (
    MODEL_INPUT_SHAPE,
    ModelInputError,
    prepare_model_input_file,
)


class ModelInputPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "model_ready"
        self.input_path = self.root / "frame_000001_mv.npy"
        self.metadata_path = self.root / "frame_000001_mv.json"
        self.write_metadata()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def valid_samples(self, dtype=np.float32) -> np.ndarray:
        time = np.arange(MODEL_INPUT_SHAPE[0], dtype=np.float32) / 250.0
        lead_i = 0.35 * np.sin(2.0 * np.pi * 1.0 * time)
        lead_ii = 0.70 * np.sin(2.0 * np.pi * 1.0 * time)
        lead_iii = lead_ii - lead_i
        return np.stack((lead_i, lead_ii, lead_iii), axis=1).astype(dtype)

    def write_metadata(self) -> None:
        metadata = {
            "unit": "mV",
            "sample_rate_hz": 250,
            "channel_order": CHANNEL_ORDER,
            "calibration": {
                "method": "rpeak_baseline",
                "scale_mV_per_count": 0.00011859582542694497,
            },
        }
        self.metadata_path.write_text(
            json.dumps(metadata) + "\n",
            encoding="utf-8",
        )

    def write_samples(self, samples: np.ndarray) -> None:
        np.save(self.input_path, samples)

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

    def test_dtype_conversion(self) -> None:
        self.write_samples(self.valid_samples(dtype=np.float64))

        prepared = self.prepare()
        output = np.load(prepared.paths.npy_path, allow_pickle=False)

        self.assertEqual(output.dtype, np.dtype("float32"))
        self.assertEqual(prepared.metadata["dtype"], "float32")


if __name__ == "__main__":
    unittest.main()
