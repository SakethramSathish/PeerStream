"""Icons drawn in code, so the application ships without an asset pipeline.

Every icon is a few QPainter primitives: a grid for the command centre, three
nodes for peers, a terminal prompt for logs. Monochrome by design — colour is
reserved for data, and a multicoloured icon set would compete with the charts.

Each icon is cached per ``(name, size, colour)`` because a table row asks for
the same glyph hundreds of times.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap

from app.ui.theme.palette import DARK

Drawer = Callable[[QPainter, float, QColor], None]

_SIZE: Final[float] = 24.0  # Icons are drawn on a 24-point canvas, then scaled.


def _stroke(painter: QPainter, colour: QColor, width: float = 1.6) -> QPen:
    pen = QPen(colour)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    return pen


def _draw_overview(painter: QPainter, size: float, colour: QColor) -> None:
    """Four panes: the command centre."""
    _stroke(painter, colour)
    unit = size / _SIZE
    outer = QRectF(3 * unit, 3 * unit, 18 * unit, 18 * unit)
    painter.drawRoundedRect(outer, 3 * unit, 3 * unit)
    painter.drawLine(
        QPointF(3 * unit, 9 * unit),
        QPointF(21 * unit, 9 * unit),
    )
    painter.drawLine(
        QPointF(12 * unit, 9 * unit),
        QPointF(12 * unit, 21 * unit),
    )


def _draw_library(painter: QPainter, size: float, colour: QColor) -> None:
    """Three stacked rows: the torrent list."""
    _stroke(painter, colour)
    unit = size / _SIZE
    for row in range(3):
        top = (5 + row * 5) * unit
        painter.drawRoundedRect(QRectF(4 * unit, top, 16 * unit, 3 * unit), 1.2 * unit, 1.2 * unit)


def _draw_peers(painter: QPainter, size: float, colour: QColor) -> None:
    """Three nodes joined by two edges: the swarm."""
    _stroke(painter, colour)
    unit = size / _SIZE
    painter.setBrush(QBrush(colour))
    for centre in ((6, 7), (18, 7), (12, 17)):
        painter.drawEllipse(QPointF(centre[0] * unit, centre[1] * unit), 2.4 * unit, 2.4 * unit)
    painter.drawLine(QPointF(6 * unit, 7 * unit), QPointF(18 * unit, 7 * unit))
    painter.drawLine(QPointF(12 * unit, 7 * unit), QPointF(12 * unit, 17 * unit))


def _draw_network(painter: QPainter, size: float, colour: QColor) -> None:
    """A signal rising to the right: throughput."""
    _stroke(painter, colour)
    unit = size / _SIZE
    heights = (5, 9, 13)
    for index, height in enumerate(heights):
        left = (7 + index * 4) * unit
        painter.drawRoundedRect(
            QRectF(left, (18 - height) * unit, 2.2 * unit, height * unit), 1 * unit, 1 * unit
        )


def _draw_dht(painter: QPainter, size: float, colour: QColor) -> None:
    """Concentric rings: a routing table."""
    _stroke(painter, colour)
    unit = size / _SIZE
    centre = QPointF(12 * unit, 12 * unit)
    painter.drawEllipse(centre, 8.5 * unit, 8.5 * unit)
    painter.drawEllipse(centre, 4.8 * unit, 4.8 * unit)
    painter.setBrush(QBrush(colour))
    painter.drawEllipse(centre, 1.6 * unit, 1.6 * unit)


def _draw_logs(painter: QPainter, size: float, colour: QColor) -> None:
    """A prompt and a cursor: the protocol log."""
    _stroke(painter, colour)
    unit = size / _SIZE
    painter.drawRoundedRect(QRectF(3 * unit, 4 * unit, 18 * unit, 16 * unit), 3 * unit, 3 * unit)
    painter.drawLine(QPointF(7 * unit, 10 * unit), QPointF(10 * unit, 12.5 * unit))
    painter.drawLine(QPointF(10 * unit, 12.5 * unit), QPointF(7 * unit, 15 * unit))
    painter.drawLine(QPointF(12 * unit, 15 * unit), QPointF(17 * unit, 15 * unit))


def _draw_settings(painter: QPainter, size: float, colour: QColor) -> None:
    """Two sliders: preferences."""
    _stroke(painter, colour)
    unit = size / _SIZE
    for row, offset in ((9, 4), (15, -4)):
        painter.drawLine(QPointF(4 * unit, row * unit), QPointF(20 * unit, row * unit))
        painter.setBrush(QBrush(colour))
        painter.drawEllipse(QPointF((12 + offset) * unit, row * unit), 2.2 * unit, 2.2 * unit)
        painter.setBrush(Qt.BrushStyle.NoBrush)


def _draw_plus(painter: QPainter, size: float, colour: QColor) -> None:
    _stroke(painter, colour, 1.8)
    unit = size / _SIZE
    painter.drawLine(QPointF(12 * unit, 6 * unit), QPointF(12 * unit, 18 * unit))
    painter.drawLine(QPointF(6 * unit, 12 * unit), QPointF(18 * unit, 12 * unit))


def _draw_play(painter: QPainter, size: float, colour: QColor) -> None:
    unit = size / _SIZE
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(colour))
    painter.drawPolygon(
        [QPointF(8 * unit, 6 * unit), QPointF(18 * unit, 12 * unit), QPointF(8 * unit, 18 * unit)]
    )


def _draw_pause(painter: QPainter, size: float, colour: QColor) -> None:
    unit = size / _SIZE
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(colour))
    painter.drawRoundedRect(QRectF(8 * unit, 6 * unit, 2.6 * unit, 12 * unit), 1 * unit, 1 * unit)
    painter.drawRoundedRect(
        QRectF(13.4 * unit, 6 * unit, 2.6 * unit, 12 * unit), 1 * unit, 1 * unit
    )


def _draw_stop(painter: QPainter, size: float, colour: QColor) -> None:
    unit = size / _SIZE
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(colour))
    painter.drawRoundedRect(QRectF(7 * unit, 7 * unit, 10 * unit, 10 * unit), 2 * unit, 2 * unit)


def _draw_trash(painter: QPainter, size: float, colour: QColor) -> None:
    _stroke(painter, colour)
    unit = size / _SIZE
    painter.drawLine(QPointF(5 * unit, 8 * unit), QPointF(19 * unit, 8 * unit))
    painter.drawRoundedRect(
        QRectF(7 * unit, 8 * unit, 10 * unit, 11 * unit), 1.6 * unit, 1.6 * unit
    )
    painter.drawLine(QPointF(10 * unit, 5 * unit), QPointF(14 * unit, 5 * unit))
    painter.drawLine(QPointF(10 * unit, 11 * unit), QPointF(10 * unit, 16 * unit))
    painter.drawLine(QPointF(14 * unit, 11 * unit), QPointF(14 * unit, 16 * unit))


def _draw_folder(painter: QPainter, size: float, colour: QColor) -> None:
    _stroke(painter, colour)
    unit = size / _SIZE
    painter.drawRoundedRect(
        QRectF(3 * unit, 7 * unit, 18 * unit, 12 * unit), 2.4 * unit, 2.4 * unit
    )
    painter.drawLine(QPointF(3 * unit, 11 * unit), QPointF(10 * unit, 11 * unit))


def _draw_check(painter: QPainter, size: float, colour: QColor) -> None:
    _stroke(painter, colour, 1.8)
    unit = size / _SIZE
    painter.drawLine(QPointF(6 * unit, 12.5 * unit), QPointF(10.5 * unit, 17 * unit))
    painter.drawLine(QPointF(10.5 * unit, 17 * unit), QPointF(18 * unit, 8 * unit))


def _draw_alert(painter: QPainter, size: float, colour: QColor) -> None:
    _stroke(painter, colour, 1.7)
    unit = size / _SIZE
    painter.drawPolygon(
        [
            QPointF(12 * unit, 4 * unit),
            QPointF(21 * unit, 19 * unit),
            QPointF(3 * unit, 19 * unit),
        ]
    )
    painter.drawLine(QPointF(12 * unit, 10 * unit), QPointF(12 * unit, 14 * unit))
    painter.drawPoint(QPointF(12 * unit, 16.5 * unit))


def _draw_search(painter: QPainter, size: float, colour: QColor) -> None:
    _stroke(painter, colour, 1.7)
    unit = size / _SIZE
    painter.drawEllipse(QPointF(10.5 * unit, 10.5 * unit), 6 * unit, 6 * unit)
    painter.drawLine(QPointF(15 * unit, 15 * unit), QPointF(20 * unit, 20 * unit))


DRAWERS: Final[dict[str, Drawer]] = {
    "overview": _draw_overview,
    "library": _draw_library,
    "peers": _draw_peers,
    "network": _draw_network,
    "dht": _draw_dht,
    "logs": _draw_logs,
    "settings": _draw_settings,
    "plus": _draw_plus,
    "play": _draw_play,
    "pause": _draw_pause,
    "stop": _draw_stop,
    "trash": _draw_trash,
    "folder": _draw_folder,
    "check": _draw_check,
    "alert": _draw_alert,
    "search": _draw_search,
}
"""Every icon by name. Unknown names fall back to :func:`icon` raising."""


_CACHE: dict[tuple[str, int, str], QIcon] = {}


def icon(name: str, *, size: int = 18, colour: str | None = None) -> QIcon:
    """The named icon, at this size and colour.

    Args:
        name: One of :data:`DRAWERS`.
        size: Pixel size (icons are square).
        colour: Hex colour; defaults to the theme's primary text.

    Returns:
        A cached :class:`QIcon`.

    Raises:
        KeyError: If no icon has that name — better a loud failure in a test
            than an invisible button.
    """
    if name not in DRAWERS:
        raise KeyError(f"no icon named {name!r}; have {sorted(DRAWERS)}")
    tint = colour or DARK.text
    key = (name, size, tint)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    try:
        DRAWERS[name](painter, float(size), QColor(tint))
    finally:
        painter.end()

    built = QIcon(pixmap)
    _CACHE[key] = built
    return built


def clear_cache() -> None:
    """Forget every rendered icon. Used when the theme changes."""
    _CACHE.clear()
