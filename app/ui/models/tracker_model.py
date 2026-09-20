"""The trackers table model (PRD §10.8).

One row per tracker URL, showing what it last told us: how many seeders and
leechers *it* counts, how many peers it handed over, how long it took, how many
times it has failed in a row, and when we will ask again.

The swarm counters are the tracker's, not ours. A tracker reporting 400 seeders
while we are connected to three is the ordinary state of things, and the table
shows both numbers side by side rather than reconciling them into one flattering
figure.
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

from app.tracker.base import TrackerState, TrackerStatus
from app.ui.format import UNKNOWN, human_duration

#: How each state reads in the table.
STATE_LABELS: dict[TrackerState, str] = {
    TrackerState.OK: "ok",
    TrackerState.WARNING: "warning",
    TrackerState.FAILED: "failed",
    TrackerState.UNKNOWN: "not announced",
}

#: The colour role each state asks the palette for.
STATE_ROLES: dict[TrackerState, str] = {
    TrackerState.OK: "ok",
    TrackerState.WARNING: "warning",
    TrackerState.FAILED: "error",
    TrackerState.UNKNOWN: "idle",
}


class TrackerTableModel(QAbstractTableModel):
    """Every tracker on one torrent, one row each.

    Args:
        parent: Qt parent.
    """

    COLUMNS: tuple[tuple[str, bool], ...] = (
        ("url", False),
        ("state", False),
        ("seeders", True),
        ("leechers", True),
        ("peers", True),
        ("latency", True),
        ("failures", True),
        ("next in", True),
        ("last message", False),
    )

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[TrackerStatus] = []

    # ----------------------------------------------------------------- columns

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many columns."""
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """Column titles."""
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return self.COLUMNS[section][0] if 0 <= section < len(self.COLUMNS) else None

    # --------------------------------------------------------------------- rows

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many trackers."""
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
        if not 0 <= column < len(self.COLUMNS):
            return None
        status = self._rows[index.row()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            numeric = self.COLUMNS[column][1]
            alignment = Qt.AlignmentFlag.AlignRight if numeric else Qt.AlignmentFlag.AlignLeft
            return int(alignment | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return _cell(status, column)

    # ------------------------------------------------------------------- update

    def set_statuses(self, statuses: tuple[TrackerStatus, ...]) -> None:
        """Replace the rows, in announce order."""
        self.beginResetModel()
        self._rows = list(statuses)
        self.endResetModel()

    def clear(self) -> None:
        """Empty the table."""
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def status_at(self, row: int) -> TrackerStatus | None:
        """The tracker in one row, or ``None``."""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    @property
    def statuses(self) -> tuple[TrackerStatus, ...]:
        """The rows, in the order they are displayed."""
        return tuple(self._rows)

    def role_at(self, row: int) -> str:
        """The colour role for one row's state, e.g. ``"ok"`` or ``"error"``."""
        status = self.status_at(row)
        return STATE_ROLES.get(status.state, "idle") if status else "idle"


def _cell(status: TrackerStatus, column: int) -> str:
    """One tracker, one column, as text."""
    if column == 0:
        return status.url
    if column == 1:
        return STATE_LABELS.get(status.state, "unknown")
    if column == 2:
        return str(status.seeders)
    if column == 3:
        return str(status.leechers)
    if column == 4:
        return str(status.peers_returned)
    if column == 5:
        return f"{status.latency_ms:.0f} ms" if status.latency_ms is not None else UNKNOWN
    if column == 6:
        return str(status.consecutive_failures)
    if column == 7:
        if status.next_announce_at is None:
            return UNKNOWN
        remaining = status.next_announce_at - time.time()
        return "now" if remaining <= 0 else human_duration(remaining)
    if column == 8:
        return status.last_error or ""
    return ""
