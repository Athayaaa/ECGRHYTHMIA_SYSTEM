from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import session_manager


class SessionManagerTests(TestCase):
    def test_session_name_layout_and_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            paths = session_manager.create_recording_session(
                Path(td) / "sessions",
                now=datetime(2026, 7, 28, 15, 38, 45),
            )

            self.assertEqual(paths.session_id, "session_28072026_153845")
            self.assertEqual(
                {path.name for path in paths.root.iterdir()},
                {
                    "raw_adc",
                    "calibrated",
                    "filtered",
                    "model_ready",
                    "predictions",
                    "logs",
                    "session.json",
                },
            )
            metadata = json.loads(paths.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "RECORDING")
            self.assertEqual(metadata["frame_count"], 0)
            self.assertEqual(metadata["channel_order"], ["Lead I", "Lead II", "Lead III"])

    def test_session_collision_keeps_existing_session_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "sessions"
            now = datetime(2026, 7, 28, 15, 38, 45)
            first = session_manager.create_recording_session(root, now=now)
            marker = first.raw_adc / "frame_000001.json"
            marker.write_text("{}", encoding="utf-8")
            second = session_manager.create_recording_session(root, now=now)

            self.assertEqual(second.session_id, "session_28072026_153845_01")
            self.assertTrue(marker.exists())

    def test_finish_updates_session_without_deleting_data(self):
        with tempfile.TemporaryDirectory() as td:
            paths = session_manager.create_recording_session(Path(td) / "sessions")
            frame = paths.raw_adc / "frame_000001.npy"
            frame.write_bytes(b"frame")

            session_manager.finish_recording_session(
                paths,
                1,
                now=datetime(2026, 7, 28, 16, 0, 0),
            )

            metadata = json.loads(paths.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "STOPPED")
            self.assertEqual(metadata["frame_count"], 1)
            self.assertIsNotNone(metadata["ended_at"])
            self.assertTrue(frame.exists())

    def test_legacy_cleanup_does_not_touch_sessions_or_datasets(self):
        with tempfile.TemporaryDirectory() as td:
            data_root = Path(td) / "data"
            session_file = data_root / "sessions" / "session_old" / "raw_adc" / "frame_000001.npy"
            legacy_frame = data_root / "frame_000001.npy"
            prosim_frame = data_root / "calibrated_prosim_test" / "frame_000001_mv.npy"
            legacy_prediction = data_root / "predictions" / "frame_000001_prediction.json"
            legacy_wal = data_root / "ecg_metadata.sqlite-wal"
            for path in (session_file, legacy_frame, prosim_frame, legacy_prediction, legacy_wal):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("data", encoding="utf-8")

            removed = session_manager.clean_legacy_output(data_root)

            self.assertEqual(set(removed), {legacy_frame, legacy_prediction, legacy_wal})
            self.assertTrue(session_file.exists())
            self.assertTrue(prosim_frame.exists())

    def test_receiver_dry_run_creates_session_without_opening_serial(self):
        import receiver

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.yaml"
            config.write_text(
                "sampling:\n  sample_rate_hz: 250\n  frame_seconds: 10\n"
                "output:\n  directory: data\n"
                "runtime:\n  launch_ecg_viewer: false\n",
                encoding="utf-8",
            )

            result = receiver.main(
                [
                    "--config", str(config), "--new-session", "--sessions-root",
                    str(root / "data" / "sessions"), "--dry-run-session",
                ]
            )

            sessions = sorted((root / "data" / "sessions").glob("session_*"))
            self.assertEqual(result, 0)
            self.assertEqual(len(sessions), 1)
            metadata = json.loads((sessions[0] / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "STOPPED")
            self.assertTrue((sessions[0] / "raw_adc").is_dir())
            self.assertTrue((sessions[0] / "model_ready").is_dir())

    def test_receiver_does_not_start_when_session_root_cannot_be_created(self):
        import receiver

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.yaml"
            config.write_text("sampling: {}\noutput: {}\nruntime: {}\n", encoding="utf-8")
            blocked_root = root / "not-a-directory"
            blocked_root.write_text("blocked", encoding="utf-8")

            result = receiver.main(
                [
                    "--config", str(config), "--new-session", "--sessions-root",
                    str(blocked_root), "--dry-run-session",
                ]
            )

            self.assertEqual(result, 1)

    def test_receiver_can_clean_legacy_output_without_starting_serial(self):
        import receiver

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.yaml"
            config.write_text("sampling: {}\noutput: {}\nruntime: {}\n", encoding="utf-8")
            legacy_frame = root / "data" / "frame_000001.npy"
            saved_session = root / "data" / "sessions" / "session_old" / "raw_adc" / "frame_000001.npy"
            for path in (legacy_frame, saved_session):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("data", encoding="utf-8")

            result = receiver.main(
                ["--config", str(config), "--clean-legacy-output"]
            )

            self.assertEqual(result, 0)
            self.assertFalse(legacy_frame.exists())
            self.assertTrue(saved_session.exists())
