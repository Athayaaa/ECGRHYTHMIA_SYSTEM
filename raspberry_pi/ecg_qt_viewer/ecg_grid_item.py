"""Medical ECG-paper grid drawn directly in plot data coordinates."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterator

from PyQt6 import QtCore, QtGui
import pyqtgraph as pg


_HORIZONTAL_SPACING = {
    25.0: (0.04, 0.20),
    50.0: (0.02, 0.10),
}
_VERTICAL_SPACING = {
    5.0: (0.20, 1.00),
    10.0: (0.10, 0.50),
    20.0: (0.05, 0.25),
}


@dataclass(frozen=True, slots=True)
class GridSpacing:
    """Minor and major ECG-grid intervals in data units."""

    minor_horizontal_s: float
    major_horizontal_s: float
    minor_vertical_mv: float
    major_vertical_mv: float

    # Coordinate-style aliases are useful in plotting code and tests.
    @property
    def minor_x(self) -> float:
        return self.minor_horizontal_s

    @property
    def major_x(self) -> float:
        return self.major_horizontal_s

    @property
    def minor_y(self) -> float:
        return self.minor_vertical_mv

    @property
    def major_y(self) -> float:
        return self.major_vertical_mv

    @property
    def minor_time_s(self) -> float:
        return self.minor_horizontal_s

    @property
    def major_time_s(self) -> float:
        return self.major_horizontal_s

    def as_dict(self) -> dict[str, float]:
        return {
            "minor_horizontal_s": self.minor_horizontal_s,
            "major_horizontal_s": self.major_horizontal_s,
            "minor_vertical_mv": self.minor_vertical_mv,
            "major_vertical_mv": self.major_vertical_mv,
        }

    def __getitem__(self, key: str) -> float:
        aliases = {
            "minor_x": "minor_horizontal_s",
            "major_x": "major_horizontal_s",
            "minor_y": "minor_vertical_mv",
            "major_y": "major_vertical_mv",
            "minor_time_s": "minor_horizontal_s",
            "major_time_s": "major_horizontal_s",
        }
        return float(getattr(self, aliases.get(key, key)))


def calculate_grid_spacing(
    paper_speed_mm_s: float = 25.0,
    gain_mm_mv: float = 10.0,
    **kwargs: float,
) -> GridSpacing:
    """Map paper speed and gain to standard ECG-grid data intervals.

    This function has no GUI state, so grid calibration can be unit tested
    without constructing a QApplication.
    """

    if "gain_mm_per_mv" in kwargs:
        gain_mm_mv = kwargs.pop("gain_mm_per_mv")
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError(f"unexpected grid-spacing argument(s): {unknown}")

    speed = _supported_value(paper_speed_mm_s, _HORIZONTAL_SPACING, "speed")
    gain = _supported_value(gain_mm_mv, _VERTICAL_SPACING, "gain")
    minor_x, major_x = _HORIZONTAL_SPACING[speed]
    minor_y, major_y = _VERTICAL_SPACING[gain]
    return GridSpacing(minor_x, major_x, minor_y, major_y)


def grid_line_values(
    lower: float,
    upper: float,
    spacing: float,
    *,
    maximum_lines: int = 5_000,
) -> tuple[float, ...]:
    """Return only grid values intersecting a visible data interval.

    For an extremely zoomed-out view the stride is increased to protect the
    GUI thread.  Ordinary clinical ranges remain unmodified.
    """

    if not all(math.isfinite(value) for value in (lower, upper, spacing)):
        return ()
    if spacing <= 0.0 or maximum_lines < 1:
        return ()
    if lower > upper:
        lower, upper = upper, lower

    first = math.ceil((lower / spacing) - 1e-12)
    last = math.floor((upper / spacing) + 1e-12)
    if last < first:
        return ()
    count = last - first + 1
    stride = max(1, math.ceil(count / maximum_lines))
    return tuple((index * spacing) for index in range(first, last + 1, stride))


class ECGGridItem(pg.GraphicsObject):
    """Fast minor/major ECG grid kept behind plot curves.

    The item caches the ViewBox's visible rectangle as its bounding rectangle,
    so its painter never creates off-screen ECG-paper lines.
    """

    spacingChanged = QtCore.pyqtSignal(object)

    MINOR_COLOR = QtGui.QColor("#f6d8dc")
    MAJOR_COLOR = QtGui.QColor("#dfa3aa")
    ZERO_COLOR = QtGui.QColor("#b86d77")

    def __init__(
        self,
        paper_speed_mm_s: float = 25.0,
        gain_mm_mv: float = 10.0,
        *,
        view_box: pg.ViewBox | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._paper_speed_mm_s = float(paper_speed_mm_s)
        self._gain_mm_mv = float(gain_mm_mv)
        self._spacing = calculate_grid_spacing(
            self._paper_speed_mm_s, self._gain_mm_mv
        )
        self._display_scale_factor = 1.0
        self._visible_rect = QtCore.QRectF(0.0, -2.0, 10.0, 4.0)
        self._view_box: pg.ViewBox | None = None
        self.setZValue(-1_000.0)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        if view_box is not None:
            self.attach_to_viewbox(view_box)

    @property
    def spacing(self) -> GridSpacing:
        return self._spacing

    @property
    def paper_speed_mm_s(self) -> float:
        return self._paper_speed_mm_s

    @property
    def gain_mm_mv(self) -> float:
        return self._gain_mm_mv

    def set_paper_speed(self, paper_speed_mm_s: float) -> None:
        spacing = calculate_grid_spacing(
            paper_speed_mm_s, self._gain_mm_mv
        )
        self._paper_speed_mm_s = _supported_value(
            paper_speed_mm_s, _HORIZONTAL_SPACING, "speed"
        )
        self._spacing = spacing
        self.spacingChanged.emit(spacing)
        self.update()

    set_speed = set_paper_speed

    def set_gain(self, gain_mm_mv: float) -> None:
        spacing = calculate_grid_spacing(
            self._paper_speed_mm_s, gain_mm_mv
        )
        self._gain_mm_mv = _supported_value(
            gain_mm_mv, _VERTICAL_SPACING, "gain"
        )
        self._spacing = spacing
        self.spacingChanged.emit(spacing)
        self.update()

    def set_display_scale_factor(self, factor: float) -> None:
        number = float(factor)
        if not math.isfinite(number) or number <= 0.0:
            raise ValueError("display scale factor must be finite and positive")
        self._display_scale_factor = number
        self.update()

    def attach_to_viewbox(self, view_box: pg.ViewBox) -> None:
        if view_box is self._view_box:
            self.update_from_viewbox()
            return
        if self._view_box is not None:
            try:
                self._view_box.sigRangeChanged.disconnect(
                    self._on_view_range_changed
                )
            except (TypeError, RuntimeError):
                pass
        self._view_box = view_box
        view_box.sigRangeChanged.connect(self._on_view_range_changed)
        self.update_from_viewbox()

    def detach_from_viewbox(self) -> None:
        if self._view_box is not None:
            try:
                self._view_box.sigRangeChanged.disconnect(
                    self._on_view_range_changed
                )
            except (TypeError, RuntimeError):
                pass
        self._view_box = None

    @QtCore.pyqtSlot(object, object)
    def _on_view_range_changed(self, _view_box, ranges) -> None:
        try:
            self.set_visible_range(ranges[0], ranges[1])
        except (IndexError, TypeError, ValueError):
            self.update_from_viewbox()

    def update_from_viewbox(self) -> None:
        if self._view_box is None:
            return
        ranges = self._view_box.viewRange()
        self.set_visible_range(ranges[0], ranges[1])

    def set_visible_range(
        self,
        x_range: tuple[float, float] | list[float],
        y_range: tuple[float, float] | list[float],
    ) -> None:
        x0, x1 = sorted((float(x_range[0]), float(x_range[1])))
        y0, y1 = sorted((float(y_range[0]), float(y_range[1])))
        new_rect = QtCore.QRectF(x0, y0, x1 - x0, y1 - y0)
        if new_rect == self._visible_rect:
            self.update()
            return
        self.prepareGeometryChange()
        self._visible_rect = new_rect
        self.update()

    def boundingRect(self) -> QtCore.QRectF:  # noqa: N802
        return QtCore.QRectF(self._visible_rect)

    def paint(self, painter, option, widget=None) -> None:
        del option, widget
        rect = self._visible_rect.normalized()
        if rect.isEmpty():
            return

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        painter.setClipRect(rect)

        minor_x = grid_line_values(
            rect.left(), rect.right(), self._spacing.minor_horizontal_s
        )
        minor_y = grid_line_values(
            rect.top(), rect.bottom(), self._spacing.minor_vertical_mv
        )
        major_x = grid_line_values(
            rect.left(), rect.right(), self._spacing.major_horizontal_s
        )
        major_y = grid_line_values(
            rect.top(), rect.bottom(), self._spacing.major_vertical_mv
        )

        painter.setPen(self._pen(self.MINOR_COLOR, 0.45))
        self._draw_vertical_lines(
            painter,
            (
                x
                for x in minor_x
                if not _is_grid_multiple(x, self._spacing.major_horizontal_s)
            ),
            rect,
        )
        self._draw_horizontal_lines(
            painter,
            (
                y
                for y in minor_y
                if not _is_grid_multiple(y, self._spacing.major_vertical_mv)
            ),
            rect,
        )

        painter.setPen(self._pen(self.MAJOR_COLOR, 0.85))
        self._draw_vertical_lines(painter, major_x, rect)
        self._draw_horizontal_lines(
            painter, (y for y in major_y if not math.isclose(y, 0.0)), rect
        )

        if rect.top() <= 0.0 <= rect.bottom():
            painter.setPen(self._pen(self.ZERO_COLOR, 1.35))
            painter.drawLine(
                QtCore.QLineF(rect.left(), 0.0, rect.right(), 0.0)
            )
        painter.restore()

    def _pen(self, color: QtGui.QColor, width: float) -> QtGui.QPen:
        pen = QtGui.QPen(color)
        pen.setCosmetic(True)
        pen.setWidthF(max(0.1, width * self._display_scale_factor))
        return pen

    @staticmethod
    def _draw_vertical_lines(
        painter: QtGui.QPainter,
        values: Iterator[float] | tuple[float, ...],
        rect: QtCore.QRectF,
    ) -> None:
        lines = [
            QtCore.QLineF(value, rect.top(), value, rect.bottom())
            for value in values
        ]
        if lines:
            painter.drawLines(lines)

    @staticmethod
    def _draw_horizontal_lines(
        painter: QtGui.QPainter,
        values: Iterator[float] | tuple[float, ...],
        rect: QtCore.QRectF,
    ) -> None:
        lines = [
            QtCore.QLineF(rect.left(), value, rect.right(), value)
            for value in values
        ]
        if lines:
            painter.drawLines(lines)


def _supported_value(
    value: float, mapping: dict[float, tuple[float, float]], label: str
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported ECG {label}: {value!r}") from exc
    for supported in mapping:
        if math.isclose(number, supported, rel_tol=0.0, abs_tol=1e-9):
            return supported
    choices = ", ".join(f"{item:g}" for item in mapping)
    raise ValueError(f"unsupported ECG {label} {number:g}; expected {choices}")


def _is_grid_multiple(value: float, spacing: float) -> bool:
    nearest = round(value / spacing)
    return math.isclose(
        value, nearest * spacing, rel_tol=0.0, abs_tol=spacing * 1e-8
    )


# Public aliases used by tests/UI variants.
get_grid_spacing = calculate_grid_spacing
grid_spacing_for = calculate_grid_spacing


__all__ = [
    "ECGGridItem",
    "GridSpacing",
    "calculate_grid_spacing",
    "get_grid_spacing",
    "grid_line_values",
    "grid_spacing_for",
]
