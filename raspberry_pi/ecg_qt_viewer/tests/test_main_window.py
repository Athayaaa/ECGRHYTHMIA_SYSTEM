"""Headless integration tests for MainWindow state and file-readiness handling."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QObject, QSettings, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

from ecg_qt_viewer.frame_repository import FrameRecord, FrameRepository
from ecg_qt_viewer.frame_validator import ValidationStatus
from ecg_qt_viewer.frame_watcher import FrameWatcher
import ecg_qt_viewer.main_window as main_window_module


def _valid_samples(offset: float = 0.0) -> np.ndarray:
    lead_i = np.linspace(-0.45, 0.55, 2500, dtype=np.float32) + offset
    lead_ii = np.linspace(-0.20, 0.80, 2500, dtype=np.float32) + 2.0 * offset
    lead_iii = lead_ii - lead_i
    return np.column_stack((lead_i, lead_ii, lead_iii)).astype(
        np.float32, copy=False
    )


def _valid_metadata() -> dict[str, object]:
    return {
        "sample_rate_hz": 250,
        "unit": "mV",
        "channel_order": ["Lead I", "Lead II", "Lead III"],
        "sample_count": 2500,
        "shape": [2500, 3],
        "dtype": "float32",
        "duration_seconds": 10.0,
    }


def _write_frame(
    directory: Path,
    frame_id: int,
    *,
    samples: np.ndarray | None = None,
    metadata: bool = True,
) -> Path:
    path = directory / f"frame_{frame_id:06d}_mv.npy"
    np.save(
        path,
        _valid_samples() if samples is None else samples,
        allow_pickle=False,
    )
    if metadata:
        path.with_suffix(".json").write_text(
            json.dumps(_valid_metadata()), encoding="utf-8"
        )
    return path


class _DeterministicFrameWatcher(QObject):
    """Minimal watcher double whose ready signals are controlled by each test."""

    frames_changed = pyqtSignal(object)
    stable_frame_available = pyqtSignal(object)
    directory_unavailable = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self,
        repository_or_path: FrameRepository | str | Path,
        parent: QObject | None = None,
        **_kwargs: object,
    ) -> None:
        super().__init__(parent)
        self.data_dir = (
            repository_or_path.data_dir
            if isinstance(repository_or_path, FrameRepository)
            else Path(repository_or_path).resolve()
        )
        self._ready_paths: set[Path] = set()
        self.running = False

    def set_directory(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self._ready_paths.clear()

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def scan_now(self) -> tuple[FrameRecord, ...]:
        return ()

    def is_ready(self, record: FrameRecord) -> bool:
        return record.npy_path.resolve() in self._ready_paths

    def is_stable(self, record: FrameRecord) -> bool:
        return self.is_ready(record)

    def emit_ready(self, record: FrameRecord) -> None:
        # Match FrameWatcher.scan_now(): one version becomes ready immediately
        # before its synchronous stable_frame_available signal is emitted.
        self._ready_paths.add(record.npy_path.resolve())
        self.stable_frame_available.emit(record)


class MainWindowHeadlessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setOrganizationName("ECGRHYTHMIA-tests")
        cls.app.setApplicationName("ECG Qt Viewer tests")

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary_directory.name)
        self.settings_path = self.directory / "viewer-settings.ini"
        self.settings = QSettings(
            str(self.settings_path), QSettings.Format.IniFormat
        )
        self.settings.clear()
        self.window = None

    def tearDown(self) -> None:
        if self.window is not None:
            self.window.close()
            self.app.processEvents()
        self.settings.clear()
        self.settings.sync()
        self._temporary_directory.cleanup()

    def _construct_window(
        self, *, follow_live: bool = False
    ) -> main_window_module.MainWindow:
        settings_factory = mock.patch.object(
            main_window_module, "QSettings", return_value=self.settings
        )
        watcher_factory = mock.patch.object(
            main_window_module, "FrameWatcher", _DeterministicFrameWatcher
        )
        with settings_factory, watcher_factory:
            self.window = main_window_module.MainWindow(
                data_dir=self.directory,
                default_data_dir=self.directory,
                follow_live=follow_live,
            )
        return self.window

    def test_follow_live_coalesces_ready_burst_to_latest_frame(self) -> None:
        for frame_id in (1, 2, 3):
            _write_frame(self.directory, frame_id)
        window = self._construct_window(follow_live=True)
        watcher = window._watcher
        records = window._repository.frames()

        with mock.patch.object(
            window, "_select_record", wraps=window._select_record
        ) as select_record:
            for record in records:
                watcher.emit_ready(record)
            self.app.processEvents()

        self.assertEqual(select_record.call_count, 1)
        self.assertIsNotNone(window._current_record)
        self.assertEqual(window._current_record.frame_id, 3)
        self.assertTrue(window.current_frame_is_valid)

    def test_ready_frame_is_invalidated_immediately_by_unscanned_replacement(
        self,
    ) -> None:
        path = _write_frame(self.directory, 1)
        repository = FrameRepository(self.directory)
        repository.refresh()
        watcher = FrameWatcher(repository)
        self.addCleanup(watcher.stop)

        watcher.scan_now()
        ready = watcher.scan_now()
        self.assertEqual(len(ready), 1)
        record = repository.get_by_id(1)
        self.assertIsNotNone(record)
        self.assertTrue(watcher.is_ready(record))

        replacement_path = self.directory / "replacement.npy"
        np.save(replacement_path, _valid_samples(offset=0.2), allow_pickle=False)
        os.replace(replacement_path, path)

        # No watcher scan has occurred yet: is_ready must still fresh-stat the
        # path so manual navigation cannot open this unverified replacement.
        self.assertFalse(watcher.is_ready(record))

    def test_ready_npy_only_frame_waits_when_unscanned_json_appears(self) -> None:
        path = _write_frame(self.directory, 1, metadata=False)
        repository = FrameRepository(self.directory)
        repository.refresh()
        watcher = FrameWatcher(repository, metadata_grace_scans=1)
        self.addCleanup(watcher.stop)

        watcher.scan_now()
        ready = watcher.scan_now()
        self.assertEqual(len(ready), 1)
        record = repository.get_by_id(1)
        self.assertIsNotNone(record)
        self.assertIsNone(record.metadata_path)
        self.assertTrue(watcher.is_ready(record))

        path.with_suffix(".json").write_text("{partial", encoding="utf-8")

        # FrameValidator can discover this canonical neighbor, so navigation
        # must wait for a repository scan plus JSON stability first.
        self.assertFalse(watcher.is_ready(record))

    def test_malformed_qsettings_fall_back_without_crashing(self) -> None:
        malformed_values = {
            "display/paper_speed_mm_s": "not-a-number",
            "display/gain_mm_per_mv": "not-a-number",
            "display/time_window_s": float("nan"),
            "display/y_range_mv": -500,
            "display/lead_mode": "Lead V1",
            "display/grid_enabled": "not-a-bool",
            "display/crosshair_enabled": "not-a-bool",
            "display/calibration_factor": float("inf"),
            "display/line_width": "not-a-number",
            "replay/speed": 99,
            "window/geometry": "not-a-QByteArray",
            "window/state": "not-a-QByteArray",
        }
        for key, value in malformed_values.items():
            self.settings.setValue(key, value)
        self.settings.sync()

        window = self._construct_window()

        self.assertEqual(window.paper_speed_combo.currentData(), 25.0)
        self.assertEqual(window.gain_combo.currentData(), 10.0)
        self.assertEqual(window.time_window_combo.currentData(), 10.0)
        self.assertEqual(window.y_range_combo.currentData(), 2.0)
        self.assertEqual(window.lead_mode_combo.currentText(), "All leads")
        self.assertTrue(window.grid_checkbox.isChecked())
        self.assertTrue(window.crosshair_checkbox.isChecked())
        self.assertEqual(window.plot_widget.display_scale_factor, 1.0)
        self.assertEqual(window.replay_speed_combo.currentData(), 1.0)

    def test_current_frame_reloads_after_atomic_replacement(self) -> None:
        path = _write_frame(self.directory, 1)
        window = self._construct_window()
        record = window._repository.get_by_id(1)
        self.assertIsNotNone(record)
        window._watcher.emit_ready(record)
        self.app.processEvents()
        original = window.plot_widget.source_data.copy()

        replacement_path = self.directory / "replacement.npy"
        replacement = _valid_samples(offset=0.1)
        np.save(replacement_path, replacement, allow_pickle=False)
        os.replace(replacement_path, path)
        window._watcher.emit_ready(record)
        self.app.processEvents()

        np.testing.assert_array_equal(window.plot_widget.source_data, replacement)
        self.assertFalse(np.array_equal(original, window.plot_widget.source_data))

    def test_current_npy_only_frame_reloads_when_json_arrives(self) -> None:
        path = _write_frame(self.directory, 1, metadata=False)
        window = self._construct_window()
        initial_record = window._repository.get_by_id(1)
        self.assertIsNotNone(initial_record)
        window._watcher.emit_ready(initial_record)
        self.app.processEvents()
        self.assertEqual(
            window._current_validation.status, ValidationStatus.WARNING
        )

        path.with_suffix(".json").write_text(
            json.dumps(_valid_metadata()), encoding="utf-8"
        )
        refreshed_records = window._repository.refresh()
        window._watcher.frames_changed.emit(refreshed_records)
        refreshed_record = window._repository.get_by_id(1)
        self.assertIsNotNone(refreshed_record)
        window._watcher.emit_ready(refreshed_record)
        self.app.processEvents()

        self.assertEqual(window._current_validation.status, ValidationStatus.PASS)
        self.assertIsNotNone(window._current_record.metadata_path)

    def test_deleted_current_frame_falls_back_to_existing_stable_frame(self) -> None:
        for frame_id in (1, 2):
            _write_frame(self.directory, frame_id)
        window = self._construct_window()
        first, second = window._repository.frames()
        window._watcher.emit_ready(first)
        window._watcher.emit_ready(second)
        self.app.processEvents()
        window._select_record(second, user_requested=True, show_error=False)

        second.npy_path.unlink()
        second.metadata_path.unlink()
        records = window._repository.refresh()
        window._watcher.frames_changed.emit(records)
        self.app.processEvents()

        self.assertIsNotNone(window._current_record)
        self.assertEqual(window._current_record.frame_id, 1)
        self.assertTrue(window.current_frame_is_valid)

    def test_follow_live_recovers_to_latest_stable_frame_after_deletion(self) -> None:
        for frame_id in (1, 2):
            _write_frame(self.directory, frame_id)
        window = self._construct_window(follow_live=True)
        first, second = window._repository.frames()
        window._watcher.emit_ready(first)
        window._watcher.emit_ready(second)
        self.app.processEvents()
        self.assertEqual(window._current_record.frame_id, 2)

        second.npy_path.unlink()
        second.metadata_path.unlink()
        records = window._repository.refresh()
        window._watcher.frames_changed.emit(records)
        self.app.processEvents()

        self.assertTrue(window.follow_live_checkbox.isChecked())
        self.assertIsNotNone(window._current_record)
        self.assertEqual(window._current_record.frame_id, 1)
        self.assertTrue(window.current_frame_is_valid)

    def test_empty_directory_rearms_non_live_selection_when_frame_returns(
        self,
    ) -> None:
        path = _write_frame(self.directory, 1)
        window = self._construct_window()
        record = window._repository.get_by_id(1)
        window._watcher.emit_ready(record)
        self.app.processEvents()
        self.assertTrue(window.current_frame_is_valid)

        path.unlink()
        path.with_suffix(".json").unlink()
        window._watcher._ready_paths.clear()
        records = window._repository.refresh()
        window._watcher.frames_changed.emit(records)
        self.app.processEvents()
        self.assertIsNone(window._current_record)
        self.assertTrue(window._seeking_first_valid_frame)

        _write_frame(self.directory, 1)
        records = window._repository.refresh()
        window._watcher.frames_changed.emit(records)
        self.app.processEvents()
        self.assertIsNone(window._current_record)
        self.assertTrue(window._seeking_first_valid_frame)

        returned = window._repository.get_by_id(1)
        window._watcher.emit_ready(returned)
        self.app.processEvents()
        self.assertEqual(window._current_record.frame_id, 1)
        self.assertTrue(window.current_frame_is_valid)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions are required")
    def test_unreadable_directory_does_not_abort_window_construction(self) -> None:
        unreadable = self.directory / "unreadable"
        unreadable.mkdir()
        unreadable.chmod(0)
        try:
            try:
                list(unreadable.iterdir())
            except PermissionError:
                pass
            else:
                self.skipTest("current user can still read chmod-000 directory")

            settings_factory = mock.patch.object(
                main_window_module, "QSettings", return_value=self.settings
            )
            watcher_factory = mock.patch.object(
                main_window_module, "FrameWatcher", _DeterministicFrameWatcher
            )
            message_box = mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok
            )
            with settings_factory, watcher_factory, message_box:
                self.window = main_window_module.MainWindow(
                    data_dir=unreadable,
                    default_data_dir=self.directory,
                )

            self.assertIsNone(self.window._current_record)
            self.assertFalse(self.window.current_frame_is_valid)
        finally:
            unreadable.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
