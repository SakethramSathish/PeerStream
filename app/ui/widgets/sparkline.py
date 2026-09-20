"""A sparkline: a bounded series, drawn with no axes and no decoration.

The graph is fed a series of ``(timestamp, value)`` pairs already measured by
the statistics engine; it invents nothing, interpolates nothing, and refuses to
draw a line through a gap it does not have data for. Points older than the
window are dropped by the caller's bounded buffer, not here.

Hovering shows the exact sample under the pointer (PRD 11: hovering over graphs
reveals exact values), because a smooth curve is a summary and sometimes the
number is the point.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.ui.format import human_rate
from app.ui.theme.palette import DARK, Palette
from app.ui.theme.tokens import TOKENS

# A series shorter than this is noise: one sample is not a trend.
MIN_POINTS: int = 2


class Sparkline(QWidget):
    """A small rate graph with an area fill and a hover readout.

    Args:
        colour: Stroke colour; defaults to the theme accent.
        parent: Qt parent.
        height: Fixed height in pixels.
    """

    point_hovered = Signal(object)
    """Emitted with ``(timestamp, value)`` under the pointer, or ``None``."""

    def __init__(
        self,
        *,
        colour: str | None = None,
        parent: QWidget | None = None,
        height: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(height or TOKENS.chart.graph_height)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._colour = colour or DARK.accent
        self._series: tuple[tuple[float, float], ...] = ()
        self._hover: int | None = None

    # -------------------------------------------------------------------- data

    @property
    def series(self) -> tuple[tuple[float, float], ...]:
        """The samples currently drawn."""
        return self._series

    @property
    def empty(self) -> bool:
        """Whether there is anything to draw."""
        return len(self._series) < MIN_POINTS

    def set_series(self, points: Sequence[tuple[float, float]]) -> None:
        """Draw these samples. A copy is taken; the caller keeps its buffer."""
        self._series = tuple(points)
        self._hover = None
        self.update()

    def clear(self) -> None:
        """Forget the series."""
        self.set_series(())

    def set_colour(self, colour: str) -> None:
        self._colour = colour
        self.update()

    # ----------------------------------------------------------------- drawing

    def _geometry(self) -> tuple[float, float, float, float, float, float]:
        """``(left, top, width, height, minimum, maximum)`` of the plot area."""
        pad = TOKENS.spacing.xs
        width = max(1.0, self.width() - pad * 2)
        height = max(1.0, self.height() - pad * 2)
        values = [value for _stamp, value in self._series]
        maximum = max(values) if values else 0.0
        minimum = 0.0
        if maximum <= 0.0:
            # A flat zero still needs a scale, or the line sits on the axis and
            # looks like a rendering bug rather than an idle torrent.
            maximum = 1.0
        return pad, pad, width, height, minimum, maximum

    def _point_at(self, index: int) -> QPointF:
        left, top, width, height, minimum, maximum = self._geometry()
        stamp, value = self._series[index]
        first = self._series[0][0]
        last = self._series[-1][0]
        span = max(1e-9, last - first)
        x = left + (stamp - first) / span * width
        y = top + height - (value - minimum) / max(1e-9, maximum - minimum) * height
        return QPointF(x, y)

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        left, top, width, height, _minimum, _maximum = self._geometry()
        palette: Palette = DARK

        if self.empty:
            painter.setPen(QPen(QColor(palette.text_faint)))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "waiting for data" if not self._series else "one sample so far",
            )
            painter.end()
            return

        points = [self._point_at(index) for index in range(len(self._series))]

        # Area fill: the same colour at low alpha, so the shape reads at a
        # glance without competing with the stroke.
        fill = QColor(self._colour)
        fill.setAlpha(46)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        from PySide6.QtGui import QPolygonF

        polygon = QPolygonF(points)
        polygon.append(QPointF(points[-1].x(), top + height))
        polygon.append(QPointF(points[0].x(), top + height))
        painter.drawPolygon(polygon)

        pen = QPen(QColor(self._colour))
        pen.setWidthF(1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for first, second in pairwise(points):
            painter.drawLine(first, second)

        if self._hover is not None and 0 <= self._hover < len(points):
            spot = points[self._hover]
            painter.setPen(QPen(QColor(palette.text)))
            painter.setBrush(QColor(self._colour))
            painter.drawEllipse(spot, 3.0, 3.0)

        _ = (left, width)
        painter.end()

    # ------------------------------------------------------------------ hover

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.empty:
            self._hover = None
            return
        position = event.position().x()
        nearest = min(
            range(len(self._series)), key=lambda index: abs(self._point_at(index).x() - position)
        )
        if self._point_at(nearest).x() - position > TOKENS.spacing.xl:
            self._on_hover(None)
            return
        self._on_hover(nearest)

    def leaveEvent(self, event: object) -> None:
        self._on_hover(None)

    def _on_hover(self, index: int | None) -> None:
        if index == self._hover:
            return
        self._hover = index
        self.setToolTip("" if index is None else _describe(self._series[index]))
        self.point_hovered.emit(None if index is None else self._series[index])
        self.update()

    @property
    def hovered(self) -> tuple[float, float] | None:
        """The sample under the pointer, for tests."""
        return None if self._hover is None else self._series[self._hover]


def _describe(sample: tuple[float, float]) -> str:
    """A tooltip for one sample: the exact value, at an exact moment."""
    stamp, value = sample
    return f"{human_rate(value)}   t+{stamp - sample[0]:.1f}s"
