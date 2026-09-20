"""The piece matrix (PRD §38 / TRD §38): every piece, one cell each.

A torrent is thousands of pieces and a progress bar is one number. This is the
view in between: a grid where each cell is a piece, coloured by which of the
five states it is in — missing, requested, downloading, verified, failed.

What it does with the information it has:

* **Partial pieces show partial fill.** A piece being downloaded is drawn with
  the fraction of its blocks that have actually arrived, so "downloading" is
  visibly different from "requested" and you can watch a piece fill up.
* **Pieces nobody has are marked.** A piece that no connected peer holds cannot
  be downloaded, and a torrent can sit at 99 % forever because of one. Those
  cells get a red outline; it is the most useful thing this control tells you.
* **Rarity is not invented.** Availability here is a count of the *connected*
  peers holding each piece. It is not an estimate of the swarm.

The grid is computed by :func:`layout_cells`, a pure function, so the mapping
can be tested without a window. Cells are keyboard reachable: arrows move a
cursor, Enter selects, Escape clears.

Args (constructor): see :meth:`PieceMatrix.__init__`.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (
    QEvent,
    QPointF,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from app.services.torrent_service import PIECE_STATE_NAMES, PieceMap, PieceMapState
from app.ui.format import human_bytes
from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS, Tokens
from app.ui.viewmodels.pieces_vm import PiecesViewModel

# Minimum cell size and gap, in pixels. Below this a 3020-piece torrent turns
# into a texture rather than a grid, and the control says so instead.
MIN_CELL: int = 3
MIN_GAP: int = 1

# Pieces above this count are drawn, but the hover caption warns that a cell is
# more than one piece: at some size a single pixel is several pieces deep.
HINT_PIECE_COUNT: int = 4000


@dataclass(frozen=True, slots=True)
class PieceCell:
    """One piece, placed.

    Attributes:
        index: Piece index in the torrent.
        row / column: Where it sits in the grid.
        x / y: Top-left of the cell, in widget pixels.
        size: Side length of the cell.
        state: A :class:`PieceMapState` code.
        name: The state's name, e.g. ``"verified"``.
        availability: Connected peers holding this piece.
        filled: ``0.0``-``1.0``, blocks received for an in-flight piece.
    """

    index: int
    row: int
    column: int
    x: float
    y: float
    size: float
    state: int
    name: str
    availability: int
    filled: float
    bytes: int

    def rect(self) -> QRectF:
        """The cell's rectangle."""
        return QRectF(self.x, self.y, self.size, self.size)

    def contains(self, point: QPointF) -> bool:
        """Whether a point is inside this cell."""
        return self.rect().contains(point)

    @property
    def orphaned(self) -> bool:
        """Whether nobody connected holds it and we still need it."""
        return self.availability <= 0 and self.state != PieceMapState.VERIFIED

    def as_dict(self) -> dict[str, float | int | str]:
        """The cell as plain data, for tests."""
        return {
            "index": self.index,
            "row": self.row,
            "column": self.column,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "size": round(self.size, 2),
            "name": self.name,
            "availability": self.availability,
            "filled": round(self.filled, 3),
            "bytes": self.bytes,
        }


class PieceMatrix(QWidget):
    """Every piece of a torrent, as a grid of cells.

    Args:
        palette: Colours.
        design: Spacing, type sizes, chart geometry.
        parent: Qt parent.
    """

    piece_hovered = Signal(object)
    """The :class:`PieceCell` under the pointer or cursor, or ``None``."""

    piece_selected = Signal(int)
    """The index of the piece that was clicked or chosen with Enter."""

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
        self._piece_map: PieceMap | None = None
        self._cells: tuple[PieceCell, ...] = ()
        self._columns = 0
        self._hovered: PieceCell | None = None
        self._cursor: int | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # The matrix is a chart you can drive from the keyboard, so it says so:
        # a focusable widget with no name is a tab stop that announces nothing.
        self.setAccessibleName("Piece map")
        self.setAccessibleDescription(
            "One cell per piece. Arrow keys move, Home and End jump to the ends, "
            "Enter or Space selects a piece and reads it out."
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 120)

    # ------------------------------------------------------------------- input

    def set_piece_map(self, piece_map: PieceMap | None) -> None:
        """Draw these pieces. Re-lays out and repaints."""
        self._piece_map = piece_map
        self._relayout()
        self.update()

    def set_view_model(self, view_model: PiecesViewModel | None) -> None:
        """Follow a :class:`PiecesViewModel`, which owns the last read."""
        self.set_piece_map(None if view_model is None else view_model.piece_map)

    def set_palette(self, palette: Palette) -> None:
        """Recolour, for when the theme changes."""
        self._palette = palette
        self.update()

    # ------------------------------------------------------------------ layout

    def cells(self) -> tuple[PieceCell, ...]:
        """Every cell, placed."""
        return self._cells

    @property
    def columns(self) -> int:
        """How many cells fit across the current width."""
        return self._columns

    def cell_at(self, point: QPointF) -> PieceCell | None:
        """The cell under a point, or ``None``.

        Arithmetic rather than a scan: with tens of thousands of pieces, a
        hover that walked the list would be the slowest thing on screen.
        """
        if not self._cells or self._columns <= 0:
            return None
        first = self._cells[0]
        step = first.size + float(self._design.chart.piece_gap)
        column = int((point.x() - first.x) // step) if step > 0 else 0
        row = int((point.y() - first.y) // step) if step > 0 else 0
        if column < 0 or row < 0 or column >= self._columns:
            return None
        index = row * self._columns + column
        if index >= len(self._cells):
            return None
        cell = self._cells[index]
        return cell if cell.contains(point) else None

    def cell_for(self, index: int) -> PieceCell | None:
        """The cell for one piece index, if it is laid out."""
        if 0 <= index < len(self._cells):
            return self._cells[index]
        return None

    @property
    def hovered(self) -> PieceCell | None:
        """The cell under the pointer."""
        return self._hovered

    @property
    def cursor_index(self) -> int | None:
        """The keyboard cursor's piece index, or ``None``.

        Not called ``cursor``: ``QWidget.cursor()`` already means the mouse
        pointer, and shadowing it would be a bug waiting for a reader.
        """
        return self._cursor

    def set_cursor(self, index: int | None) -> None:
        """Move the keyboard cursor, emitting the hover signal as it goes."""
        if index is not None and not 0 <= index < len(self._cells):
            return
        self._cursor = index
        cell = None if index is None else self.cell_for(index)
        self.piece_hovered.emit(cell)
        self.update()

    def _relayout(self) -> None:
        """Recompute the grid for the current size and piece count."""
        piece_map = self._piece_map
        if piece_map is None or piece_map.piece_count <= 0:
            self._cells = ()
            self._columns = 0
            self._hovered = None
            self._cursor = None
            return
        self._cells, self._columns = layout_cells(
            piece_map,
            width=float(self.width()),
            height=float(self.height()),
            design=self._design,
        )
        self._hovered = None
        if self._cursor is not None and self._cursor >= len(self._cells):
            self._cursor = None

    # ----------------------------------------------------------------- drawing

    def sizeHint(self) -> QSize:
        """Wide and short: a strip of pieces, like a film strip."""
        return QSize(520, 160)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw the grid, then the legend."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if not self._cells:
            self._paint_empty(painter)
            return

        palette = self._palette
        for cell in self._cells:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette.state_colour(cell.name)))
            painter.drawRect(cell.rect())
            if cell.state == PieceMapState.DOWNLOADING and cell.filled > 0.0:
                # The block tally, drawn as a fill from the left: you can watch
                # a piece come together rather than waiting for it to turn green.
                painter.setBrush(QColor(palette.state_colour("verified")))
                painter.drawRect(QRectF(cell.x, cell.y, cell.size * cell.filled, cell.size))
            elif cell.orphaned:
                painter.setPen(QPen(QColor(palette.danger), 1.0, Qt.PenStyle.SolidLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(cell.rect().adjusted(0.5, 0.5, -0.5, -0.5))

        focus = self._hovered
        if focus is None and self._cursor is not None:
            focus = self.cell_for(self._cursor)
        if focus is not None:
            painter.setPen(QPen(QColor(palette.text), 1.4, Qt.PenStyle.SolidLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(focus.rect().adjusted(-1.0, -1.0, 1.0, 1.0))
        self._paint_legend(painter)

    def _paint_empty(self, painter: QPainter) -> None:
        """No pieces to draw: say that, rather than showing an empty box."""
        palette = self._palette
        font = QFont(self.font())
        font.setPixelSize(self._design.type.body)
        painter.setFont(font)
        painter.setPen(QColor(palette.text_muted))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No pieces to show")

    def _paint_legend(self, painter: QPainter) -> None:
        """Counts per state, with their colours, along the bottom."""
        palette = self._palette
        piece_map = self._piece_map
        if piece_map is None:
            return
        font = QFont(self.font())
        font.setPixelSize(max(8, self._design.type.body - 3))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        y = float(self.height()) - 12.0
        x = 4.0
        for name in PIECE_STATE_NAMES:
            count = piece_map.counts.get(name, 0)
            if count == 0 and name == "failed":
                continue
            label = f"{name} {count}"
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette.state_colour(name)))
            painter.drawRect(QRectF(x, y - 3.0, 7.0, 7.0))
            painter.setPen(QColor(palette.text_muted))
            width = float(metrics.horizontalAdvance(label))
            painter.drawText(
                QRectF(x + 11.0, y - 8.0, width + 6.0, 14.0), Qt.AlignmentFlag.AlignVCenter, label
            )
            x += 11.0 + width + 14.0

    # ------------------------------------------------------------------ events

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Follow the pointer, cell by cell."""
        found = self.cell_at(QPointF(event.position()))
        if found is not self._hovered:
            self._hovered = found
            self.piece_hovered.emit(found)
            if found is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(event.globalPosition().toPoint(), _tooltip(found), self)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """Clear the pointer's cell when it leaves."""
        if self._hovered is not None:
            self._hovered = None
            self.piece_hovered.emit(None)
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Select the piece under the pointer."""
        found = self.cell_at(QPointF(event.position()))
        if found is not None:
            self._cursor = found.index
            self.piece_selected.emit(found.index)
            self.update()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Move the cursor with the arrows and choose with Enter or Space."""
        key = Qt.Key(event.key())
        if not self._cells:
            super().keyPressEvent(event)
            return
        step = {
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Up: -max(1, self._columns),
            Qt.Key.Key_Down: max(1, self._columns),
            Qt.Key.Key_Home: -len(self._cells),
            Qt.Key.Key_End: len(self._cells),
            Qt.Key.Key_PageUp: -max(1, self._columns) * 5,
            Qt.Key.Key_PageDown: max(1, self._columns) * 5,
        }.get(key)
        current = 0 if self._cursor is None else self._cursor
        if step is not None:
            target = min(len(self._cells) - 1, max(0, current + step))
            self.set_cursor(target)
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.set_cursor(current)
            self.piece_selected.emit(current)
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.set_cursor(None)
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-wrap the grid when the control changes size."""
        self._relayout()
        super().resizeEvent(event)


# ---------------------------------------------------------------------- mapping


def layout_cells(
    piece_map: PieceMap,
    *,
    width: float,
    height: float,
    design: Tokens | None = None,
) -> tuple[tuple[PieceCell, ...], int]:
    """Lay a piece map out as a grid: the mapping, with no widget involved.

    The column count is chosen so the whole torrent fits the available width at
    the configured cell size; if it does not fit, the cells shrink to the
    minimum and the grid stays in reading order rather than interleaving.

    Args:
        piece_map: The pieces and their states.
        width / height: Available area in pixels.
        design: Cell size and gap.

    Returns:
        The cells in piece order, and how many columns the grid has.
    """
    chart = (design or TOKENS).chart
    gap = float(max(MIN_GAP, chart.piece_gap))
    size = float(max(MIN_CELL, chart.piece_cell))

    usable_width = max(size, width - 8.0)
    per_row = max(1, int((usable_width + gap) // (size + gap)))
    columns = min(per_row, max(1, piece_map.piece_count))

    step = size + gap
    rows = (piece_map.piece_count + columns - 1) // columns
    needed = rows * step
    if needed > height and height > 0:
        # Shrink to fit rather than clip: a matrix that hid the last third of
        # the torrent would be worse than a slightly smaller grid.
        scale = max(MIN_CELL / size, height / needed)
        size = max(float(MIN_CELL), size * scale)
        step = size + gap
        columns = min(
            max(1, piece_map.piece_count),
            max(1, int((usable_width + gap) // step)),
        )

    cells: list[PieceCell] = []
    for index in range(piece_map.piece_count):
        row, column = divmod(index, columns)
        code = piece_map.states[index]
        cells.append(
            PieceCell(
                index=index,
                row=row,
                column=column,
                x=4.0 + column * step,
                y=4.0 + row * step,
                size=size,
                state=code,
                name=PIECE_STATE_NAMES[code],
                availability=piece_map.availability[index],
                filled=piece_map.filled[index],
                bytes=piece_map.piece_size(index),
            )
        )
    return tuple(cells), columns


def _tooltip(cell: PieceCell) -> str:
    """Everything measurable about one piece: index, state, size, who has it."""
    lines = [
        f"Piece {cell.index} · {cell.name} · {human_bytes(cell.bytes)}",
        f"held by {cell.availability} connected peer{'s' if cell.availability != 1 else ''}",
    ]
    if cell.state == PieceMapState.DOWNLOADING:
        lines.append(f"{cell.filled * 100:.0f}% of its blocks have arrived")
    if cell.orphaned:
        lines.append("nobody connected has this piece")
    if cell.index >= HINT_PIECE_COUNT:
        lines.append("large torrent: one cell may cover more than one piece")
    return "\n".join(lines)
