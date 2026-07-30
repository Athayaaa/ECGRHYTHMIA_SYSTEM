"""Persistent, non-clinical settings for the standalone ECG viewer.

Only presentation preferences are stored here.  ECG samples, timestamps and
metadata are deliberately outside this module and are never written to
``QSettings``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any

from PyQt6 import QtCore, QtGui, QtWidgets


ORGANIZATION_NAME = "ECGRHYTHMIA"
APPLICATION_NAME = "ECG Qt Viewer"

PAPER_SPEEDS_MM_S = (25.0, 50.0)
GAINS_MM_MV = (5.0, 10.0, 20.0)
TIME_WINDOWS_S = (2.5, 5.0, 10.0)
Y_RANGES_MV = (0.5, 1.0, 2.0, 5.0)
LEAD_MODES = ("All leads", "Lead I", "Lead II", "Lead III")
REPLAY_SPEEDS = (0.5, 1.0, 2.0)

# Raw ADC mode settings
DATA_MODES = ("Raw ADC", "Calibrated mV")
RAW_VIEW_MODES = ("Absolute Code", "Zero-Centered", "Median-Centered")
RAW_Y_SCALES = (1000.0, 2500.0, 5000.0, 10000.0, 25000.0, 50000.0)
RAW_GRID_SPACINGS = (100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0)


@dataclass(frozen=True, slots=True)
class ViewerSettings:
    """Validated viewer preferences with clinically conventional defaults."""

    last_directory: str = ""
    paper_speed_mm_s: float = 25.0
    gain_mm_per_mv: float = 10.0
    time_window_s: float = 10.0
    y_range_mv: float = 2.0
    lead_mode: str = "All leads"
    grid_enabled: bool = True
    crosshair_enabled: bool = True
    display_scale_factor: float = 1.0
    line_width: float = 1.25
    replay_speed: float = 1.0
    lock_view: bool = False
    synchronize_y_ranges: bool = False
    data_mode: str = "Calibrated mV"
    raw_view_mode: str = "Absolute Code"
    raw_y_scale: float = 10000.0
    raw_grid_spacing: float = 1000.0
    notch_50hz: bool = False
    zero_origin: bool = False

    @property
    def gain_mm_mv(self) -> float:
        """Compatibility alias used by some UI code."""

        return self.gain_mm_per_mv

    @property
    def paper_speed_mm_per_s(self) -> float:
        return self.paper_speed_mm_s

    def normalized(self) -> "ViewerSettings":
        """Return a safe settings value, replacing unsupported values."""

        return replace(
            self,
            paper_speed_mm_s=_choice_or_default(
                self.paper_speed_mm_s, PAPER_SPEEDS_MM_S, 25.0
            ),
            gain_mm_per_mv=_choice_or_default(
                self.gain_mm_per_mv, GAINS_MM_MV, 10.0
            ),
            time_window_s=_choice_or_default(
                self.time_window_s, TIME_WINDOWS_S, 10.0
            ),
            y_range_mv=_choice_or_default(
                self.y_range_mv, Y_RANGES_MV, 2.0
            ),
            lead_mode=(
                self.lead_mode if self.lead_mode in LEAD_MODES else "All leads"
            ),
            display_scale_factor=_finite_in_range(
                self.display_scale_factor, 0.25, 4.0, 1.0
            ),
            line_width=_finite_in_range(self.line_width, 0.25, 8.0, 1.25),
            replay_speed=_choice_or_default(
                self.replay_speed, REPLAY_SPEEDS, 1.0
            ),
            data_mode=(
                self.data_mode if self.data_mode in DATA_MODES else "Calibrated mV"
            ),
            raw_view_mode=(
                self.raw_view_mode if self.raw_view_mode in RAW_VIEW_MODES else "Absolute Code"
            ),
            raw_y_scale=_choice_or_default(
                self.raw_y_scale, RAW_Y_SCALES, 10000.0
            ),
            raw_grid_spacing=_choice_or_default(
                self.raw_grid_spacing, RAW_GRID_SPACINGS, 1000.0
            ),
        )

    @classmethod
    def from_qsettings(
        cls, qsettings: QtCore.QSettings | None = None
    ) -> "ViewerSettings":
        return load_viewer_settings(qsettings)

    def save(self, qsettings: QtCore.QSettings | None = None) -> None:
        save_viewer_settings(self, qsettings)


_KEYS = {
    "last_directory": "paths/last_directory",
    "paper_speed_mm_s": "display/paper_speed_mm_s",
    "gain_mm_per_mv": "display/gain_mm_per_mv",
    "time_window_s": "display/time_window_s",
    "y_range_mv": "display/y_range_mv",
    "lead_mode": "display/lead_mode",
    "grid_enabled": "display/grid_enabled",
    "crosshair_enabled": "display/crosshair_enabled",
    "display_scale_factor": "display/calibration_factor",
    "line_width": "display/line_width",
    "replay_speed": "replay/speed",
    "lock_view": "display/lock_view",
    "synchronize_y_ranges": "display/synchronize_y_ranges",
    "data_mode": "display/data_mode",
    "raw_view_mode": "display/raw_view_mode",
    "raw_y_scale": "display/raw_y_scale",
    "raw_grid_spacing": "display/raw_grid_spacing",
    "notch_50hz": "display/notch_50hz",
    "zero_origin": "display/zero_origin",
}


def make_qsettings() -> QtCore.QSettings:
    """Create the application's settings store."""

    return QtCore.QSettings(ORGANIZATION_NAME, APPLICATION_NAME)


def load_viewer_settings(
    qsettings: QtCore.QSettings | None = None,
) -> ViewerSettings:
    """Load viewer preferences, tolerating stale or malformed values."""

    store = qsettings or make_qsettings()
    defaults = ViewerSettings()
    loaded = ViewerSettings(
        last_directory=str(
            store.value(_KEYS["last_directory"], defaults.last_directory) or ""
        ),
        paper_speed_mm_s=_as_float(
            store.value(
                _KEYS["paper_speed_mm_s"], defaults.paper_speed_mm_s
            ),
            defaults.paper_speed_mm_s,
        ),
        gain_mm_per_mv=_as_float(
            store.value(_KEYS["gain_mm_per_mv"], defaults.gain_mm_per_mv),
            defaults.gain_mm_per_mv,
        ),
        time_window_s=_as_float(
            store.value(_KEYS["time_window_s"], defaults.time_window_s),
            defaults.time_window_s,
        ),
        y_range_mv=_as_float(
            store.value(_KEYS["y_range_mv"], defaults.y_range_mv),
            defaults.y_range_mv,
        ),
        lead_mode=str(store.value(_KEYS["lead_mode"], defaults.lead_mode)),
        grid_enabled=_as_bool(
            store.value(_KEYS["grid_enabled"], defaults.grid_enabled),
            defaults.grid_enabled,
        ),
        crosshair_enabled=_as_bool(
            store.value(
                _KEYS["crosshair_enabled"], defaults.crosshair_enabled
            ),
            defaults.crosshair_enabled,
        ),
        display_scale_factor=_as_float(
            store.value(
                _KEYS["display_scale_factor"], defaults.display_scale_factor
            ),
            defaults.display_scale_factor,
        ),
        line_width=_as_float(
            store.value(_KEYS["line_width"], defaults.line_width),
            defaults.line_width,
        ),
        replay_speed=_as_float(
            store.value(_KEYS["replay_speed"], defaults.replay_speed),
            defaults.replay_speed,
        ),
        lock_view=_as_bool(
            store.value(_KEYS["lock_view"], defaults.lock_view),
            defaults.lock_view,
        ),
        synchronize_y_ranges=_as_bool(
            store.value(
                _KEYS["synchronize_y_ranges"],
                defaults.synchronize_y_ranges,
            ),
            defaults.synchronize_y_ranges,
        ),
        data_mode=str(store.value(_KEYS["data_mode"], defaults.data_mode)),
        raw_view_mode=str(store.value(_KEYS["raw_view_mode"], defaults.raw_view_mode)),
        raw_y_scale=_as_float(
            store.value(_KEYS["raw_y_scale"], defaults.raw_y_scale),
            defaults.raw_y_scale,
        ),
        raw_grid_spacing=_as_float(
            store.value(_KEYS["raw_grid_spacing"], defaults.raw_grid_spacing),
            defaults.raw_grid_spacing,
        ),
        notch_50hz=_as_bool(
            store.value(_KEYS["notch_50hz"], defaults.notch_50hz),
            defaults.notch_50hz,
        ),
        zero_origin=_as_bool(
            store.value(_KEYS["zero_origin"], defaults.zero_origin),
            defaults.zero_origin,
        ),
    )
    return loaded.normalized()


def save_viewer_settings(
    settings: ViewerSettings,
    qsettings: QtCore.QSettings | None = None,
) -> None:
    """Persist presentation settings only; no frame or patient data."""

    store = qsettings or make_qsettings()
    normalized = settings.normalized()
    for field_name, key in _KEYS.items():
        store.setValue(key, getattr(normalized, field_name))
    store.sync()


def restore_window_geometry(
    window: QtWidgets.QWidget,
    qsettings: QtCore.QSettings | None = None,
) -> bool:
    """Restore saved window size and position, if present."""

    store = qsettings or make_qsettings()
    geometry = store.value("window/geometry")
    if geometry is None:
        return False
    try:
        return bool(window.restoreGeometry(geometry))
    except (TypeError, RuntimeError):
        return False


def save_window_geometry(
    window: QtWidgets.QWidget,
    qsettings: QtCore.QSettings | None = None,
) -> None:
    store = qsettings or make_qsettings()
    store.setValue("window/geometry", window.saveGeometry())
    store.sync()


class _VirtualRuler(QtWidgets.QWidget):
    """A nominal 50 mm ruler rendered from logical DPI and calibration."""

    NOMINAL_LENGTH_MM = 50.0

    def __init__(self, scale_factor: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self._scale_factor = float(scale_factor)
        self.setMinimumHeight(86)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )

    def set_scale_factor(self, factor: float) -> None:
        self._scale_factor = _finite_in_range(factor, 0.25, 4.0, 1.0)
        self.update()

    def _logical_dpi_x(self) -> float:
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        return float(screen.logicalDotsPerInchX()) if screen is not None else 96.0

    def ruler_pixels(self) -> float:
        return (
            self.NOMINAL_LENGTH_MM
            * self._logical_dpi_x()
            / 25.4
            * self._scale_factor
        )

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QtGui.QColor("#fffafa"))

        length = min(self.ruler_pixels(), max(1.0, self.width() - 32.0))
        x0 = (self.width() - length) / 2.0
        x1 = x0 + length
        y = 42.0
        pen = QtGui.QPen(QtGui.QColor("#3b2624"), 2.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(QtCore.QPointF(x0, y), QtCore.QPointF(x1, y))
        painter.drawLine(QtCore.QPointF(x0, y - 10), QtCore.QPointF(x0, y + 10))
        painter.drawLine(QtCore.QPointF(x1, y - 10), QtCore.QPointF(x1, y + 10))
        painter.drawText(
            QtCore.QRectF(x0, y + 13, length, 22),
            int(QtCore.Qt.AlignmentFlag.AlignCenter),
            "50 mm",
        )
        painter.end()


class DisplayCalibrationDialog(QtWidgets.QDialog):
    """Adjust logical-DPI-to-physical-size rendering without touching data."""

    scaleFactorChanged = QtCore.pyqtSignal(float)

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        qsettings: QtCore.QSettings | None = None,
        scale_factor: float | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Display Calibration")
        self.setModal(True)
        self.resize(650, 260)
        self._qsettings = qsettings or make_qsettings()
        if scale_factor is None:
            scale_factor = _as_float(
                self._qsettings.value("display/calibration_factor", 1.0), 1.0
            )
        scale_factor = _finite_in_range(scale_factor, 0.25, 4.0, 1.0)

        self._ruler = _VirtualRuler(scale_factor, self)
        self._factor = QtWidgets.QDoubleSpinBox(self)
        self._factor.setRange(0.25, 4.0)
        self._factor.setDecimals(3)
        self._factor.setSingleStep(0.01)
        self._factor.setValue(scale_factor)
        self._factor.setSuffix(" ×")
        self._factor.setToolTip(
            "Increase the factor if the virtual ruler is physically too short."
        )

        reset_button = QtWidgets.QPushButton("Reset to system DPI", self)
        reset_button.clicked.connect(self.reset_to_system_dpi)
        self._factor.valueChanged.connect(self._preview_factor)

        form = QtWidgets.QFormLayout()
        form.addRow("Display scale factor:", self._factor)
        form.addRow("", reset_button)

        warning = QtWidgets.QLabel(
            "Physical millimeter accuracy depends on monitor DPI and OS scaling.",
            self,
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #6f3b36; font-weight: 600;")

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(
            QtWidgets.QLabel(
                "Adjust the ruler until it measures 50 mm with a physical ruler.",
                self,
            )
        )
        layout.addWidget(self._ruler)
        layout.addLayout(form)
        layout.addWidget(warning)
        layout.addWidget(buttons)

    @property
    def scale_factor(self) -> float:
        return float(self._factor.value())

    def display_scale_factor(self) -> float:
        return self.scale_factor

    @QtCore.pyqtSlot()
    def reset_to_system_dpi(self) -> None:
        self._factor.setValue(1.0)

    @QtCore.pyqtSlot(float)
    def _preview_factor(self, factor: float) -> None:
        self._ruler.set_scale_factor(factor)
        self.scaleFactorChanged.emit(float(factor))

    def accept(self) -> None:
        self._qsettings.setValue(
            "display/calibration_factor", self.scale_factor
        )
        self._qsettings.sync()
        super().accept()


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _choice_or_default(
    value: Any, choices: tuple[float, ...], default: float
) -> float:
    number = _as_float(value, default)
    for choice in choices:
        if math.isclose(number, choice, rel_tol=0.0, abs_tol=1e-9):
            return choice
    return default


def _finite_in_range(
    value: Any, minimum: float, maximum: float, default: float
) -> float:
    number = _as_float(value, default)
    return number if minimum <= number <= maximum else default


# Short aliases make the helpers convenient from main_window.py.
load_settings = load_viewer_settings
save_settings = save_viewer_settings


__all__ = [
    "APPLICATION_NAME",
    "DATA_MODES",
    "DisplayCalibrationDialog",
    "GAINS_MM_MV",
    "LEAD_MODES",
    "ORGANIZATION_NAME",
    "PAPER_SPEEDS_MM_S",
    "RAW_GRID_SPACINGS",
    "RAW_VIEW_MODES",
    "RAW_Y_SCALES",
    "REPLAY_SPEEDS",
    "TIME_WINDOWS_S",
    "ViewerSettings",
    "Y_RANGES_MV",
    "load_settings",
    "load_viewer_settings",
    "make_qsettings",
    "restore_window_geometry",
    "save_settings",
    "save_viewer_settings",
    "save_window_geometry",
]
