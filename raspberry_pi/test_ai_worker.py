"""Unit tests for ai_worker.py using a mock ECGEngine."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase, mock

import numpy as np

# Ensure the raspberry_pi directory is importable
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ai_worker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_valid_frame(shape=(2500, 3)) -> np.ndarray:
    """Create a valid model-ready frame: Lead III = Lead II - Lead I."""
    rng = np.random.RandomState(42)
    frame = np.zeros(shape, dtype=np.float32)
    frame[:, 0] = rng.randn(shape[0]).astype(np.float32) * 0.3  # Lead I
    frame[:, 1] = rng.randn(shape[0]).astype(np.float32) * 0.4  # Lead II
    frame[:, 2] = frame[:, 1] - frame[:, 0]  # Lead III = II - I
    return frame


def make_metadata(status="PASS", unit="mV", sample_rate_hz=250) -> dict:
    """Create valid model-ready metadata."""
    return {
        "unit": unit,
        "sample_rate_hz": sample_rate_hz,
        "channel_order": ["Lead I", "Lead II", "Lead III"],
        "validation": {"status": status},
        "shape": [2500, 3],
        "dtype": "float32",
    }


def save_frame(directory: Path, frame_id: str, frame: np.ndarray,
               metadata: dict | None = None) -> Path:
    """Save a model-ready frame and optional metadata to directory."""
    directory.mkdir(parents=True, exist_ok=True)
    npy_path = directory / f"frame_{frame_id}_input.npy"
    np.save(npy_path, frame)
    if metadata is not None:
        json_path = directory / f"frame_{frame_id}_input.json"
        json_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return npy_path


def save_prediction(directory: Path, frame_id: str, data: dict) -> Path:
    """Save a prediction JSON file."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"frame_{frame_id}_prediction.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


MOCK_ENGINE_RESULT = {
    "prediction": "Normal",
    "confidence": 98.7,
    "probabilities": {"Normal": 98.7, "AF": 0.5, "Takikardia": 0.3, "Bradikardia": 0.5},
    "metadata": {
        "model_name": "test",
        "version": "1.0",
        "preprocessing_version": "test",
        "input_length": 2500,
        "sampling_rate": 250.0,
    },
}


class MockEngineContext:
    """Mock EngineContext that does not load TFLite."""

    def __init__(self):
        self._threshold = 0.5
        self._labels = ["Normal", "AF", "Takikardia", "Bradikardia"]
        self._model_path = "/mock/model.tflite"
        self._model_sha256 = "abc123"
        self._runtime = "mock"
        self._predict_result = dict(MOCK_ENGINE_RESULT)
        self._predict_exception = None

    def load(self):
        pass

    def predict(self, frame, sampling_rate):
        if self._predict_exception is not None:
            raise self._predict_exception
        return dict(self._predict_result)

    @property
    def model_path(self): return self._model_path
    @property
    def model_sha256(self): return self._model_sha256
    @property
    def runtime(self): return self._runtime
    @property
    def threshold(self): return self._threshold
    @property
    def labels(self): return list(self._labels)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestFrameDiscovery(TestCase):

    def test_frame_sorting_by_time(self):
        """Frames should be sorted by modification time."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for fid in [3, 1, 10, 2]:
                np.save(d / f"frame_{fid:06d}_input.npy", make_valid_frame())
                time.sleep(0.01) # ensure distinct mtime
            frames = ai_worker.discover_frames(d)
            ids = [fid for fid, _ in frames]
            self.assertEqual(ids, ["000003", "000001", "000010", "000002"])

    def test_hidden_and_temp_files_skipped(self):
        """Hidden and temporary files should be ignored."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            np.save(d / "frame_000001_input.npy", make_valid_frame())
            np.save(d / ".frame_000002_input.npy", make_valid_frame())
            (d / "frame_000003_input.npy.tmp").write_text("temp")
            frames = ai_worker.discover_frames(d)
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0][0], "000001")

    def test_calibrated_input_rejected_by_default(self):
        """Calibrated _mv.npy files should not be discovered by default."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            np.save(d / "frame_000001_mv.npy", make_valid_frame())
            frames = ai_worker.discover_frames(d, allow_calibrated=False)
            self.assertEqual(len(frames), 0)

    def test_calibrated_input_accepted_with_flag(self):
        """Calibrated _mv.npy files should be discovered with flag."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            np.save(d / "frame_000001_mv.npy", make_valid_frame())
            frames = ai_worker.discover_frames(d, allow_calibrated=True)
            self.assertEqual(len(frames), 1)


class TestStableFileDetection(TestCase):

    def test_stable_file_detected(self):
        """A file that does not change should be detected as stable."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.npy"
            np.save(p, make_valid_frame())
            self.assertTrue(ai_worker.is_file_stable(p, delay=0.05))

    def test_missing_file_not_stable(self):
        """A nonexistent file should not be stable."""
        self.assertFalse(
            ai_worker.is_file_stable(Path("/nonexistent"), delay=0.01)
        )


class TestInputValidation(TestCase):

    def test_valid_frame(self):
        """A valid (2500,3) float32 mV frame should pass validation."""
        frame = make_valid_frame()
        meta = make_metadata()
        is_valid, errors, warnings = ai_worker.validate_frame(frame, meta, 250.0)
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_wrong_shape(self):
        """A frame with wrong shape should be rejected."""
        frame = np.zeros((1000, 3), dtype=np.float32)
        is_valid, errors, _ = ai_worker.validate_frame(frame, None, 250.0)
        self.assertFalse(is_valid)
        self.assertTrue(any("shape" in e for e in errors))

    def test_nan_rejected(self):
        """A frame containing NaN should be rejected."""
        frame = make_valid_frame()
        frame[100, 0] = float("nan")
        is_valid, errors, _ = ai_worker.validate_frame(frame, None, 250.0)
        self.assertFalse(is_valid)
        self.assertTrue(any("NaN" in e for e in errors))

    def test_inf_rejected(self):
        """A frame containing Inf should be rejected."""
        frame = make_valid_frame()
        frame[200, 1] = float("inf")
        is_valid, errors, _ = ai_worker.validate_frame(frame, None, 250.0)
        self.assertFalse(is_valid)
        self.assertTrue(any("Inf" in e for e in errors))

    def test_lead_iii_mismatch(self):
        """A frame where Lead III != Lead II - Lead I should be rejected."""
        frame = make_valid_frame()
        frame[:, 2] = 99.0  # Break Lead III relationship
        meta = make_metadata()
        is_valid, errors, _ = ai_worker.validate_frame(frame, meta, 250.0)
        self.assertFalse(is_valid)
        self.assertTrue(any("Lead III" in e for e in errors))

    def test_acquisition_fail_rejected(self):
        """A frame with acquisition status FAIL should be rejected."""
        frame = make_valid_frame()
        meta = make_metadata(status="FAIL")
        is_valid, errors, _ = ai_worker.validate_frame(frame, meta, 250.0)
        self.assertFalse(is_valid)
        self.assertTrue(any("FAIL" in e for e in errors))

    def test_missing_metadata_warns(self):
        """A frame without metadata should produce warnings but still pass."""
        frame = make_valid_frame()
        is_valid, errors, warnings = ai_worker.validate_frame(frame, None, 250.0)
        self.assertTrue(is_valid)
        self.assertTrue(len(warnings) > 0)


class TestPredictionSkip(TestCase):

    def test_skip_existing_prediction(self):
        """Frames with valid prediction JSON (PASS or WARNING) should be skipped."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            save_prediction(d, "000001", {
                "schema_version": 1,
                "status": "PASS",
                "frame_id": "000001",
            })
            self.assertTrue(ai_worker.has_valid_prediction(d, "000001"))

            save_prediction(d, "000002", {
                "schema_version": 1,
                "status": "WARNING",
                "frame_id": "000002",
            })
            self.assertTrue(ai_worker.has_valid_prediction(d, "000002"))

    def test_retry_corrupt_prediction(self):
        """Corrupt prediction JSON should not count as valid."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            p = d / "frame_000001_prediction.json"
            d.mkdir(parents=True, exist_ok=True)
            p.write_text("{ corrupt json", encoding="utf-8")
            self.assertFalse(ai_worker.has_valid_prediction(d, "000001"))

    def test_wrong_schema_retried(self):
        """Prediction with wrong schema_version should be retried."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            save_prediction(d, "000001", {
                "schema_version": 999,
                "status": "PASS",
                "frame_id": "000001",
            })
            self.assertFalse(ai_worker.has_valid_prediction(d, "000001"))


class TestAtomicWrite(TestCase):

    def test_atomic_json_write(self):
        """Prediction JSON should be written atomically."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            data = {"schema_version": 1, "status": "PASS", "frame_id": "000001"}
            path = ai_worker.write_prediction_atomic(d, "test.json", data)
            self.assertTrue(path.exists())
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded["status"], "PASS")

    def test_no_temp_files_left(self):
        """No temporary files should remain after atomic write."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            ai_worker.write_prediction_atomic(d, "test.json", {"test": 1})
            files = list(d.iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "test.json")


class TestProcessFrame(TestCase):

    def test_prediction_pass(self):
        """Valid frame + mock engine should produce PASS prediction."""
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "output"
            frame = make_valid_frame()
            save_frame(input_dir, "000001", frame, make_metadata())

            engine = MockEngineContext()
            result = ai_worker.process_frame(
                "000001",
                input_dir / "frame_000001_input.npy",
                output_dir,
                engine,
                250.0,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["prediction"], "Normal")
            self.assertIn("confidence_percent", result)
            self.assertIn("latency_ms", result)
            self.assertEqual(result["schema_version"], 1)
            self.assertIsNone(result.get("input_validation_status"))
            self.assertIsNone(result.get("input_warnings"))

    def test_prediction_warning_for_large_baseline(self):
        """Frame with large baseline should produce a PASS prediction but contain WARNING metadata."""
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "output"
            
            # Baseline offset of -10 mV
            frame = make_valid_frame()
            frame[:, 0] -= 2.0
            frame[:, 1] -= 10.0
            frame[:, 2] = frame[:, 1] - frame[:, 0]
            save_frame(input_dir, "000002", frame, make_metadata())

            engine = MockEngineContext()
            result = ai_worker.process_frame(
                "000002",
                input_dir / "frame_000002_input.npy",
                output_dir,
                engine,
                250.0,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "PASS") # AI inference itself succeeded
            self.assertEqual(result["input_validation_status"], "WARNING")
            self.assertIsNotNone(result["input_warnings"])
            self.assertTrue(any("Large DC baseline" in w for w in result["input_warnings"]))

    def test_prediction_unclassified(self):
        """Below-threshold result should produce Unclassified prediction."""
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "output"
            frame = make_valid_frame()
            save_frame(input_dir, "000001", frame, make_metadata())

            engine = MockEngineContext()
            engine._predict_result = {
                "prediction": "Unclassified",
                "confidence": 30.0,
                "probabilities": {"Normal": 30.0, "AF": 25.0, "Takikardia": 25.0, "Bradikardia": 20.0},
                "metadata": {},
            }
            result = ai_worker.process_frame(
                "000001",
                input_dir / "frame_000001_input.npy",
                output_dir,
                engine,
                250.0,
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["prediction"], "Unclassified")

    def test_inference_exception_produces_fail_json(self):
        """Engine exception should produce FAIL JSON, not crash."""
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "output"
            frame = make_valid_frame()
            save_frame(input_dir, "000001", frame, make_metadata())

            engine = MockEngineContext()
            engine._predict_exception = RuntimeError("TFLite crash")
            result = ai_worker.process_frame(
                "000001",
                input_dir / "frame_000001_input.npy",
                output_dir,
                engine,
                250.0,
            )
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("TFLite crash", result["error"])

            # FAIL JSON should exist on disk
            fail_path = output_dir / "frame_000001_prediction.json"
            self.assertTrue(fail_path.exists())

    def test_latest_prediction_updated(self):
        """latest_prediction.json should be updated after each frame."""
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "output"
            frame = make_valid_frame()
            save_frame(input_dir, "000001", frame, make_metadata())

            engine = MockEngineContext()
            ai_worker.process_frame(
                "000001",
                input_dir / "frame_000001_input.npy",
                output_dir,
                engine,
                250.0,
            )
            latest = output_dir / "latest_prediction.json"
            self.assertTrue(latest.exists())
            data = json.loads(latest.read_text())
            self.assertEqual(data["frame_id"], "000001")

    def test_source_npy_unchanged(self):
        """The source NPY file should not be modified by the worker."""
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "output"
            frame = make_valid_frame()
            npy_path = save_frame(input_dir, "000001", frame, make_metadata())

            # Record original file content
            original_bytes = npy_path.read_bytes()

            engine = MockEngineContext()
            ai_worker.process_frame(
                "000001",
                npy_path,
                output_dir,
                engine,
                250.0,
            )

            # Source file must be unchanged
            self.assertEqual(npy_path.read_bytes(), original_bytes)


class TestGUIPredictionReading(TestCase):
    """Test that prediction reading logic matches frames correctly."""

    def test_gui_reads_prediction_for_frame(self):
        """Prediction JSON matched by frame_id should be accepted."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            pred_data = {
                "schema_version": 1,
                "status": "PASS",
                "frame_id": "000005",
                "prediction": "Normal",
                "confidence_percent": 95.0,
            }
            save_prediction(d, "000005", pred_data)

            # Simulate GUI reading
            path = d / "frame_000005_prediction.json"
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded["frame_id"], "000005")
            self.assertEqual(loaded["prediction"], "Normal")

    def test_gui_rejects_wrong_frame_prediction(self):
        """Prediction for a different frame should not be accepted."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            pred_data = {
                "schema_version": 1,
                "status": "PASS",
                "frame_id": "000010",
                "prediction": "AF",
            }
            save_prediction(d, "000010", pred_data)

            # Looking for frame 5 should find nothing
            path = d / "frame_000005_prediction.json"
            self.assertFalse(path.is_file())


class TestOLEDPredictionReading(TestCase):
    """Test OLED reads latest prediction correctly."""

    def test_oled_reads_latest(self):
        """OLED should read latest_prediction.json."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pred_dir = root / "predictions"
            pred_dir.mkdir()
            pred = {
                "schema_version": 1,
                "status": "PASS",
                "frame_id": "000005",
                "prediction": "Normal",
                "confidence_percent": 97.5,
            }
            (pred_dir / "latest_prediction.json").write_text(
                json.dumps(pred), encoding="utf-8"
            )
            # Import from oled_status
            sys.path.insert(0, str(SCRIPT_DIR))
            from oled_status import read_latest_prediction

            result = read_latest_prediction(root)
            self.assertIsNotNone(result)
            self.assertEqual(result["prediction"], "Normal")

    def test_oled_shows_wait_when_no_prediction(self):
        """OLED should show WAIT when no prediction file exists."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            from oled_status import read_latest_prediction

            result = read_latest_prediction(root)
            self.assertIsNone(result)


class TestWorkerSafety(TestCase):
    """Verify the worker does not import serial."""

    def test_worker_no_serial_import(self):
        """ai_worker.py source must not contain 'import serial'."""
        source = Path(SCRIPT_DIR / "ai_worker.py").read_text()
        self.assertNotIn("import serial", source)
        self.assertNotIn("from serial", source)
        self.assertNotIn("/dev/tty", source)


class TestRunWorkerOnce(TestCase):
    """Integration-level test: run_worker in --once mode with mock engine."""

    def test_run_once_processes_frames(self):
        """Worker in --once mode should process frames and exit."""
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "model_ready"
            output_dir = Path(td) / "predictions"

            # Create 3 frames
            for i in range(1, 4):
                frame = make_valid_frame()
                save_frame(input_dir, f"{i:06d}", frame, make_metadata())

            # Patch EngineContext
            mock_engine = MockEngineContext()
            with mock.patch.object(
                ai_worker, "EngineContext", return_value=mock_engine
            ):
                args = ai_worker.build_parser().parse_args([
                    "--input-dir", str(input_dir),
                    "--output-dir", str(output_dir),
                    "--once",
                ])
                args = ai_worker.resolve_args(args)
                result = ai_worker.run_worker(args)

            self.assertEqual(result, 0)

            # Check predictions were created
            for i in range(1, 4):
                pred = output_dir / f"frame_{i:06d}_prediction.json"
                self.assertTrue(pred.exists(), f"Missing prediction for frame {i}")
                data = json.loads(pred.read_text())
                self.assertEqual(data["status"], "PASS")

            # Check latest_prediction.json
            latest = output_dir / "latest_prediction.json"
            self.assertTrue(latest.exists())

    def test_run_once_skips_existing(self):
        """Worker should skip frames that already have valid predictions."""
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "model_ready"
            output_dir = Path(td) / "predictions"

            frame = make_valid_frame()
            save_frame(input_dir, "000001", frame, make_metadata())

            # Pre-create a valid prediction
            save_prediction(output_dir, "000001", {
                "schema_version": 1,
                "status": "PASS",
                "frame_id": "000001",
            })

            mock_engine = MockEngineContext()
            original_predict = mock_engine.predict
            call_count = [0]
            def counting_predict(frame, sr):
                call_count[0] += 1
                return original_predict(frame, sr)
            mock_engine.predict = counting_predict

            with mock.patch.object(
                ai_worker, "EngineContext", return_value=mock_engine
            ):
                args = ai_worker.build_parser().parse_args([
                    "--input-dir", str(input_dir),
                    "--output-dir", str(output_dir),
                    "--once",
                ])
                args = ai_worker.resolve_args(args)
                ai_worker.run_worker(args)

            # Engine.predict should NOT have been called
            self.assertEqual(call_count[0], 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
