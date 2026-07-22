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
from PyQt6.QtWidgets import QApplication, QMessageBox, QToolBar

from ecg_qt_viewer.ecg_plot_widget import ECGPlotWidget
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


def _synthetic_ecg(bpm: float, amplitude_mv: float = 1.0) -> np.ndarray:
    """Build a deterministic Lead-II pulse train for BPM display tests."""
    sample_rate = 250.0
    sample_count = 2500
    rr_samples = sample_rate * 60.0 / bpm
    indices = np.arange(sample_count, dtype=np.float64)
    lead_ii = np.zeros(sample_count, dtype=np.float64)
    center = 0.5 * rr_samples
    while center < sample_count:
        lead_ii += amplitude_mv * np.exp(-0.5 * ((indices - center) / 3.0) ** 2)
        center += rr_samples
    lead_i = 0.25 * lead_ii
    lead_iii = lead_ii - lead_i
    return np.column_stack((lead_i, lead_ii, lead_iii)).astype(np.float32)


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


def _write_raw_frame(directory: Path, frame_id: int) -> Path:
    phase = np.linspace(0.0, 8.0 * np.pi, 2500, dtype=np.float32)
    lead_i = np.rint(32768.0 + 120.0 * np.sin(phase))
    lead_ii = np.rint(32768.0 + 850.0 * np.sin(phase + 0.25))
    lead_iii = lead_ii - lead_i
    samples = np.column_stack((lead_i, lead_ii, lead_iii)).astype(np.int32)
    path = directory / f"frame_{frame_id:06d}.npy"
    np.save(path, samples, allow_pickle=False)
    metadata = {
        "sample_rate_hz": 250,
        "unit": "ADC count",
        "channel_order": ["Lead I", "Lead II", "Lead III"],
        "sample_count": 2500,
        "shape": [2500, 3],
        "dtype": "int32",
        "duration_seconds": 10.0,
        "zero_code": 32768,
        "ADCMAX": 65535,
    }
    path.with_suffix(".json").write_text(
        json.dumps(metadata), encoding="utf-8"
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
        self.repository = (
            repository_or_path
            if isinstance(repository_or_path, FrameRepository)
            else FrameRepository(repository_or_path)
        )
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

    def test_primary_controls_use_visible_three_row_panel_without_toolbar(self) -> None:
        window = self._construct_window()
        window.resize(1366, 768)
        window.show()
        self.app.processEvents()

        self.assertEqual(window.findChildren(QToolBar), [])
        self.assertTrue(window._control_panel.isVisible())
        self.assertTrue(window.grid_checkbox.isVisible())
        self.assertTrue(window.crosshair_checkbox.isVisible())
        self.assertTrue(window.auto_y_button.isVisible())
        self.assertTrue(window.reset_view_button.isVisible())
        self.assertLessEqual(
            window._control_panel.sizeHint().width(), window.width()
        )

    def test_watcher_shares_mode_aware_repository(self) -> None:
        window = self._construct_window()
        self.assertIs(window._watcher.repository, window._repository)
        window.data_mode_combo.setCurrentText("Raw ADC")
        self.assertEqual(window._watcher.repository.source_mode, "raw")

    def test_raw_and_mv_modes_swap_controls_axes_grid_and_badge(self) -> None:
        window = self._construct_window()
        window.show()

        window.data_mode_combo.setCurrentText("Raw ADC")
        self.app.processEvents()
        self.assertTrue(window.raw_view_combo.isVisible())
        self.assertTrue(window.raw_y_scale_combo.isVisible())
        self.assertTrue(window.raw_grid_combo.isVisible())
        self.assertFalse(window.paper_speed_combo.isVisible())
        self.assertFalse(window.gain_combo.isVisible())
        self.assertFalse(window.y_range_combo.isVisible())
        self.assertFalse(window.display_calibration_btn.isVisible())
        self.assertFalse(window.display_calibration_action.isEnabled())
        self.assertIn("RAW ADC", window.mode_badge.text())
        self.assertEqual(window.plot_widget.display_unit, "count")
        self.assertEqual(window.plot_widget.grid_style, "engineering")
        raw_axis = window.plot_widget.plots[0].getAxis("left")
        self.assertEqual(raw_axis.label.toPlainText(), "Lead I (ADC count)")
        self.assertEqual(raw_axis.autoSIPrefixScale, 1.0)
        self.assertEqual(
            raw_axis.tickStrings([32768.0], raw_axis.autoSIPrefixScale, 100.0),
            ["32768"],
        )
        self.assertEqual(
            (
                window.plot_widget.grid_items[0].spacing.minor_x,
                window.plot_widget.grid_items[0].spacing.major_x,
            ),
            (0.1, 0.5),
        )

        window.data_mode_combo.setCurrentText("Calibrated mV")
        self.app.processEvents()
        self.assertFalse(window.raw_view_combo.isVisible())
        self.assertFalse(window.raw_y_scale_combo.isVisible())
        self.assertFalse(window.raw_grid_combo.isVisible())
        self.assertTrue(window.paper_speed_combo.isVisible())
        self.assertTrue(window.gain_combo.isVisible())
        self.assertTrue(window.y_range_combo.isVisible())
        self.assertTrue(window.display_calibration_btn.isVisible())
        self.assertTrue(window.display_calibration_action.isEnabled())
        self.assertEqual(window.plot_widget.display_unit, "mv")
        self.assertEqual(window.plot_widget.grid_style, "medical")
        mv_axis = window.plot_widget.plots[0].getAxis("left")
        self.assertEqual(mv_axis.label.toPlainText(), "Lead I (mV)")
        window._update_mode_badge(
            {"calibration": {"scale_mV_per_count": 0.001}}
        )
        self.assertIn("CALIBRATED mV", window.mode_badge.text())
        window._update_mode_badge(
            {"calibration": {"method": "legacy_preliminary"}}
        )
        self.assertIn("PRELIMINARY mV", window.mode_badge.text())

    def test_bpm_badge_information_and_status_follow_selected_frame(self) -> None:
        _write_frame(self.directory, 1, samples=_synthetic_ecg(60.0))
        window = self._construct_window()
        first = window._repository.get_by_id(1)
        self.assertIsNotNone(first)
        window._watcher.emit_ready(first)
        self.app.processEvents()

        self.assertEqual(window.bpm_badge.text().strip(), "BPM 60.0")
        self.assertIn("BPM 60.0", window.status_summary.text())
        rows = {
            window.info_table.item(row, 0).text(): window.info_table.item(row, 1).text()
            for row in range(window.info_table.rowCount())
        }
        self.assertEqual(rows["Estimated BPM"], "60.0 BPM")
        self.assertEqual(rows["R peaks detected"], "10")
        self.assertEqual(rows["BPM source"], "Lead II")

        _write_frame(
            self.directory,
            2,
            samples=_synthetic_ecg(150.0, amplitude_mv=15.0),
        )
        window._repository.refresh()
        second = window._repository.get_by_id(2)
        self.assertIsNotNone(second)
        window._watcher.emit_ready(second)
        window._select_record(second, user_requested=True, show_error=False)
        self.app.processEvents()

        self.assertEqual(window.validation_badge.text().strip(), "FAIL")
        self.assertEqual(window.bpm_badge.text().strip(), "BPM ~150.0")
        rows = {
            window.info_table.item(row, 0).text(): window.info_table.item(row, 1).text()
            for row in range(window.info_table.rowCount())
        }
        self.assertEqual(rows["Estimated BPM"], "150.0 BPM (unreliable)")

    def test_frame_selection_refresh_and_follow_live_invoke_auto_y(self) -> None:
        for frame_id in (1, 2):
            _write_frame(self.directory, frame_id)
        window = self._construct_window()
        first, second = window._repository.frames()

        with mock.patch.object(window.plot_widget, "auto_scale_y") as auto_y:
            window._watcher.emit_ready(first)
            self.app.processEvents()
            self.assertGreaterEqual(auto_y.call_count, 1)

            auto_y.reset_mock()
            window._watcher.emit_ready(second)
            window._frame_combo_activated(1)
            self.app.processEvents()
            self.assertGreaterEqual(auto_y.call_count, 1)

            auto_y.reset_mock()
            window.refresh_frames()
            self.app.processEvents()
            self.assertGreaterEqual(auto_y.call_count, 1)

        window.close()
        self.app.processEvents()
        self.window = None

        live_window = self._construct_window(follow_live=True)
        latest = live_window._repository.latest()
        with mock.patch.object(live_window.plot_widget, "auto_scale_y") as auto_y:
            live_window._watcher.emit_ready(latest)
            self.app.processEvents()
            self.app.processEvents()
            self.assertGreaterEqual(auto_y.call_count, 1)

    def test_mode_and_raw_view_changes_auto_y_without_mutating_source(self) -> None:
        _write_frame(self.directory, 1)
        raw_path = _write_raw_frame(self.directory, 1)
        window = self._construct_window()
        mv_record = window._repository.get_by_id(1)
        window._watcher.emit_ready(mv_record)
        self.app.processEvents()

        window._watcher._ready_paths.add(raw_path.resolve())
        with mock.patch.object(window.plot_widget, "auto_scale_y") as auto_y:
            window.data_mode_combo.setCurrentText("Raw ADC")
            self.app.processEvents()
            self.assertGreaterEqual(auto_y.call_count, 1)

            raw_source = window._raw_source_data
            source_before = raw_source.copy()
            auto_y.reset_mock()
            window.raw_view_combo.setCurrentText("Median-Centered")
            self.app.processEvents()
            self.assertGreaterEqual(auto_y.call_count, 1)
            np.testing.assert_array_equal(raw_source, source_before)

    def test_auto_y_preserves_x_uses_independent_ranges_and_reset_still_works(self) -> None:
        plot = ECGPlotWidget()
        lead_i = np.linspace(0.0, 1.0, 2500, dtype=np.float32)
        lead_ii = np.linspace(100.0, 200.0, 2500, dtype=np.float32)
        lead_iii = lead_ii - lead_i
        samples = np.column_stack((lead_i, lead_ii, lead_iii)).astype(
            np.float32, copy=False
        )
        source_before = samples.copy()
        plot.set_display_unit("count")
        plot.set_frame(samples)
        plot.set_y_ranges_synchronized(True)
        plot.plots[0].setXRange(2.0, 4.0, padding=0.0)
        x_before = tuple(plot.plots[0].viewRange()[0])

        replacement = samples.copy()
        replacement[:, 0] += 0.25
        plot.set_frame(replacement)
        self.assertEqual(tuple(plot.plots[0].viewRange()[0]), x_before)

        plot.auto_scale_y()

        self.assertEqual(tuple(plot.plots[0].viewRange()[0]), x_before)
        ranges = [tuple(item.viewRange()[1]) for item in plot.plots]
        self.assertNotEqual(ranges[0], ranges[1])
        np.testing.assert_array_equal(samples, source_before)

        plot.set_raw_y_scale(5000)
        plot.reset_view()
        for item in plot.plots:
            self.assertEqual(tuple(item.viewRange()[1]), (-5000.0, 5000.0))
        plot.close()

    def test_follow_live_coalesces_ready_burst_to_latest_frame(self) -> None:
        for frame_id in (1, 2, 3):
            _write_frame(self.directory, frame_id)
        window = self._construct_window(follow_live=True)
        watcher = window._watcher
        records = window._repository.frames()
        now = [100.0]
        window.plot_widget._clock = lambda: now[0]

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
        self.assertEqual(window.plot_widget.replay_state, "playing")
        self.assertEqual(window.plot_widget.replay_speed, 2.0)
        self.assertLess(
            window.plot_widget.current_sample_count,
            window.plot_widget.sample_count,
        )
        self.assertEqual(window.replay_speed_combo.currentData(), 2.0)
        self.assertFalse(window.replay_speed_combo.isEnabled())
        self.assertIn("2×", window.live_badge.text())
        self.assertIn("Replay 2×", window.live_status.text())

        now[0] += 1.0
        window.plot_widget._on_replay_tick()
        self.assertEqual(window.plot_widget.current_sample_count, 500)

        window.follow_live_checkbox.setChecked(False)
        self.assertTrue(window.replay_speed_combo.isEnabled())

    def test_follow_live_uses_newest_write_time_after_receiver_restart(self) -> None:
        first_path = _write_frame(self.directory, 1)
        second_path = _write_frame(self.directory, 2)
        base_ns = 1_800_000_000_000_000_000
        for path, modified_ns in (
            (first_path, base_ns),
            (second_path, base_ns + 1_000_000),
        ):
            os.utime(path, ns=(modified_ns, modified_ns))
            os.utime(path.with_suffix(".json"), ns=(modified_ns, modified_ns))

        window = self._construct_window(follow_live=True)
        watcher = window._watcher
        for record in window._repository.frames():
            watcher.emit_ready(record)
        self.app.processEvents()
        self.assertEqual(window._current_record.frame_id, 2)

        # A restarted receiver begins again at frame 1. Its lower numerical ID
        # must still win because this file is the newest completed write.
        replacement = _valid_samples(offset=0.15)
        np.save(first_path, replacement, allow_pickle=False)
        newest_ns = base_ns + 2_000_000
        os.utime(first_path, ns=(newest_ns, newest_ns))
        os.utime(
            first_path.with_suffix(".json"), ns=(newest_ns, newest_ns)
        )
        window._repository.refresh()
        restarted_first = window._repository.get_by_id(1)
        watcher.emit_ready(restarted_first)
        self.app.processEvents()
        self.app.processEvents()

        self.assertTrue(window.follow_live_checkbox.isChecked())
        self.assertEqual(window._current_record.frame_id, 1)
        np.testing.assert_array_equal(
            window.plot_widget.source_data, replacement
        )

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

    def test_finite_amplitude_fail_is_visible_for_diagnosis(self) -> None:
        samples = _valid_samples()
        samples[100, 1] = np.float32(15.8)
        samples[100, 2] = samples[100, 1] - samples[100, 0]
        _write_frame(self.directory, 1, samples=samples)
        window = self._construct_window()
        record = window._repository.get_by_id(1)

        window._watcher.emit_ready(record)
        self.app.processEvents()

        self.assertEqual(
            window._current_validation.status, ValidationStatus.FAIL
        )
        self.assertTrue(window._current_validation.can_plot)
        self.assertEqual(window.plot_widget.sample_count, 2500)
        self.assertEqual(window.validation_badge.text().strip(), "FAIL")
        self.assertGreaterEqual(
            window.plot_widget.plots[1].viewRange()[1][1], 15.8
        )

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
