from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np

from calibration_utils import CalibrationError, FrameSet, load_raw_frame
from ecg_buffer import ECGFrame
from metadata_store import MetadataStore
from window_exporter import WindowExporter


class ADS1293FrameMetadataTests(unittest.TestCase):
    def test_frame_references_verified_session_and_never_dummy_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = {
                "session_id": "session-test",
                "firmware_commit": "abc123",
                "protocol_version": 2,
                "resample_up": 15,
                "resample_down": 16,
                "adc_encoding": "offset_binary_uint24",
                "adcmax": 0xF30000,
                "zero_code": 0x798000,
                "vref_nominal_v": 2.4,
                "analog_gain": 3.5,
            }
            exporter = WindowExporter(
                root,
                "raw_ecg.csv",
                250,
                "ads1293_0x01",
                "stored",
                session_metadata=session,
            )
            samples = np.empty((2500, 3), dtype=np.float32)
            samples[:, 0] = 0x798000
            samples[:, 1] = 0x798000
            samples[:, 2] = 0
            provenance = ECGFrame(
                samples=samples,
                sequences=np.asarray([100, 2766], dtype=np.uint32),
                timestamp_us=np.asarray([1000, 10_001_000], dtype=np.uint32),
            )
            paths = exporter.export_frame(
                1,
                provenance,
                samples,
                {
                    "native_samples_consumed": 2667,
                    "output_samples_produced": 2500,
                    "acquisition_status": "PASS",
                    "calibration_status": "preliminary",
                    "signal_quality_status": "PASS",
                },
            )
            exporter.close()

            metadata = json.loads(paths.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["session_id"], "session-test")
            self.assertEqual(metadata["firmware_commit"], "abc123")
            self.assertEqual(metadata["protocol_version"], 2)
            self.assertEqual(metadata["native_sequence_start"], 100)
            self.assertEqual(metadata["native_sequence_end"], 2766)
            self.assertEqual(metadata["native_samples_consumed"], 2667)
            self.assertEqual(metadata["unit"], "ADC_count")
            self.assertEqual(
                metadata["sample_domain"], "resampled_offset_code_counts"
            )
            self.assertIn("signed derived", metadata["channel_encoding"]["ch3"])
            self.assertEqual(metadata["vref_nominal_v"], 2.4)
            self.assertEqual(metadata["analog_gain"], 3.5)
            self.assertEqual(
                metadata["bin_sha256"],
                hashlib.sha256(paths.bin_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                metadata["npy_sha256"],
                hashlib.sha256(paths.npy_path.read_bytes()).hexdigest(),
            )
            self.assertNotIn("esp32dev_dummy", json.dumps(metadata))

            store = MetadataStore(root / "metadata.sqlite")
            store.insert_frame(metadata, paths.metadata_path.name)
            loaded = store.get_latest_metadata()
            store.close()
            self.assertEqual(loaded["session_id"], "session-test")
            self.assertEqual(loaded["signal_quality_status"], "PASS")

            frame_set = FrameSet(
                stem="frame_000001",
                csv_path=paths.csv_path,
                bin_path=paths.bin_path,
                npy_path=paths.npy_path,
                json_path=paths.metadata_path,
                frame_index=1,
                sort_time=0.0,
            )
            verified_samples, _ = load_raw_frame(frame_set)
            self.assertTrue(np.array_equal(verified_samples, samples))
            tampered = samples.copy()
            tampered[0, 0] += np.float32(1.0)
            tampered[0, 2] = tampered[0, 1] - tampered[0, 0]
            np.save(paths.npy_path, tampered)
            with self.assertRaises(CalibrationError):
                load_raw_frame(frame_set)

    def test_latest_metadata_uses_creation_time_not_restarted_frame_index(self) -> None:
        def metadata(measurement_id: str, frame_index: int, created_at: str) -> dict:
            return {
                "measurement_id": measurement_id,
                "device_id": "ads1293_0x01",
                "frame_index": frame_index,
                "state": "stored",
                "created_at_utc": created_at,
                "sample_rate_hz": 250,
                "sample_count": 2500,
                "duration_seconds": 10.0,
                "shape": [2500, 3],
                "dtype": "float32",
                "sha256_checksum": measurement_id,
                "channel_order": ["Lead I", "Lead II", "Lead III"],
                "lead_mapping": {},
                "ch3_derived": True,
                "sequence_start": 0,
                "sequence_end": 1,
                "timestamp_us_start": 0,
                "timestamp_us_end": 1,
                "csv_file": f"{measurement_id}.csv",
                "bin_file": f"{measurement_id}.bin",
                "npy_file": f"{measurement_id}.npy",
            }

        with tempfile.TemporaryDirectory() as directory:
            store = MetadataStore(Path(directory) / "metadata.sqlite")
            store.insert_frame(
                metadata("older-high-index", 10, "2026-01-01T00:00:00Z"),
                "older.json",
            )
            store.insert_frame(
                metadata("newer-low-index", 1, "2026-07-18T00:00:00Z"),
                "newer.json",
            )
            store.insert_frame(
                metadata("newest-row-tie", 0, "2026-07-18T00:00:00Z"),
                "newest-row-tie.json",
            )
            latest = store.get_latest_metadata()
            store.close()
        self.assertEqual(latest["measurement_id"], "newest-row-tie")


if __name__ == "__main__":
    unittest.main()
