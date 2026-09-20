"""The peers table model (PRD §10.6).

A :class:`QAbstractTableModel` over the swarm, so the peers tab is a real,
sortable table rather than a list of strings that happens to line up.

One column is deliberately missing: the specification lists *country*, and
resolving a peer's country needs a GeoIP database this client does not ship.
A column of dashes would not be information, it would be an apology, so the
column is absent and this is where that is written down. Everything shown is
measured by the peer connection itself.

Rates come from :class:`~app.ui.viewmodels.peers_vm.PeersViewModel`, which
differences the counters; the model never computes a rate of its own, because
it does not hold the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
)

from app.services.torrent_service import PeerView
from app.ui.format import UNKNOWN, human_bytes, human_duration, human_rate
from app.ui.viewmodels.peers_vm import PeersViewModel


@dataclass(frozen=True, slots=True)
class Column:
    """One column of the table.

    Attributes:
        title: What the header says.
        text: Renders one peer as a string.
        sort_key: Orders peers; smaller sorts first under AscendingOrder.
        numeric: Whether the column reads as a number, so it can be right-aligned.
    """

    title: str
    text: Any
    sort_key: Any
    numeric: bool = False


def _yes_no(value: bool) -> str:
    """A flag as a word. Booleans render badly as numbers in a table."""
    return "yes" if value else "no"


class PeerTableModel(QAbstractTableModel):
    """Every peer, one row each, sortable by any column.

    Args:
        view_model: Source of rates and pulses. Kept, not copied: the table
            re-reads it, so rates stay live without the model owning a clock.
        parent: Qt parent.
    """

    counts_changed = Signal()

    def __init__(self, view_model: PeersViewModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._rows: list[PeerView] = []
        self._order: tuple[int, ...] = ()
        self._sort_column = 0
        self._sort_order = Qt.SortOrder.AscendingOrder

    # ------------------------------------------------------------------ columns

    def _columns(self) -> tuple[Column, ...]:
        """The columns, built per call so they can close over the view model."""
        activity = self._view_model.activity_for
        return (
            Column("peer", lambda peer: peer.label, lambda peer: peer.key),
            Column("client", lambda peer: peer.client, lambda peer: peer.client.lower()),
            Column(
                "down",
                lambda peer: human_rate(activity(peer.key).down_rate),
                lambda peer: activity(peer.key).down_rate or -1.0,
                numeric=True,
            ),
            Column(
                "up",
                lambda peer: human_rate(activity(peer.key).up_rate),
                lambda peer: activity(peer.key).up_rate or -1.0,
                numeric=True,
            ),
            Column(
                "downloaded",
                lambda peer: human_bytes(peer.downloaded),
                lambda peer: peer.downloaded,
                numeric=True,
            ),
            Column(
                "uploaded",
                lambda peer: human_bytes(peer.uploaded),
                lambda peer: peer.uploaded,
                numeric=True,
            ),
            Column(
                "pieces",
                lambda peer: (
                    f"{peer.pieces_held}/{peer.piece_count}" if peer.piece_count else UNKNOWN
                ),
                lambda peer: peer.share,
                numeric=True,
            ),
            Column(
                "choked",
                lambda peer: "no" if peer.state == "connected" and not peer.choking_us else "yes",
                lambda peer: 0 if not peer.choking_us else 1,
            ),
            Column(
                "interested",
                lambda peer: (
                    _yes_no(peer.interested_in_us) if peer.state == "connected" else UNKNOWN
                ),
                lambda peer: 0 if peer.interested_in_us else 1,
            ),
            Column(
                "latency",
                lambda peer: (
                    f"{peer.latency_ms:.0f} ms" if peer.latency_ms is not None else UNKNOWN
                ),
                lambda peer: peer.latency_ms if peer.latency_ms is not None else 1e9,
                numeric=True,
            ),
            Column(
                "idle",
                lambda peer: (
                    human_duration(peer.idle_for) if peer.state == "connected" else UNKNOWN
                ),
                lambda peer: peer.idle_for,
                numeric=True,
            ),
            Column(
                "in flight",
                lambda peer: str(peer.blocks_in_flight) if peer.state == "connected" else UNKNOWN,
                lambda peer: peer.blocks_in_flight,
                numeric=True,
            ),
            Column("found via", lambda peer: peer.source or UNKNOWN, lambda peer: peer.source),
        )

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many columns the table has."""
        return 0 if parent.isValid() else len(self._columns())

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """Column titles."""
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        columns = self._columns()
        return columns[section].title if 0 <= section < len(columns) else None

    # --------------------------------------------------------------------- rows

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many peers are in the table."""
        return 0 if parent.isValid() else len(self._rows)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """One cell."""
        if not index.isValid() or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.TextAlignmentRole,
        ):
            return None
        row, column = index.row(), index.column()
        if not 0 <= row < len(self._rows) or not 0 <= column < len(self._columns()):
            return None
        peer = self._rows[row]
        spec = self._columns()[column]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if spec.numeric
                else int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            )
        return spec.text(peer)

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Sort by a column, stable within ties."""
        columns = self._columns()
        if not 0 <= column < len(columns):
            return
        self.layoutAboutToBeChanged.emit()
        self._sort_column, self._sort_order = column, order
        key = columns[column].sort_key
        # Connected peers before candidates, always: the peers that are
        # actually working are the ones you came to look at.
        self._rows.sort(key=key, reverse=order == Qt.SortOrder.DescendingOrder)
        self._rows.sort(key=lambda peer: peer.state != "connected")
        self.layoutChanged.emit()

    # ------------------------------------------------------------------- update

    def set_peers(self, peers: tuple[PeerView, ...]) -> None:
        """Replace the rows with this swarm, keeping the sort."""
        self.beginResetModel()
        self._rows = list(peers)
        self.endResetModel()
        self.sort(self._sort_column, self._sort_order)
        self.counts_changed.emit()

    def clear(self) -> None:
        """Empty the table."""
        self.beginResetModel()
        self._rows = []
        self.endResetModel()
        self.counts_changed.emit()

    def peer_at(self, row: int) -> PeerView | None:
        """The peer in one row, or ``None``."""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_of(self, key: str) -> int:
        """The row holding one peer, or ``-1``."""
        for index, peer in enumerate(self._rows):
            if peer.key == key:
                return index
        return -1

    @property
    def peers(self) -> tuple[PeerView, ...]:
        """The rows, in the order they are displayed."""
        return tuple(self._rows)
