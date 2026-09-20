"""The piece legend model (PRD §10.5).

The matrix draws the pieces; this model says what the colours mean and how many
pieces are in each state. It is a table with five rows — missing, requested,
downloading, verified, failed — each with a count, a share, and the colour role
the matrix paints it in.

It exists as a model rather than five labels so the legend cannot drift from
the matrix: both read the same :class:`~app.ui.viewmodels.pieces_vm.PiecesViewModel`,
and a sixth state would appear in both or neither.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

from app.services.torrent_service import PIECE_STATE_NAMES
from app.ui.format import human_percent
from app.ui.viewmodels.pieces_vm import PiecesViewModel

#: The colour role each state asks the palette for. They match the matrix.
STATE_ROLES: dict[str, str] = {
    "missing": "missing",
    "requested": "requested",
    "downloading": "downloading",
    "verified": "verified",
    "failed": "failed",
}

COLUMNS: tuple[str, ...] = ("state", "pieces", "share")


class PieceStateModel(QAbstractTableModel):
    """The five piece states, counted.

    Args:
        view_model: Source of the counts.
        parent: Qt parent.
    """

    def __init__(self, view_model: PiecesViewModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model

    # ----------------------------------------------------------------- columns

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many columns."""
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """Column titles."""
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return COLUMNS[section] if 0 <= section < len(COLUMNS) else None

    # --------------------------------------------------------------------- rows

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """Five states, always: a legend that only listed what happens to be
        non-zero would move around as the download runs."""
        return 0 if parent.isValid() else len(PIECE_STATE_NAMES)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """One cell."""
        if not index.isValid() or not 0 <= index.row() < len(PIECE_STATE_NAMES):
            return None
        name = PIECE_STATE_NAMES[index.row()]
        counts = self._view_model.counts
        total = self._view_model.piece_count
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return name
            if index.column() == 1:
                return str(counts.get(name, 0))
            return human_percent(counts.get(name, 0) / total) if total else "--"
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() > 0:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            return STATE_ROLES.get(name, "missing")
        return None

    def refresh(self) -> None:
        """Re-read the counts after the pieces changed."""
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(PIECE_STATE_NAMES) - 1, len(COLUMNS) - 1),
        )

    def role_for(self, row: int) -> str:
        """The colour role for one state's row."""
        if 0 <= row < len(PIECE_STATE_NAMES):
            return STATE_ROLES.get(PIECE_STATE_NAMES[row], "missing")
        return "missing"
