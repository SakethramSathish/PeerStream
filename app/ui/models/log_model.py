"""The protocol log table model (PRD §10.10).

A :class:`QAbstractTableModel` over the rows the
:class:`~app.ui.viewmodels.logs_vm.LogsViewModel` has filtered, so the timeline
is a real table: columns you can resize, rows you can select, and a scroll area
that does not rebuild the world on every event.

The model does no filtering of its own — the view model owns the filter, the
search and the bounded buffer, and this is only the table-shaped view of what
it decided to show. Two places cannot disagree about what "ERROR" means if only
one of them decides.
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

from app.ui.viewmodels.logs_vm import LogRow, LogsViewModel

COLUMNS: tuple[tuple[str, int], ...] = (
    ("time", 74),
    ("category", 88),
    ("event", 190),
    ("message", 0),
)


class LogTableModel(QAbstractTableModel):
    """The timeline's rows, newest first.

    Args:
        view_model: The filtered, bounded log.
        parent: Qt parent.
    """

    def __init__(self, view_model: LogsViewModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model
        view_model.changed.connect(self.refresh)

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

    def column_width(self, section: int) -> int:
        """The width a column asks for; ``0`` means "take what is left"."""
        return COLUMNS[section][1] if 0 <= section < len(COLUMNS) else 0

    # --------------------------------------------------------------------- rows

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many rows are visible under the current filter."""
        return 0 if parent.isValid() else len(self._view_model.rows())

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """One cell, or the error flag for styling."""
        rows = self._view_model.rows()
        if not index.isValid() or not 0 <= index.row() < len(rows):
            return None
        row = rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return _cell(row, index.column())
        if role == Qt.ItemDataRole.UserRole:
            return row.error
        if role == Qt.ItemDataRole.ForegroundRole and row.error and index.column() == 3:
            from PySide6.QtGui import QColor

            from app.ui.theme.palette import palette_for

            return QColor(palette_for("dark").danger)
        return None

    # ------------------------------------------------------------------- update

    def refresh(self) -> None:
        """Re-read the rows after the view model changed."""
        self.layoutChanged.emit()

    def row_at(self, row: int) -> LogRow | None:
        """The row at one position, or ``None``."""
        rows = self._view_model.rows()
        return rows[row] if 0 <= row < len(rows) else None


def _cell(row: LogRow, column: int) -> str:
    """One log row, one column."""
    if column == 0:
        return row.when
    if column == 1:
        return row.category
    if column == 2:
        return row.kind
    if column == 3:
        return row.message
    return ""
