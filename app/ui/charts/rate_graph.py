"""The rate graph (PRD §37 / TRD §37): throughput over the last minute and a half.

Two series — downloaded, uploaded — drawn as a filled area and a line over a
grid, with the current values and a peak in the corner and a crosshair that
reads out exact numbers where the pointer is.

The rules it keeps, which are the difference between a graph and a decoration:

* **The buffer is bounded.** 180 samples, one every 500 ms, oldest dropped. A
  graph of a long session does not grow into a graph of the whole session; it
  stays a window.
* **The y axis is honest.** It is scaled to the largest value actually in the
  buffer, rounded up to a clean binary unit, and that number is printed. A
  graph autoscaled to zero would draw noise as a mountain range.
* **Unknown is not zero.** Before the first two samples there is nothing to
  plot, and the control says so instead of drawing a flat line at zero, which
  would read as "downloading at 0 B/s" — a different claim.
* **The peak is remembered, not extrapolated.** It is the largest rate in the
  buffer; when it scrolls off the end, it goes down again.

The series mapping (samples → points in widget space) is a pure function,
:func:`map_series`, so it can be tested without a window.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import (
    QPointF,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui.format import UNKNOWN, human_duration, human_rate
from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS, Tokens

# Left gutter for the axis labels, and the space kept for the header line.
AXIS_WIDTH: float = 56.0
HEADER_HEIGHT: float = 20.0
FOOTER_HEIGHT: float = 16.0

# How many horizontal gridlines to draw.
GRID_LINES: int = 4

# Values above this many units are rounded up to a clean multiple of a binary
# unit, so the axis reads "8 MiB/s" rather than "7.63 MiB/s".
_UNITS: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024)


@dataclass(frozen=True, slots=True)
class RateSeries:
    """One series, mapped onto the plot area.

    Attributes:
        name: ``"download"`` or ``"upload"``.
        points: Widget-space points, oldest first.
        runs: Indices where a new line starts. More than one run means the
            recording has a hole in it — the client was paused, or stalled for
            longer than a few samples — and the gap is drawn as a gap rather
            than as a slope.
        current: The newest value, or ``None`` if nothing has been measured.
        peak: The largest value in the buffer.
        colour: The role colour to draw it in.
        span: Seconds covered by the plotted samples.
    """

    name: str
    points: tuple[QPointF, ...]
    runs: tuple[int, ...]
    current: float | None
    peak: float
    colour: str
    span: float = 0.0

    @property
    def plotted(self) -> bool:
        """Whether there is anything to draw."""
        return len(self.points) >= 2

    @property
    def continuous(self) -> bool:
        """Whether the series is one unbroken line."""
        return len(self.runs) <= 1


class RateGraph(QWidget):
    """A throughput graph with a crosshair.

    Args:
        palette: Colours.
        design: Type sizes and chart geometry.
        parent: Qt parent.
    """

    hovered = Signal(object)
    """The hovered ``(seconds_ago, download, upload)``, or ``None``."""

    def __init__(
        self,
        palette: Palette,
        design: Tokens | None = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._design = design or TOKENS
        self._download: tuple[tuple[float, float], ...] = ()
        self._upload: tuple[tuple[float, float], ...] = ()
        self._hover: float | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------- input

    def set_series(
        self,
        download: Sequence[tuple[float, float]],
        upload: Sequence[tuple[float, float]] | None = None,
    ) -> None:
        """Plot these series. Each sample is ``(monotonic seconds, bytes/s)``."""
        self._download = tuple(download)
        self._upload = tuple(upload or ())
        self.update()

    def clear(self) -> None:
        """Forget both series."""
        self._download = ()
        self._upload = ()
        self._hover = None
        self.update()

    def set_palette(self, palette: Palette) -> None:
        """Recolour, for when the theme changes."""
        self._palette = palette
        self.update()

    # ------------------------------------------------------------------ layout

    def plot_rect(self) -> QRectF:
        """The area the curves are drawn in, leaving room for labels."""
        left = AXIS_WIDTH
        top = HEADER_HEIGHT
        right = float(self.width()) - 8.0
        bottom = float(self.height()) - FOOTER_HEIGHT
        return QRectF(left, top, max(1.0, right - left), max(1.0, bottom - top))

    def series(self) -> tuple[RateSeries, RateSeries]:
        """Both series, mapped into widget space. This is what tests read."""
        area = self.plot_rect()
        scale = self._scale()
        return (
            self._map(self._download, "download", self._palette.accent, area, scale),
            self._map(self._upload, "upload", self._palette.success, area, scale),
        )

    def _scale(self) -> float:
        """The y-axis maximum in bytes per second, rounded up to a clean unit."""
        values = [value for _, value in self._download] + [value for _, value in self._upload]
        biggest = max(values, default=0.0)
        return nice_ceiling(biggest)

    def _map(
        self,
        samples: tuple[tuple[float, float], ...],
        name: str,
        colour: str,
        area: QRectF,
        scale: float,
    ) -> RateSeries:
        return map_series(
            samples,
            name=name,
            colour=colour,
            area=area,
            scale=scale,
        )

    # ----------------------------------------------------------------- drawing

    def sizeHint(self) -> QSize:
        """A graph is wide, and as tall as the token says."""
        height = int(self._design.chart.graph_height + HEADER_HEIGHT + FOOTER_HEIGHT)
        return QSize(520, height)

    def minimumSizeHint(self) -> QSize:
        return QSize(220, 96)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw the header, the grid, the curves, and the crosshair."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        area = self.plot_rect()
        download, upload = self.series()

        self._paint_header(painter, download, upload)
        self._paint_grid(painter, area, self._scale())
        self._paint_axis(painter, area, download)
        if download.plotted:
            self._paint_area(painter, download, area)
        if upload.plotted:
            self._paint_line(painter, upload)
        if not download.plotted and not upload.plotted:
            self._paint_waiting(painter, area)
        if self._hover is not None and download.plotted:
            self._paint_crosshair(painter, area, download, upload)

    def _paint_header(self, painter: QPainter, download: RateSeries, upload: RateSeries) -> None:
        """The two current values and the peak, in words."""
        palette = self._palette
        font = QFont(self.font())
        font.setPixelSize(max(9, self._design.type.body - 2))
        painter.setFont(font)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette.accent))
        painter.drawRect(QRectF(AXIS_WIDTH - 12.0, 6.0, 7.0, 7.0))
        painter.setPen(QColor(palette.text_muted))
        painter.drawText(
            QRectF(AXIS_WIDTH, 1.0, 150.0, 16.0),
            Qt.AlignmentFlag.AlignVCenter,
            f"down {human_rate(download.current)}",
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette.success))
        painter.drawRect(QRectF(AXIS_WIDTH + 96.0, 6.0, 7.0, 7.0))
        painter.setPen(QColor(palette.text_muted))
        painter.drawText(
            QRectF(AXIS_WIDTH + 108.0, 1.0, 150.0, 16.0),
            Qt.AlignmentFlag.AlignVCenter,
            f"up {human_rate(upload.current)}",
        )

        peak = max(download.peak, upload.peak)
        painter.setPen(QColor(palette.text_faint))
        painter.drawText(
            QRectF(float(self.width()) - 160.0, 1.0, 152.0, 16.0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"peak {human_rate(peak)}",
        )

    def _paint_grid(self, painter: QPainter, area: QRectF, scale: float) -> None:
        """Gridlines with a stated value beside each one."""
        palette = self._palette
        font = QFont(self.font())
        font.setPixelSize(max(8, self._design.type.body - 4))
        painter.setFont(font)
        pen = QPen(QColor(palette.graph_grid), 1.0, Qt.PenStyle.SolidLine)
        for step in range(GRID_LINES + 1):
            fraction = step / GRID_LINES
            y = area.bottom() - fraction * area.height()
            painter.setPen(pen)
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(QColor(palette.text_faint))
            painter.drawText(
                QRectF(0.0, y - 7.0, AXIS_WIDTH - 6.0, 14.0),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                human_rate(scale * fraction),
            )

    def _paint_area(self, painter: QPainter, series: RateSeries, area: QRectF) -> None:
        """The download series as a filled area under a line."""
        palette = self._palette
        for run in _runs_of(series):
            polygon = QPolygonF(list(run))
            polygon.append(QPointF(run[-1].x(), area.bottom()))
            polygon.append(QPointF(run[0].x(), area.bottom()))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette.graph_fill))
            painter.drawPolygon(polygon)

        painter.setPen(QPen(QColor(palette.accent), 1.8, Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for run in _runs_of(series):
            painter.drawPolyline(QPolygonF(list(run)))

    def _paint_line(self, painter: QPainter, series: RateSeries) -> None:
        """The upload series as a line."""
        painter.setPen(QPen(QColor(series.colour), 1.6, Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for run in _runs_of(series):
            painter.drawPolyline(QPolygonF(list(run)))

    def _paint_axis(self, painter: QPainter, area: QRectF, download: RateSeries) -> None:
        """How far back the window reaches, and that the right edge is now.

        Without it a graph is a picture with no scale on the axis that matters
        most, and five minutes looks exactly like five seconds.
        """
        palette = self._palette
        font = QFont(self.font())
        font.setPixelSize(max(8, self._design.type.body - 4))
        painter.setFont(font)
        painter.setPen(QColor(palette.text_faint))
        painter.drawText(
            QRectF(area.left(), area.bottom() + 1.0, 90.0, 14.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"-{human_duration(download.span) if download.plotted else UNKNOWN}",
        )
        painter.drawText(
            QRectF(area.right() - 60.0, area.bottom() + 1.0, 60.0, 14.0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "now",
        )

    def _paint_waiting(self, painter: QPainter, area: QRectF) -> None:
        """Nothing measured yet: say so, rather than drawing a zero line."""
        palette = self._palette
        font = QFont(self.font())
        font.setPixelSize(max(9, self._design.type.body - 1))
        painter.setFont(font)
        painter.setPen(QColor(palette.text_faint))
        painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Waiting for measurements")

    def _paint_crosshair(
        self,
        painter: QPainter,
        area: QRectF,
        download: RateSeries,
        upload: RateSeries,
    ) -> None:
        """A vertical line at the pointer, with both values read out."""
        palette = self._palette
        if self._hover is None:
            return
        x = min(max(self._hover, area.left()), area.right())
        painter.setPen(
            QPen(QColor(palette.with_alpha(palette.text, 90)), 1.0, Qt.PenStyle.DashLine)
        )
        painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))

        index = _nearest_index(download.points, x)
        label = (
            f"{human_rate(download.current if index is None else _value_at(self._download, index))}"
        )
        if len(self._upload) >= 2:
            up = _value_at(self._upload, min(index or 0, len(self._upload) - 1))
            label += f" · {human_rate(up)}"
        font = QFont(self.font())
        font.setPixelSize(max(8, self._design.type.body - 3))
        painter.setFont(font)
        width = float(painter.fontMetrics().horizontalAdvance(label)) + 10.0
        left = min(max(x + 6.0, area.left()), area.right() - width)
        box = QRectF(left, area.top() + 2.0, width, 16.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette.with_alpha(palette.surface_alt, 235)))
        painter.drawRoundedRect(box, 3.0, 3.0)
        painter.setPen(QColor(palette.text))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    # ------------------------------------------------------------------ events

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Move the crosshair and report what it is on."""
        area = self.plot_rect()
        position = event.position().x()
        inside = area.left() <= position <= area.right()
        self._hover = position if inside else None
        if inside and self._download:
            index = _nearest_index(self.series()[0].points, position)
            self.hovered.emit(
                None
                if index is None
                else (
                    _seconds_ago(self._download, index),
                    _value_at(self._download, index),
                    _value_at(self._upload, min(index, max(0, len(self._upload) - 1)))
                    if self._upload
                    else None,
                )
            )
        else:
            self.hovered.emit(None)
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: object) -> None:
        """Drop the crosshair when the pointer leaves."""
        self._hover = None
        self.hovered.emit(None)
        self.update()


# ---------------------------------------------------------------------- mapping


def nice_ceiling(value: float) -> float:
    """Round a rate up to the next clean binary value, for the axis.

    ``1.9 MiB/s`` becomes ``2 MiB/s``; ``3.1 MiB/s`` becomes ``4 MiB/s``. The
    axis is labelled with the rounded number, so the top gridline is never a
    value the graph is hiding.
    """
    if value <= 0.0:
        return 1024.0**2  # An empty graph is still scaled in MiB/s, not in bytes.
    unit = 1024.0 ** _magnitude(value)
    scaled = value / unit
    for candidate in _UNITS:
        if scaled <= candidate:
            return candidate * unit
    return _UNITS[-1] * unit


def _magnitude(value: float) -> int:
    """Which binary unit a value belongs to: 0 for bytes, 1 for KiB, ..."""
    magnitude = 0
    while value >= 1024.0 and magnitude < 6:
        value /= 1024.0
        magnitude += 1
    return magnitude


def map_series(
    samples: Sequence[tuple[float, float]],
    *,
    name: str,
    colour: str,
    area: QRectF,
    scale: float,
) -> RateSeries:
    """Map a series of ``(seconds, value)`` onto the plot area.

    The x axis is time, running right to left from the newest sample to the
    oldest, over the span the samples actually cover — not a fixed window, so a
    graph that has only been running for five seconds is not squashed into a
    corner of a ninety second grid.

    Args:
        samples: ``(monotonic seconds, value)`` pairs, oldest first.
        name: Series name.
        colour: Role colour to draw it in.
        area: The plot rectangle.
        scale: The y-axis maximum.

    Returns:
        The mapped series, with its current value and peak.
    """
    if len(samples) < 2 or area.width() <= 0 or scale <= 0:
        current = samples[-1][1] if samples else None
        return RateSeries(name=name, points=(), runs=(), current=current, peak=0.0, colour=colour)

    first = samples[0][0]
    last = samples[-1][0]
    span = max(1e-6, last - first)
    points: list[QPointF] = []
    runs: list[int] = [0]
    previous: float | None = None
    gap = _gap_limit(samples)
    for index, (seconds, value) in enumerate(samples):
        if previous is not None and seconds - previous > gap:
            # A hole in the recording. Joining the two sides would draw a rate
            # that was never measured, so the line starts again instead.
            runs.append(index)
        x = area.left() + area.width() * ((seconds - first) / span)
        y = area.bottom() - area.height() * min(1.0, max(0.0, value / scale))
        points.append(QPointF(x, y))
        previous = seconds
    values = [value for _, value in samples]
    return RateSeries(
        name=name,
        points=tuple(points),
        runs=tuple(runs),
        current=values[-1],
        peak=max(values),
        colour=colour,
        span=span,
    )


def _gap_limit(samples: Sequence[tuple[float, float]]) -> float:
    """How long a hole has to be before the line refuses to cross it.

    Three times the usual spacing between samples, by token. Anything less is
    jitter in when the samples were taken, not a gap in what was measured.
    """
    if len(samples) < 3:
        return float("inf")
    spacing = [samples[index + 1][0] - samples[index][0] for index in range(len(samples) - 1)]
    typical = sorted(spacing)[len(spacing) // 2]
    return max(typical, 1e-6) * TOKENS.chart.max_gap_factor


def _runs_of(series: RateSeries) -> tuple[tuple[QPointF, ...], ...]:
    """Split a series into the stretches that were actually recorded."""
    if not series.points:
        return ()
    bounds = [*series.runs, len(series.points)]
    runs: list[tuple[QPointF, ...]] = []
    for start, end in itertools.pairwise(bounds):
        run = series.points[start:end]
        if len(run) >= 2:
            runs.append(run)
    return tuple(runs)


def _nearest_index(points: Sequence[QPointF], x: float) -> int | None:
    """The index of the sample nearest an x coordinate."""
    if not points:
        return None
    best = 0
    best_gap = abs(points[0].x() - x)
    for index in range(1, len(points)):
        gap = abs(points[index].x() - x)
        if gap < best_gap:
            best, best_gap = index, gap
    return best


def _value_at(samples: tuple[tuple[float, float], ...], index: int) -> float | None:
    """The value of one sample, or ``None`` if there is no such sample."""
    if not samples:
        return None
    return samples[min(max(0, index), len(samples) - 1)][1]


def _seconds_ago(samples: tuple[tuple[float, float], ...], index: int) -> float:
    """How long ago a sample was taken, in seconds."""
    if not samples:
        return 0.0
    return max(0.0, samples[-1][0] - samples[min(max(0, index), len(samples) - 1)][0])
