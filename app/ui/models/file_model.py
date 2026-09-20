"""The files table model (PRD §10.7).

One row per file in the torrent: name, size, and how much of it has verified.
Per-file progress is counted from the pieces the file spans, so it is the same
number the piece matrix is showing, from the other end of the telescope.

The model holds the torrent's file list, which is metadata and does not change,
and accepts a fresh :class:`~app.services.torrent_service.FileView` tuple when
pieces verify. Nothing here computes progress from sizes: a 4 GiB file whose
first piece verified is not 0.02 % done in any useful sense until the pieces
say so.
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

from app.services.torrent_service import FileView
from app.ui.format import human_bytes, human_percent

COLUMNS: tuple[tuple[str, bool], ...] = (
    ("file", False),
    ("size", True),
    ("pieces", True),
    ("done", True),
)


class FileTableModel(QAbstractTableModel):
    """Every file in one torrent, one row each.

    Args:
        parent: Qt parent.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[FileView] = []

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
        return COLUMNS[section][0] if 0 <= section < len(COLUMNS) else None

    # --------------------------------------------------------------------- rows

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many files."""
        return 0 if parent.isValid() else len(self._rows)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """One cell."""
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        column = index.column()
        if not 0 <= column < len(COLUMNS):
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            alignment = (
                Qt.AlignmentFlag.AlignRight if COLUMNS[column][1] else Qt.AlignmentFlag.AlignLeft
            )
            return int(alignment | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if column == 0:
            return row.path
        if column == 1:
            return human_bytes(row.length)
        if column == 2:
            return f"{row.verified_pieces}/{row.piece_count}" if row.piece_count else "--"
        return human_percent(row.progress)

    # ------------------------------------------------------------------- update

    def set_files(self, files: tuple[FileView, ...]) -> None:
        """Replace the rows."""
        self.beginResetModel()
        self._rows = list(files)
        self.endResetModel()

    def clear(self) -> None:
        """Empty the table."""
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def file_at(self, row: int) -> FileView | None:
        """The file in one row, or ``None``."""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    @property
    def files(self) -> tuple[FileView, ...]:
        """The rows, in torrent order."""
        return tuple(self._rows)

    @property
    def total_length(self) -> int:
        """How big the torrent's files add up to."""
        return sum(row.length for row in self._rows)

    @property
    def progress(self) -> float:
        """Bytes verified over bytes total — the torrent's own progress, by files."""
        total = self.total_length
        if total <= 0:
            return 0.0
        return min(1.0, sum(row.length * row.progress for row in self._rows) / total)
