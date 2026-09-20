"""The pieces tab (PRD §10.5): every piece, one cell.

The matrix shows the shape of the download — a torrent that is 80 % done in
contiguous blocks looks nothing like one that is 80 % done in scattered pieces,
and that difference is the whole reason this tab exists instead of a bar.

Alongside it: how many pieces are in each state, and the warning that matters
most — pieces we still need that nobody connected is holding.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.ui.charts.piece_matrix import PieceCell, PieceMatrix
from app.ui.format import human_bytes, human_percent
from app.ui.models.piece_model import PieceStateModel
from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.torrent_vm import TorrentViewModel
from app.ui.widgets.panel import Panel


class PiecesTab(QWidget):
    """The piece matrix, its legend, and its warnings.

    Args:
        view_model: The torrent whose pieces this is.
        palette: Colours.
        parent: Qt parent.
    """

    def __init__(
        self,
        view_model: TorrentViewModel,
        palette: Palette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._vm = view_model
        self._palette = palette

        column = QVBoxLayout(self)
        column.setContentsMargins(
            TOKENS.spacing.xl, TOKENS.spacing.lg, TOKENS.spacing.xl, TOKENS.spacing.xl
        )
        column.setSpacing(TOKENS.spacing.lg)

        # ---- matrix
        self._matrix_panel = Panel(
            "Pieces",
            "One cell per piece. Hover for its state; arrow keys walk the grid.",
            self,
        )
        self._matrix = PieceMatrix(palette, parent=self._matrix_panel)
        self._matrix_panel.body.addWidget(self._matrix, stretch=1)
        self._readout = QLabel("")
        self._readout.setProperty("role", "faint")
        self._readout.setWordWrap(True)
        self._matrix_panel.body.addWidget(self._readout)
        column.addWidget(self._matrix_panel, stretch=3)

        # ---- legend and warnings
        self._legend_panel = Panel("States", "Counted, not estimated.", self)
        self._legend_model = PieceStateModel(view_model.pieces, self)
        self._legend = QTableView(self._legend_panel)
        self._legend.setAccessibleName("Piece states")
        self._legend.setAccessibleDescription(
            "How many pieces are missing, requested, downloading, verified or failed."
        )
        self._legend.setModel(self._legend_model)
        self._legend.setShowGrid(False)
        self._legend.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._legend.verticalHeader().setVisible(False)
        self._legend.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._legend.setFixedHeight(150)
        self._legend_panel.body.addWidget(self._legend)

        self._warning = QLabel("")
        self._warning.setProperty("role", "error")
        self._warning.setWordWrap(True)
        self._warning.setVisible(False)
        self._legend_panel.body.addWidget(self._warning)
        column.addWidget(self._legend_panel, stretch=2)

        self._matrix.piece_hovered.connect(self._on_hovered)
        self.refresh()

    # ------------------------------------------------------------------ access

    @property
    def matrix(self) -> PieceMatrix:
        """The piece matrix, for tests and hover wiring."""
        return self._matrix

    @property
    def warning(self) -> QLabel:
        """The label that warns about pieces nobody has."""
        return self._warning

    # ------------------------------------------------------------------ drawing

    def refresh(self) -> None:
        """Redraw the matrix, the counts and the warning."""
        pieces = self._vm.pieces
        self._matrix.set_piece_map(pieces.piece_map)
        self._legend_model.refresh()
        piece_map = pieces.piece_map
        if piece_map is None:
            self._matrix_panel.set_subtitle("No pieces read yet.")
            self._warning.setVisible(False)
            return

        self._matrix_panel.set_subtitle(
            f"{piece_map.piece_count} pieces · {human_bytes(piece_map.piece_length)} each · "
            f"{human_bytes(piece_map.total_length)} total · "
            f"{human_percent(pieces.progress)} verified"
        )

        orphaned = pieces.orphaned()
        if orphaned:
            shown = ", ".join(str(index) for index in orphaned[:8])
            more = "" if len(orphaned) <= 8 else f" (+{len(orphaned) - 8} more)"
            self._warning.setText(
                f"{len(orphaned)} pieces are wanted but nobody connected has them "
                f"({shown}{more}). They cannot download until a peer holding them "
                f"appears."
            )
            self._warning.setVisible(True)
        else:
            self._warning.setVisible(False)

    def set_palette(self, palette: Palette) -> None:
        """Recolour, for when the theme changes."""
        self._palette = palette
        self._matrix.set_palette(palette)

    # ------------------------------------------------------------------ events

    def _on_hovered(self, cell: object) -> None:
        """Read out one piece under the pointer."""
        if cell is None:
            self._readout.setText("")
            return
        assert isinstance(cell, PieceCell)
        held = f"held by {cell.availability}" if cell.availability else "held by nobody connected"
        extra = ""
        if cell.state == 2 and cell.filled:
            extra = f" · {cell.filled * 100:.0f}% of its blocks arrived"
        self._readout.setText(
            f"Piece {cell.index}: {cell.name} · {human_bytes(cell.bytes)} · {held}{extra}"
        )
