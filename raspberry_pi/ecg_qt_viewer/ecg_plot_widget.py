"""Three-lead, clinically scaled ECG plotting widget."""

from __future__ import annotations

from enum import Enum
import math
from pathlib import Path
import time
from typing import Callable, Sequence

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from .ecg_grid_item import ECGGridItem, calculate_grid_spacing


LEAD_NAMES = ("Lead I", "Lead II", "Lead III")
PAPER_BACKGROUND = "#fffafa"
ENGINEERING_BACKGROUND = "#f6f8fa"
ECG_INK = "#352522"
AXIS_INK = "#6d514c"


class ReplayState(str, Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class ECGPlotWidget(QtWidgets.QWidget):
    """One GraphicsLayoutWidget containing three synchronized ECG plots.

    The input array is retained by reference and is never written, filtered,
    clipped, resampled or normalized.  pyqtgraph clipping/downsampling is used
    only while rendering the curves.
    """

    replayStateChanged = QtCore.pyqtSignal(str)
    replayProgressChanged = QtCore.pyqtSignal(int, int)
    replayFinished = QtCore.pyqtSignal()
    cursorChanged = QtCore.pyqtSignal(float, float, float, float)
    physicalScaleChanged = QtCore.pyqtSignal(float)

    # Snake-case signal variants keep integration code readable.
    replay_state_changed = QtCore.pyqtSignal(str)
    replay_progress_changed = QtCore.pyqtSignal(int, int)
    replay_finished = QtCore.pyqtSignal()
    cursor_changed = QtCore.pyqtSignal(float, float, float, float)

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        sample_rate: float = 250.0,
        line_width: float = 1.25,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(parent)
        if not math.isfinite(float(sample_rate)) or float(sample_rate) <= 0:
            raise ValueError("sample rate must be finite and positive")

        self._sample_rate = float(sample_rate)
        self._source_data: np.ndarray | None = None
        self._time_data = np.empty(0, dtype=np.float64)
        self._paper_speed_mm_s = 25.0
        self._gain_mm_mv = 10.0
        self._time_window_s = 10.0
        self._y_range_mv = 2.0
        self._lead_mode = "All leads"
        self._grid_enabled = True
        self._crosshair_enabled = True
        self._view_locked = False
        self._synchronize_y_ranges = False
        self._line_width = _positive_finite(line_width, "line width")
        self._display_scale_factor = 1.0
        self._auto_y_active = False
        self._syncing_x = False
        self._syncing_y = False
        self._active_crosshair_plot: int | None = None

        # Raw ADC mode properties
        self._display_unit = "mv"  # "mv" or "count"
        self._raw_y_scale = 10000.0
        self._raw_grid_spacing = 1000.0
        self._grid_style = "medical"  # "medical" or "engineering"

        self._clock = clock or time.monotonic
        self._replay_state = ReplayState.STOPPED
        self._replay_speed = 1.0
        self._replay_sample_count = 0
        self._replay_base_seconds = 0.0
        self._replay_started_at = 0.0
        self._follow_replay_view = True
        self._replay_timer = QtCore.QTimer(self)
        self._replay_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._replay_timer.setInterval(30)
        self._replay_timer.timeout.connect(self._on_replay_tick)

        self.graphics_widget = pg.GraphicsLayoutWidget(self)
        self.graphics_layout_widget = self.graphics_widget
        self.plot_widget = self.graphics_widget
        self.graphics_widget.setBackground(PAPER_BACKGROUND)
        self.graphics_widget.setStyleSheet(
            f"background-color: {PAPER_BACKGROUND}; border: 0;"
        )

        # A scroll area is necessary for calibrated 50 mm/s rendering: a full
        # 10-second strip is about 500 physical millimetres wide.
        self.scroll_area = QtWidgets.QScrollArea(self)
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignHCenter
            | QtCore.Qt.AlignmentFlag.AlignTop
        )
        self.scroll_area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll_area.setWidget(self.graphics_widget)
        self.scroll_area.setStyleSheet(f"background: {PAPER_BACKGROUND};")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.scroll_area)

        self.plots: list[pg.PlotItem] = []
        self.plot_items = self.plots
        self.curves: list[pg.PlotDataItem] = []
        self.grid_items: list[ECGGridItem] = []
        self.vertical_cursors: list[pg.InfiniteLine] = []
        self.horizontal_cursors: list[pg.InfiniteLine] = []
        self.crosshair_labels: list[pg.TextItem] = []

        for index, lead_name in enumerate(LEAD_NAMES):
            plot = self.graphics_widget.addPlot(row=index, col=0)
            self._configure_plot(plot, lead_name)
            self.plots.append(plot)

            grid = ECGGridItem(
                self._paper_speed_mm_s,
                self._gain_mm_mv,
                raw_count_spacing=(self._raw_grid_spacing if self._display_unit == "count" else None),
            )
            plot.addItem(grid, ignoreBounds=True)
            grid.attach_to_viewbox(plot.getViewBox())
            self.grid_items.append(grid)

            curve = plot.plot(
                np.empty(0, dtype=np.float64),
                np.empty(0, dtype=np.float32),
                pen=self._curve_pen(),
                symbol=None,
                connect="all",
                skipFiniteCheck=True,
            )
            curve.setZValue(10.0)
            curve.setClipToView(True)
            curve.setDownsampling(auto=True, method="peak")
            self.curves.append(curve)

            vertical = pg.InfiniteLine(
                angle=90,
                movable=False,
                pen=pg.mkPen("#7c5550", width=1.0),
            )
            horizontal = pg.InfiniteLine(
                angle=0,
                movable=False,
                pen=pg.mkPen("#7c5550", width=1.0),
            )
            vertical.setZValue(100.0)
            horizontal.setZValue(100.0)
            vertical.hide()
            horizontal.hide()
            plot.addItem(vertical, ignoreBounds=True)
            plot.addItem(horizontal, ignoreBounds=True)
            self.vertical_cursors.append(vertical)
            self.horizontal_cursors.append(horizontal)

            label = pg.TextItem(
                color="#2e2220",
                fill=pg.mkBrush(255, 255, 248, 235),
                border=pg.mkPen("#9b7771", width=1.0),
                anchor=(0.0, 0.0),
            )
            label.setZValue(110.0)
            label.hide()
            plot.addItem(label, ignoreBounds=True)
            self.crosshair_labels.append(label)

            view_box = plot.getViewBox()
            view_box.sigXRangeChanged.connect(
                lambda _vb, ranges, source=index: self._sync_x_range(
                    source, ranges
                )
            )
            view_box.sigYRangeChanged.connect(
                lambda _vb, ranges, source=index: self._sync_y_range(
                    source, ranges
                )
            )

        scene = self.graphics_widget.scene()
        self._mouse_proxy = pg.SignalProxy(
            scene.sigMouseMoved,
            rateLimit=30,
            slot=self._on_mouse_moved,
        )
        self.graphics_widget.installEventFilter(self)
        self.graphics_widget.viewport().installEventFilter(self)
        self.scroll_area.viewport().installEventFilter(self)

        self.reset_view()
        self._update_physical_geometry()

    # ------------------------------------------------------------------
    # Source data
    @property
    def source_data(self) -> np.ndarray | None:
        """The exact float32 object supplied to :meth:`set_frame`."""

        return self._source_data

    @property
    def time_data(self) -> np.ndarray:
        return self._time_data

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @property
    def sample_count(self) -> int:
        return 0 if self._source_data is None else int(self._source_data.shape[0])

    @property
    def duration_s(self) -> float:
        return self.sample_count / self._sample_rate

    def set_frame(
        self,
        samples: np.ndarray,
        time_seconds: np.ndarray | Sequence[float] | float | None = None,
        *,
        sample_rate: float | None = None,
    ) -> None:
        """Display a float32 ``(samples, 3)`` frame without modifying it.

        The second positional argument may be a float64 time array for older
        callers, or a scalar sample rate.  With no supplied time array the
        canonical expression is used exactly::

            np.arange(sample_count, dtype=np.float64) / sample_rate
        """

        if not isinstance(samples, np.ndarray):
            raise TypeError("ECG samples must be a NumPy array")
        if samples.ndim != 2 or samples.shape[1] != 3:
            raise ValueError("ECG samples must have shape (sample_count, 3)")
        if samples.dtype != np.dtype(np.float32):
            raise TypeError("ECG source array must have dtype float32")

        if time_seconds is not None and np.isscalar(time_seconds):
            if sample_rate is not None:
                raise TypeError("sample rate was supplied twice")
            sample_rate = float(time_seconds)
            time_seconds = None
        rate = self._sample_rate if sample_rate is None else float(sample_rate)
        if not math.isfinite(rate) or rate <= 0.0:
            raise ValueError("sample rate must be finite and positive")

        if time_seconds is None:
            times = np.arange(samples.shape[0], dtype=np.float64) / rate
        else:
            if not isinstance(time_seconds, np.ndarray):
                times = np.asarray(time_seconds, dtype=np.float64)
            else:
                times = time_seconds
            if times.dtype != np.dtype(np.float64):
                raise TypeError("time array must have dtype float64")
            if times.ndim != 1 or times.shape[0] != samples.shape[0]:
                raise ValueError("time array must match the ECG sample count")
            if times.size > 1 and np.any(np.diff(times) <= 0.0):
                raise ValueError("time array must be strictly increasing")

        previous_x_range = (
            tuple(float(value) for value in self.plots[0].viewRange()[0])
            if self._source_data is not None
            else None
        )
        self.stop_replay(show_full=False)
        self._sample_rate = rate
        self._source_data = samples
        self._time_data = times
        self._replay_sample_count = self.sample_count
        self._set_x_limits()
        self._render_sample_count(self.sample_count)
        self.reset_view()
        if previous_x_range is not None:
            # Replacing a frame or applying a display-only Raw View transform
            # must not discard the operator's selected time window.  Clamp
            # only when the new frame is shorter than the old visible range.
            maximum = max(self.duration_s, 1e-9)
            old_start, old_end = previous_x_range
            span = min(max(old_end - old_start, 1e-9), maximum)
            start = min(max(0.0, old_start), maximum - span)
            self.plots[0].setXRange(start, start + span, padding=0.0)
        self.mouse_left()

    def clear_frame(self) -> None:
        self.stop_replay(show_full=False)
        self._source_data = None
        self._time_data = np.empty(0, dtype=np.float64)
        self._replay_sample_count = 0
        for curve in self.curves:
            curve.setData(
                np.empty(0, dtype=np.float64),
                np.empty(0, dtype=np.float32),
                skipFiniteCheck=True,
            )
        self.mouse_left()
        self.replayProgressChanged.emit(0, 0)
        self.replay_progress_changed.emit(0, 0)

    # ------------------------------------------------------------------
    # Clinical display controls
    @property
    def paper_speed_mm_s(self) -> float:
        return self._paper_speed_mm_s

    @property
    def display_unit(self) -> str:
        """Current display unit: 'mV' or 'count'."""
        return self._display_unit

    def set_display_unit(self, unit: str) -> None:
        """Set display unit to 'mV' or 'count'."""
        normalized = str(unit).lower().strip()
        if normalized not in ("mv", "count"):
            raise ValueError(f"Invalid display unit: {unit!r}; expected 'mV' or 'count'")
        self._display_unit = normalized

        # Update grids based on unit
        if normalized == "count":
            for grid in self.grid_items:
                grid.set_raw_count_spacing(self._raw_grid_spacing)
        else:
            for grid in self.grid_items:
                grid.set_mv_mode()

        axis_unit = "ADC count" if normalized == "count" else "mV"
        for plot, lead_name in zip(self.plots, LEAD_NAMES):
            left_axis = plot.getAxis("left")
            # The values are already expressed in ADC count or mV.  Letting
            # pyqtgraph add SI prefixes would produce misleading labels such
            # as "mmV" or "kADC count" as the visible range changes.
            left_axis.enableAutoSIPrefix(False)
            left_axis.autoSIPrefixScale = 1.0
            left_axis.labelUnitPrefix = ""
            left_axis.setScale(1.0)
            plot.setLabel("left", lead_name, units=axis_unit)
            plot.setLabel("bottom", "Time", units="s")

        self._update_physical_geometry()

    def set_grid_style(self, style: str) -> None:
        """Set grid style to 'medical' or 'engineering'."""
        normalized = str(style).lower().strip()
        if normalized not in ("medical", "engineering"):
            raise ValueError(f"Invalid grid style: {style!r}; expected 'medical' or 'engineering'")
        self._grid_style = normalized

        # Update background color
        if normalized == "engineering":
            self.graphics_widget.setBackground(ENGINEERING_BACKGROUND)
            self.graphics_widget.setStyleSheet(
                f"background-color: {ENGINEERING_BACKGROUND}; border: 0;"
            )
            self.scroll_area.setStyleSheet(
                f"background: {ENGINEERING_BACKGROUND};"
            )
        else:
            # Pink ECG paper background for medical display
            self.graphics_widget.setBackground(PAPER_BACKGROUND)
            self.graphics_widget.setStyleSheet(
                f"background-color: {PAPER_BACKGROUND}; border: 0;"
            )
            self.scroll_area.setStyleSheet(f"background: {PAPER_BACKGROUND};")

        # Update grid style
        for grid in self.grid_items:
            grid.set_style(normalized)

    @property
    def grid_style(self) -> str:
        return self._grid_style

    def set_paper_speed(self, speed_mm_s: float) -> None:
        # The mapping function performs strict support validation.
        calculate_grid_spacing(speed_mm_s, self._gain_mm_mv)
        self._paper_speed_mm_s = float(speed_mm_s)
        for grid in self.grid_items:
            grid.set_paper_speed(speed_mm_s)
        self._update_physical_geometry()

    set_speed = set_paper_speed

    @property
    def gain_mm_mv(self) -> float:
        return self._gain_mm_mv

    def set_gain(self, gain_mm_mv: float) -> None:
        calculate_grid_spacing(self._paper_speed_mm_s, gain_mm_mv)
        self._gain_mm_mv = float(gain_mm_mv)
        for grid in self.grid_items:
            grid.set_gain(gain_mm_mv)
        self._update_physical_geometry()

    @property
    def time_window_s(self) -> float:
        return self._time_window_s

    def set_time_window(self, seconds: float) -> None:
        self._time_window_s = _positive_finite(seconds, "time window")
        self.reset_view()
        self._update_physical_geometry()

    @property
    def y_range_mv(self) -> float:
        return self._y_range_mv

    def set_y_range(self, half_range_mv: float) -> None:
        self._y_range_mv = _positive_finite(half_range_mv, "Y range")
        self._auto_y_active = False
        for plot in self.plots:
            plot.setYRange(
                -self._y_range_mv, self._y_range_mv, padding=0.0
            )
        self._update_physical_geometry()

    @property
    def raw_y_scale(self) -> float:
        return self._raw_y_scale

    def set_raw_y_scale(self, half_range_count: float) -> None:
        """Set Y range for raw ADC count mode."""
        self._raw_y_scale = _positive_finite(half_range_count, "raw Y scale")
        self._auto_y_active = False
        if self._display_unit == "count":
            for plot in self.plots:
                plot.setYRange(
                    -self._raw_y_scale, self._raw_y_scale, padding=0.0
                )

    @property
    def raw_grid_spacing(self) -> float:
        return self._raw_grid_spacing

    def set_raw_grid_spacing(self, spacing: float) -> None:
        """Set vertical grid spacing for raw ADC count mode."""
        self._raw_grid_spacing = _positive_finite(spacing, "raw grid spacing")
        if self._display_unit == "count":
            for grid in self.grid_items:
                grid.set_raw_count_spacing(spacing)

    @property
    def lead_mode(self) -> str:
        return self._lead_mode

    def set_lead_mode(self, mode: str) -> None:
        normalized = _normalize_lead_mode(mode)
        self._lead_mode = normalized
        for index, plot in enumerate(self.plots):
            visible = normalized == "All leads" or normalized == LEAD_NAMES[index]
            plot.setVisible(visible)
        self.mouse_left()
        self._update_physical_geometry()

    def set_grid_visible(self, visible: bool) -> None:
        self._grid_enabled = bool(visible)
        for grid in self.grid_items:
            grid.setVisible(self._grid_enabled)

    set_grid_enabled = set_grid_visible

    def set_crosshair_enabled(self, enabled: bool) -> None:
        self._crosshair_enabled = bool(enabled)
        if not self._crosshair_enabled:
            self.mouse_left()

    set_crosshair_visible = set_crosshair_enabled

    def set_line_width(self, width: float) -> None:
        self._line_width = _positive_finite(width, "line width")
        pen = self._curve_pen()
        for curve in self.curves:
            curve.setPen(pen)

    def set_display_scale_factor(self, factor: float) -> None:
        self._display_scale_factor = _positive_finite(
            factor, "display scale factor"
        )
        for grid in self.grid_items:
            grid.set_display_scale_factor(self._display_scale_factor)
        self.set_line_width(self._line_width)
        self._update_physical_geometry()
        self.physicalScaleChanged.emit(self._display_scale_factor)

    @property
    def display_scale_factor(self) -> float:
        return self._display_scale_factor

    def set_view_locked(self, locked: bool) -> None:
        self._view_locked = bool(locked)
        for plot in self.plots:
            view_box = plot.getViewBox()
            view_box.setMouseEnabled(
                x=not self._view_locked, y=not self._view_locked
            )
            view_box.setMenuEnabled(not self._view_locked)

    set_lock_view = set_view_locked

    def set_y_ranges_synchronized(self, synchronized: bool) -> None:
        self._synchronize_y_ranges = bool(synchronized)
        if synchronized:
            current = self.plots[0].viewRange()[1]
            self._sync_y_range(0, current)

    def reset_view(self) -> None:
        self._auto_y_active = False
        x_end = self._default_x_end()
        for plot in self.plots:
            plot.setXRange(0.0, x_end, padding=0.0)
            # Use appropriate Y range based on display unit
            if self._display_unit == "count":
                plot.setYRange(-self._raw_y_scale, self._raw_y_scale, padding=0.0)
            else:
                plot.setYRange(-self._y_range_mv, self._y_range_mv, padding=0.0)
        self._update_physical_geometry()

    def auto_scale_y(self) -> None:
        if self._source_data is None or self._source_data.size == 0:
            return
        self._auto_y_active = True
        # Auto Y is deliberately independent per lead.  In particular, raw
        # ADC channels can have very different offsets and amplitudes.  Keep
        # Sync Y as a manual pan/zoom aid without letting its range-change
        # signal collapse these independently calculated ranges.
        self._syncing_y = True
        try:
            for index, plot in enumerate(self.plots):
                values = self._source_data[:, index]
                low, high = _padded_range(
                    float(np.min(values)), float(np.max(values))
                )
                plot.setYRange(low, high, padding=0.0)
        finally:
            self._syncing_y = False
        self._update_physical_geometry(use_current_y_ranges=True)

    auto_y = auto_scale_y

    # ------------------------------------------------------------------
    # Replay
    @property
    def replay_state(self) -> str:
        return self._replay_state.value

    @property
    def current_sample_count(self) -> int:
        return self._replay_sample_count

    @property
    def replay_speed(self) -> float:
        return self._replay_speed

    def set_replay_speed(self, speed: float) -> None:
        value = float(speed)
        if value not in (0.5, 1.0, 2.0):
            raise ValueError("replay speed must be 0.5x, 1x, or 2x")
        if self._replay_state is ReplayState.PLAYING:
            now = self._clock()
            self._replay_base_seconds = self._replay_elapsed_at(now)
            self._replay_started_at = now
        self._replay_speed = value

    @QtCore.pyqtSlot()
    def start_replay(self, speed: float | None = None) -> None:
        if self._source_data is None or self.sample_count == 0:
            return
        if speed is not None:
            self.set_replay_speed(speed)
        if self._replay_state is ReplayState.PLAYING:
            return

        if (
            self._replay_state is ReplayState.STOPPED
            or self._replay_sample_count >= self.sample_count
        ):
            self._replay_base_seconds = 0.0
            self._render_sample_count(0)
            for plot in self.plots:
                plot.setXRange(0.0, self._default_x_end(), padding=0.0)
        self._replay_started_at = self._clock()
        self._set_replay_state(ReplayState.PLAYING)
        self._replay_timer.start()

    play_replay = start_replay

    @QtCore.pyqtSlot()
    def pause_replay(self) -> None:
        if self._replay_state is not ReplayState.PLAYING:
            return
        now = self._clock()
        self._replay_base_seconds = min(
            self.duration_s, self._replay_elapsed_at(now)
        )
        self._replay_timer.stop()
        self._set_replay_state(ReplayState.PAUSED)

    @QtCore.pyqtSlot()
    def toggle_replay(self) -> None:
        if self._replay_state is ReplayState.PLAYING:
            self.pause_replay()
        else:
            self.start_replay()

    def stop_replay(self, *, show_full: bool = True) -> None:
        self._replay_timer.stop()
        self._replay_base_seconds = 0.0
        self._replay_started_at = 0.0
        self._set_replay_state(ReplayState.STOPPED)
        count = self.sample_count if show_full else 0
        self._render_sample_count(count)

    def set_replay_sample_count(self, sample_count: int) -> None:
        """Render a synchronized prefix; useful for controller-driven replay."""

        self._render_sample_count(sample_count)

    @QtCore.pyqtSlot()
    def _on_replay_tick(self) -> None:
        if self._replay_state is not ReplayState.PLAYING:
            return
        elapsed = self._replay_elapsed_at(self._clock())
        target = min(self.sample_count, int(elapsed * self._sample_rate))
        self._render_sample_count(target)
        self._scroll_replay_window(target)
        if elapsed >= self.duration_s or target >= self.sample_count:
            self._render_sample_count(self.sample_count)
            self._replay_timer.stop()
            self._replay_base_seconds = 0.0
            self._set_replay_state(ReplayState.STOPPED)
            self.replayFinished.emit()
            self.replay_finished.emit()

    def _replay_elapsed_at(self, now: float) -> float:
        elapsed_wall = max(0.0, float(now) - self._replay_started_at)
        return self._replay_base_seconds + elapsed_wall * self._replay_speed

    def _set_replay_state(self, state: ReplayState) -> None:
        if state is self._replay_state:
            return
        self._replay_state = state
        self.replayStateChanged.emit(state.value)
        self.replay_state_changed.emit(state.value)

    def _render_sample_count(self, sample_count: int) -> None:
        if self._source_data is None:
            self._replay_sample_count = 0
            return
        count = max(0, min(int(sample_count), self.sample_count))
        x_view = self._time_data[:count]
        for index, curve in enumerate(self.curves):
            # Both slices are views; the float32 ECG source is not converted.
            curve.setData(
                x_view,
                self._source_data[:count, index],
                skipFiniteCheck=True,
            )
        self._replay_sample_count = count
        self.replayProgressChanged.emit(count, self.sample_count)
        self.replay_progress_changed.emit(count, self.sample_count)

    def _scroll_replay_window(self, count: int) -> None:
        if not self._follow_replay_view or count <= 0:
            return
        duration = self.duration_s
        if self._time_window_s >= duration:
            return
        current_time = min(duration, count / self._sample_rate)
        current_range = self.plots[0].viewRange()[0]
        if current_time <= current_range[1]:
            return
        start = max(0.0, current_time - self._time_window_s)
        end = min(duration, start + self._time_window_s)
        self.plots[0].setXRange(start, end, padding=0.0)

    # ------------------------------------------------------------------
    # Crosshair
    def _on_mouse_moved(self, event) -> None:
        if not self._crosshair_enabled or self._source_data is None:
            self.mouse_left()
            return
        scene_position = event
        while isinstance(scene_position, (tuple, list)):
            if not scene_position:
                self.mouse_left()
                return
            scene_position = scene_position[0]

        active_index = None
        for index, plot in enumerate(self.plots):
            plot_area = plot.getViewBox().sceneBoundingRect()
            if plot.isVisible() and plot_area.contains(scene_position):
                active_index = index
                break
        if active_index is None:
            self.mouse_left()
            return

        plot = self.plots[active_index]
        point = plot.getViewBox().mapSceneToView(scene_position)
        sample_index = self._nearest_sample_index(float(point.x()))
        if sample_index is None:
            self.mouse_left()
            return

        timestamp = float(self._time_data[sample_index])
        values = self._source_data[sample_index]
        for vertical in self.vertical_cursors:
            vertical.setPos(timestamp)
            vertical.show()
        for index, horizontal in enumerate(self.horizontal_cursors):
            if index == active_index:
                horizontal.setPos(float(values[index]))
                horizontal.show()
            else:
                horizontal.hide()
        for index, label in enumerate(self.crosshair_labels):
            if index == active_index:
                # Use appropriate unit based on display mode
                unit = "mV" if self._display_unit == "mv" else "count"
                value_format = ".6f" if self._display_unit == "mv" else ".0f"
                label.setText(
                    f"t: {timestamp:.3f} s\n"
                    f"Lead I: {float(values[0]):{value_format}} {unit}\n"
                    f"Lead II: {float(values[1]):{value_format}} {unit}\n"
                    f"Lead III: {float(values[2]):{value_format}} {unit}"
                )
                x_range, y_range = plot.viewRange()
                label.setPos(
                    x_range[0] + 0.018 * (x_range[1] - x_range[0]),
                    y_range[1] - 0.04 * (y_range[1] - y_range[0]),
                )
                label.show()
            else:
                label.hide()
        self._active_crosshair_plot = active_index
        cursor_values = tuple(float(value) for value in values)
        self.cursorChanged.emit(timestamp, *cursor_values)
        self.cursor_changed.emit(timestamp, *cursor_values)

    def _nearest_sample_index(self, x_value: float) -> int | None:
        if self._time_data.size == 0 or not math.isfinite(x_value):
            return None
        insertion = int(np.searchsorted(self._time_data, x_value, side="left"))
        if insertion <= 0:
            return 0
        if insertion >= self._time_data.size:
            return int(self._time_data.size - 1)
        before = insertion - 1
        if (x_value - self._time_data[before]) <= (
            self._time_data[insertion] - x_value
        ):
            return before
        return insertion

    @QtCore.pyqtSlot()
    def mouse_left(self) -> None:
        self._active_crosshair_plot = None
        for item in (
            *self.vertical_cursors,
            *self.horizontal_cursors,
            *self.crosshair_labels,
        ):
            item.hide()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QtCore.QEvent.Type.Leave:
            self.mouse_left()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Physical display geometry and linked ranges
    def _logical_dpi(self) -> tuple[float, float]:
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return 96.0, 96.0
        return (
            float(screen.logicalDotsPerInchX()),
            float(screen.logicalDotsPerInchY()),
        )

    def _update_physical_geometry(
        self, *, use_current_y_ranges: bool | None = None
    ) -> None:
        """Map seconds/mV to physical pixels without altering coordinates.

        The selected clinical range has exact target density after Reset View.
        User zoom intentionally overrides that clinical scale until reset.
        """

        if self._display_unit == "count":
            # Raw ADC is not paper.  Give it a stable, readable engineering
            # canvas instead of multiplying count ranges by clinical mm/mV.
            plot_width = max(600.0, self._time_window_s * 90.0)
            plot_heights = [180.0] * len(self.plots)
            for plot in self.plots:
                view_box = plot.getViewBox()
                view_box.setMinimumWidth(plot_width)
                view_box.setMaximumWidth(plot_width)
                view_box.setMinimumHeight(180.0)
                view_box.setMaximumHeight(180.0)

            visible_count = 3 if self._lead_mode == "All leads" else 1
            self.graphics_widget.setFixedSize(
                int(math.ceil(plot_width + 105.0)),
                int(math.ceil(234.0 * visible_count + 12.0)),
            )
            self.graphics_widget.updateGeometry()
            return

        dpi_x, dpi_y = self._logical_dpi()
        pixels_per_mm_x = dpi_x / 25.4 * self._display_scale_factor
        pixels_per_mm_y = dpi_y / 25.4 * self._display_scale_factor
        plot_width = max(
            120.0,
            self._time_window_s
            * self._paper_speed_mm_s
            * pixels_per_mm_x,
        )

        if use_current_y_ranges is None:
            use_current_y_ranges = self._auto_y_active
        plot_heights: list[float] = []
        for plot in self.plots:
            if use_current_y_ranges:
                y_range = plot.viewRange()[1]
                y_span = max(1e-9, float(y_range[1] - y_range[0]))
            else:
                y_span = 2.0 * self._y_range_mv
            plot_heights.append(
                max(60.0, y_span * self._gain_mm_mv * pixels_per_mm_y)
            )

        # Equalize heights for all 3 plots in All leads mode
        if self._lead_mode == "All leads":
            max_height = max(plot_heights)
            plot_heights = [max_height, max_height, max_height]
        elif self._lead_mode in LEAD_NAMES:
            lead_idx = LEAD_NAMES.index(self._lead_mode)
            single_height = max(plot_heights[lead_idx], 360.0)
            plot_heights = [single_height] * 3

        for plot, height in zip(self.plots, plot_heights):
            view_box = plot.getViewBox()
            view_box.setMinimumWidth(plot_width)
            view_box.setMaximumWidth(plot_width)
            view_box.setMinimumHeight(height)
            view_box.setMaximumHeight(height)

        visible_count = 3 if self._lead_mode == "All leads" else 1
        visible_heights = (
            plot_heights
            if visible_count == 3
            else [plot_heights[LEAD_NAMES.index(self._lead_mode)]]
        )
        canvas_width = int(math.ceil(plot_width + 105.0))
        canvas_height = int(
            math.ceil(sum(visible_heights) + 40.0 * visible_count + 12.0)
        )
        self.graphics_widget.setFixedSize(canvas_width, canvas_height)
        self.graphics_widget.updateGeometry()



    @property
    def recommended_canvas_size(self) -> QtCore.QSize:
        return self.graphics_widget.size()

    def _sync_x_range(self, source: int, ranges: Sequence[float]) -> None:
        if self._syncing_x:
            return
        self._syncing_x = True
        try:
            for index, plot in enumerate(self.plots):
                if index != source:
                    plot.setXRange(float(ranges[0]), float(ranges[1]), padding=0.0)
        finally:
            self._syncing_x = False

    def _sync_y_range(self, source: int, ranges: Sequence[float]) -> None:
        if not self._synchronize_y_ranges or self._syncing_y:
            return
        self._syncing_y = True
        try:
            for index, plot in enumerate(self.plots):
                if index != source:
                    plot.setYRange(
                        float(ranges[0]), float(ranges[1]), padding=0.0
                    )
        finally:
            self._syncing_y = False

    def _set_x_limits(self) -> None:
        maximum = max(self.duration_s, self._time_window_s, 1e-9)
        for plot in self.plots:
            plot.getViewBox().setLimits(xMin=0.0, xMax=maximum)

    def _default_x_end(self) -> float:
        if self._source_data is None:
            return self._time_window_s
        return min(self._time_window_s, self.duration_s)

    # ------------------------------------------------------------------
    # Export
    def export_png(self, file_path: str | Path) -> Path:
        """Capture all three currently rendered plots and their medical grid."""

        destination = Path(file_path).expanduser()
        if destination.suffix.lower() != ".png":
            destination = destination.with_suffix(".png")
        pixmap = self.graphics_widget.grab()
        if pixmap.isNull() or not pixmap.save(str(destination), "PNG"):
            raise OSError(f"could not export ECG view to {destination}")
        return destination

    export_current_view = export_png

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._update_physical_geometry()

    # ------------------------------------------------------------------
    def _configure_plot(self, plot: pg.PlotItem, lead_name: str) -> None:
        plot.setLabel("left", lead_name, units="mV")
        plot.setLabel("bottom", "Time", units="s")
        plot.showGrid(x=False, y=False)
        plot.hideButtons()
        plot.setMenuEnabled(True)
        plot.setMouseEnabled(x=True, y=True)
        plot.getViewBox().setMouseMode(pg.ViewBox.PanMode)
        plot.getViewBox().setDefaultPadding(0.0)
        for axis_name in ("left", "bottom"):
            axis = plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(AXIS_INK, width=1.0))
            axis.setTextPen(pg.mkPen(AXIS_INK))

    def _curve_pen(self):
        # Display factor affects cosmetic rendering only, never the samples.
        return pg.mkPen(
            ECG_INK,
            width=max(0.25, self._line_width * self._display_scale_factor),
        )


def _normalize_lead_mode(mode: str) -> str:
    normalized = str(mode).strip().lower()
    aliases = {
        "all": "All leads",
        "all leads": "All leads",
        "lead i": "Lead I",
        "i": "Lead I",
        "lead ii": "Lead II",
        "ii": "Lead II",
        "lead iii": "Lead III",
        "iii": "Lead III",
    }
    if normalized not in aliases:
        raise ValueError(f"unsupported lead mode: {mode!r}")
    return aliases[normalized]


def _positive_finite(value: float, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _padded_range(low: float, high: float) -> tuple[float, float]:
    if math.isclose(low, high, rel_tol=0.0, abs_tol=1e-12):
        padding = max(0.05, abs(low) * 0.08)
    else:
        padding = (high - low) * 0.08
    return low - padding, high + padding


__all__ = ["ECGPlotWidget", "LEAD_NAMES", "ReplayState"]
