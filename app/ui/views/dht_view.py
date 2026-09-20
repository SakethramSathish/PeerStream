"""The DHT screen: our node, and the part of the network it can see.

A DHT is not a feature you turn on and forget, it is a network you are either
in or not in. So the screen leads with the one thing worth knowing — are we in
it, and with how many neighbours — and then shows the routing table itself,
which is the honest answer to "who are you talking to?".

Nothing here is estimated. The numbers are the routing table's own: contacts
are nodes that answered a question, "last seen" is the last time one did, and
failures are counted. When the DHT is off, or on but alone after bootstrap, the
screen says which — a table of zeroes would read the same either way, and those
two states demand different action from the user.
"""

from __future__ import annotations

from PySide6.QtCore import QItemSelection, QModelIndex
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.ui.models.dht_model import DhtTableModel
from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.dht_vm import DhtContactView, DhtSummary, DhtViewModel
from app.ui.widgets.panel import Panel
from app.ui.widgets.stat_card import StatCard


class DhtView(QWidget):
    """The DHT page.

    Args:
        view_model: The DHT's state, read off the node.
        palette: Colours.
        parent: Qt parent.
    """

    def __init__(
        self,
        view_model: DhtViewModel,
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

        title = QLabel("Distributed hash table")
        title.setProperty("role", "strong")
        title.setStyleSheet(f"font-size: {TOKENS.type.title}pt;")
        column.addWidget(title)

        self._note = QLabel("")
        self._note.setProperty("role", "muted")
        self._note.setWordWrap(True)
        column.addWidget(self._note)

        # ---- summary cards
        cards = QHBoxLayout()
        cards.setSpacing(TOKENS.spacing.md)
        self._status = StatCard("status", "--")
        self._nodes = StatCard("nodes known", "0")
        self._buckets = StatCard("buckets", "0")
        self._announcements = StatCard("peers held", "0")
        self._published = StatCard("published", "0")
        for card in (
            self._status,
            self._nodes,
            self._buckets,
            self._announcements,
            self._published,
        ):
            cards.addWidget(card, stretch=1)
        column.addLayout(cards)

        # ---- routing table
        self._table_model = DhtTableModel(self._vm, self)
        table_panel = Panel(
            "routing table",
            "Nodes we have spoken to, closest first. A node that stops answering is dropped.",
        )
        self._table = QTableView()
        self._table.setAccessibleName("DHT nodes")
        self._table.setAccessibleDescription(
            "Nodes in the routing table, closest first, with when each last answered."
        )
        self._table.setObjectName("dht_table")
        self._table.setModel(self._table_model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        table_panel.body.addWidget(self._table)
        column.addWidget(table_panel, stretch=1)

        self._empty = QLabel("No nodes yet. Bootstrap runs when the DHT starts.")
        self._empty.setProperty("role", "faint")
        self._empty.setVisible(False)
        column.addWidget(self._empty)

        self.refresh()

    # ------------------------------------------------------------------ content

    @property
    def summary(self) -> DhtSummary:
        """The DHT state this page is showing."""
        return self._vm.summary

    @property
    def contacts(self) -> tuple[DhtContactView, ...]:
        """The rows in the table, closest first."""
        return self._table_model.rows

    def refresh(self) -> None:
        """Re-read the view model and redraw."""
        summary = self._vm.summary
        self._note.setText(summary.note)
        self._status.set_value("running" if summary.running else "off")
        self._nodes.set_value(str(summary.contacts))
        self._buckets.set_value(str(summary.buckets))
        self._announcements.set_value(str(summary.peers_known))
        self._status.set_detail(f"node {summary.node_id[:12]}" if summary.node_id else "")
        self._nodes.set_detail(f"udp port {summary.port}" if summary.port else "")
        self._buckets.set_detail("routing table depth")
        # Five cards in a 1440 px window: the detail line elides at about twenty-four
        # characters, so these say what they mean in that space rather than a
        # sentence that gets cut off mid-word.
        self._announcements.set_detail("announcements held")
        self._published.set_value(str(summary.published))
        self._published.set_detail(
            "torrents a node accepted" if summary.running else "the DHT is off"
        )

        self._table_model.refresh()
        self._empty.setVisible(not self._table_model.rows)
        self._table.setVisible(bool(self._table_model.rows))

    def _on_selection(self, _selected: QItemSelection, _deselected: QItemSelection) -> None:
        """Keep the row the user picked selected, and nothing else."""
        index: QModelIndex = self._table.currentIndex()
        if index.isValid():
            self._table.scrollTo(index)
