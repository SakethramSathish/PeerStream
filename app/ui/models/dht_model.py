"""The DHT contacts table model.

One row per node in the routing table. Every column is something the routing
table actually recorded — the id the node gave us, the address it answered
from, when it last answered, how often it has not. There is no "health" column,
because health here is two facts already shown: how long since it spoke, and
how many times it has failed since.

A node we have been *told* about but never met shows ``--`` for "seen", because
we have not seen it. That is not the same as a node that has gone quiet, and
conflating them would make the table unreadable at exactly the moment it
matters.
"""

from __future__ import annotations

from typing import Any, Final

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

from app.ui.format import UNKNOWN, human_duration
from app.ui.viewmodels.dht_vm import DhtContactView, DhtViewModel

COLUMNS: Final[tuple[tuple[str, bool], ...]] = (
    ("node id", False),
    ("address", False),
    ("last seen", True),
    ("failures", True),
    ("status", False),
)


class DhtTableModel(QAbstractTableModel):
    """Every node we know, one row each, sortable by any column.

    Args:
        view_model: Source of the contacts. Kept, not copied, so the table
            re-reads it on every repaint.
        parent: Qt parent.
    """

    def __init__(self, view_model: DhtViewModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._vm = view_model
        self._rows: tuple[DhtContactView, ...] = ()
        self._sort_column = 2
        self._sort_order = Qt.SortOrder.AscendingOrder

    # ----------------------------------------------------------------- content

    def refresh(self) -> None:
        """Re-read the view model and tell Qt what changed."""
        rows = self._sort(self._vm.contacts)
        if rows == self._rows:
            return
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    @property
    def rows(self) -> tuple[DhtContactView, ...]:
        """The rows, in the order the table shows them."""
        return self._rows

    def row_at(self, row: int) -> DhtContactView | None:
        """The contact at ``row``, or ``None`` when out of range."""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    # ------------------------------------------------------------------- model

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        if 0 <= section < len(COLUMNS):
            return COLUMNS[section][0]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or role not in {
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.TextAlignmentRole,
        }:
            return None
        contact = self.row_at(index.row())
        if contact is None or not 0 <= index.column() < len(COLUMNS):
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            numeric = COLUMNS[index.column()][1]
            if numeric:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        match index.column():
            case 0:
                return contact.hex_id
            case 1:
                return contact.address
            case 2:
                return human_duration(contact.seen_seconds)
            case 3:
                return str(contact.failures) if contact.failures else UNKNOWN
            case 4:
                if contact.questionable:
                    return "questionable"
                return "ok" if contact.seen_seconds is not None else "unverified"
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Sort by ``column``. Unknown values sort last, not first."""
        if not 0 <= column < len(COLUMNS):
            return
        self._sort_column = column
        self._sort_order = order
        self.refresh()

    # ------------------------------------------------------------------ sorting

    def _sort(self, contacts: tuple[DhtContactView, ...]) -> tuple[DhtContactView, ...]:
        def key(contact: DhtContactView) -> Any:
            match self._sort_column:
                case 0:
                    return contact.hex_id
                case 1:
                    return contact.address
                case 2:
                    # A node we have never seen sorts after every node we have.
                    return (
                        contact.seen_seconds if contact.seen_seconds is not None else float("inf")
                    )
                case 3:
                    return contact.failures
                case 4:
                    return (contact.questionable, contact.seen_seconds is None)
            return contact.hex_id

        return tuple(
            sorted(contacts, key=key, reverse=self._sort_order == Qt.SortOrder.DescendingOrder)
        )
