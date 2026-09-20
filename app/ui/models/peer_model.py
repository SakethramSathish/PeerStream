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

Performance notes
-----------------
* ``_COLUMN_SPECS`` is a module-level tuple built *once* at import time. Qt
  calls ``data()`` for every visible cell on every repaint; rebuilding 13
  Column objects and their closures 650+ times per frame is measurably
  expensive. The view model is passed explicitly at render time instead.
* ``set_peers()`` uses a surgical diff (insert/remove rows + ``dataChanged``
  only for changed rows) instead of ``beginResetModel`` / ``endResetModel``.
  The nuclear-reset approach discards Qt's cached row heights, selection state
  and delegate renders for every peer on every tick — the primary source of
  the Peers-tab per-second stutter.
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
        text: Renders one peer as a string (takes peer + view_model).
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


# ---------------------------------------------------------------------------
# Module-level column definitions — built once, never rebuilt.
# ``text`` and ``sort_key`` callables now accept (peer, view_model) so
# rate lookups happen live without closing over a mutable reference.
# ---------------------------------------------------------------------------

_COLUMN_SPECS: tuple[Column, ...] = (
    Column("peer",       lambda p, _v: p.label,        lambda p, _v: p.key),
    Column("client",     lambda p, _v: p.client,       lambda p, _v: p.client.lower()),
    Column(
        "down",
        lambda p, v: human_rate(v.activity_for(p.key).down_rate),
        lambda p, v: v.activity_for(p.key).down_rate or -1.0,
        numeric=True,
    ),
    Column(
        "up",
        lambda p, v: human_rate(v.activity_for(p.key).up_rate),
        lambda p, v: v.activity_for(p.key).up_rate or -1.0,
        numeric=True,
    ),
    Column("downloaded", lambda p, _v: human_bytes(p.downloaded), lambda p, _v: p.downloaded, numeric=True),
    Column("uploaded",   lambda p, _v: human_bytes(p.uploaded),   lambda p, _v: p.uploaded,   numeric=True),
    Column(
        "pieces",
        lambda p, _v: f"{p.pieces_held}/{p.piece_count}" if p.piece_count else UNKNOWN,
        lambda p, _v: p.share,
        numeric=True,
    ),
    Column(
        "choked",
        lambda p, _v: "no" if p.state == "connected" and not p.choking_us else "yes",
        lambda p, _v: 0 if not p.choking_us else 1,
    ),
    Column(
        "interested",
        lambda p, _v: _yes_no(p.interested_in_us) if p.state == "connected" else UNKNOWN,
        lambda p, _v: 0 if p.interested_in_us else 1,
    ),
    Column(
        "latency",
        lambda p, _v: f"{p.latency_ms:.0f} ms" if p.latency_ms is not None else UNKNOWN,
        lambda p, _v: p.latency_ms if p.latency_ms is not None else 1e9,
        numeric=True,
    ),
    Column(
        "idle",
        lambda p, _v: human_duration(p.idle_for) if p.state == "connected" else UNKNOWN,
        lambda p, _v: p.idle_for,
        numeric=True,
    ),
    Column(
        "in flight",
        lambda p, _v: str(p.blocks_in_flight) if p.state == "connected" else UNKNOWN,
        lambda p, _v: p.blocks_in_flight,
        numeric=True,
    ),
    Column("found via", lambda p, _v: p.source or UNKNOWN, lambda p, _v: p.source),
)

_NUM_COLUMNS: int = len(_COLUMN_SPECS)


def _display_snapshot(peer: PeerView, vm: PeersViewModel) -> tuple[Any, ...]:
    """Capture every display value for a row in one cheap tuple.

    Compared against the previous snapshot to find which cells changed.
    Only those cells get a ``dataChanged`` signal — unchanged rows cost
    nothing extra.
    """
    act = vm.activity_for(peer.key)
    return (
        peer.label, peer.client,
        act.down_rate, act.up_rate,
        peer.downloaded, peer.uploaded,
        peer.pieces_held, peer.piece_count,
        peer.choking_us, peer.interested_in_us,
        peer.latency_ms, peer.idle_for,
        peer.blocks_in_flight, peer.source, peer.state,
    )


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
        self._display_cache: dict[str, tuple[Any, ...]] = {}
        self._sort_column = 0
        self._sort_order = Qt.SortOrder.AscendingOrder

    # ------------------------------------------------------------------ columns

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many columns the table has."""
        return 0 if parent.isValid() else _NUM_COLUMNS

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """Column titles."""
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return _COLUMN_SPECS[section].title if 0 <= section < _NUM_COLUMNS else None

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
        if not 0 <= row < len(self._rows) or not 0 <= column < _NUM_COLUMNS:
            return None
        peer = self._rows[row]
        spec = _COLUMN_SPECS[column]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if spec.numeric
                else int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            )
        return spec.text(peer, self._view_model)

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Sort by a column, stable within ties."""
        if not 0 <= column < _NUM_COLUMNS:
            return
        self.layoutAboutToBeChanged.emit()
        self._sort_column, self._sort_order = column, order
        key_fn = _COLUMN_SPECS[column].sort_key
        # Connected peers before candidates, always: the peers that are
        # actually working are the ones you came to look at.
        self._rows.sort(
            key=lambda peer: key_fn(peer, self._view_model),
            reverse=order == Qt.SortOrder.DescendingOrder,
        )
        self._rows.sort(key=lambda peer: peer.state != "connected")
        self.layoutChanged.emit()

    # ------------------------------------------------------------------- update

    def set_peers(self, peers: tuple[PeerView, ...]) -> None:
        """Replace the swarm using a surgical diff, not a full model reset.

        Only rows that are added, removed, or whose display values changed
        trigger Qt invalidation. Unchanged rows are untouched — Qt keeps its
        cached row heights, selection state, and delegate renders intact,
        which eliminates the per-second full-table repaint stutter.
        """
        new_map: dict[str, PeerView] = {p.key: p for p in peers}

        # ---- removals (reverse order to keep indices stable)
        for i in range(len(self._rows) - 1, -1, -1):
            if self._rows[i].key not in new_map:
                self.beginRemoveRows(QModelIndex(), i, i)
                gone = self._rows.pop(i)
                self._display_cache.pop(gone.key, None)
                self.endRemoveRows()

        # ---- insertions
        current_keys = {p.key for p in self._rows}
        new_peers = [p for p in peers if p.key not in current_keys]
        if new_peers:
            first = len(self._rows)
            self.beginInsertRows(QModelIndex(), first, first + len(new_peers) - 1)
            self._rows.extend(new_peers)
            self.endInsertRows()

        # ---- update peer objects in-place (rates / state may have changed)
        for i, peer in enumerate(self._rows):
            if peer.key in new_map:
                self._rows[i] = new_map[peer.key]

        # ---- emit dataChanged only for rows whose display values differ
        for row_idx, peer in enumerate(self._rows):
            snap = _display_snapshot(peer, self._view_model)
            if snap != self._display_cache.get(peer.key):
                self._display_cache[peer.key] = snap
                tl = self.index(row_idx, 0)
                br = self.index(row_idx, _NUM_COLUMNS - 1)
                self.dataChanged.emit(tl, br, [Qt.ItemDataRole.DisplayRole])

        self.sort(self._sort_column, self._sort_order)
        self.counts_changed.emit()

    def clear(self) -> None:
        """Empty the table."""
        self.beginResetModel()
        self._rows = []
        self._display_cache.clear()
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

