"""The peers tab (PRD §10.6): the swarm as a picture and as a table.

The canvas answers "what is this swarm doing?" in one look; the table answers
"what exactly is that peer doing?" for the peer you point at. They are fed by
the same :class:`~app.ui.viewmodels.peers_vm.PeersViewModel`, so they cannot
disagree: hovering a row highlights nothing the canvas has not already drawn.

Rates in the table are the same measured differences the canvas draws as edge
widths. A peer that has not been measured twice yet shows ``"--"``, not zero.
"""

from __future__ import annotations

from PySide6.QtCore import QItemSelection, QModelIndex
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.services.torrent_service import PeerView
from app.ui.charts.swarm_canvas import SwarmCanvas, SwarmNode
from app.ui.format import human_bytes, human_rate
from app.ui.models.peer_model import PeerTableModel
from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.peers_vm import PeerActivity
from app.ui.viewmodels.torrent_vm import TorrentViewModel
from app.ui.widgets.panel import Panel
from app.ui.widgets.peer_inspector import PeerInspector


class PeersTab(QWidget):
    """The swarm, and every peer in it.

    Args:
        view_model: The torrent whose swarm this is.
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

        # ---- canvas
        self._canvas_panel = Panel(
            "Swarm",
            "You are the centre. Ring size is how much a peer holds; line "
            "thickness is what it is sending.",
            self,
        )
        self._canvas = SwarmCanvas(palette, parent=self._canvas_panel)
        self._canvas_panel.body.addWidget(self._canvas, stretch=1)
        self._hovered = QLabel("")
        self._hovered.setProperty("role", "faint")
        self._hovered.setWordWrap(True)
        self._canvas_panel.body.addWidget(self._hovered)
        column.addWidget(self._canvas_panel, stretch=3)

        # ---- inspector: the node you clicked, in words
        self._inspector = PeerInspector(view_model.peers, self)
        column.addWidget(self._inspector)

        # ---- table
        self._table_panel = Panel("Peers", parent=self)
        self._model = PeerTableModel(view_model.peers, self)
        self._table = QTableView(self._table_panel)
        self._table.setAccessibleName("Peers")
        self._table.setAccessibleDescription(
            "Every peer in this swarm, with measured download and upload rates."
        )
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._canvas.peer_hovered.connect(self._on_canvas_hover)
        self._canvas.peer_selected.connect(self._on_canvas_selected)
        self._table.clicked.connect(self._on_row_clicked)
        self._table_panel.body.addWidget(self._table)
        column.addWidget(self._table_panel, stretch=2)

        self.refresh()

    # ------------------------------------------------------------------ access

    @property
    def canvas(self) -> SwarmCanvas:
        """The swarm canvas, for tests and for hover wiring."""
        return self._canvas

    @property
    def table(self) -> QTableView:
        """The peers table."""
        return self._table

    @property
    def model(self) -> PeerTableModel:
        """The peers table model."""
        return self._model

    @property
    def inspector(self) -> PeerInspector:
        """The panel that describes the selected peer."""
        return self._inspector

    # ------------------------------------------------------------------ drawing

    def refresh(self) -> None:
        """Redraw both halves from the view model."""
        peers = self._vm.peers
        self._canvas.set_our_progress(self._vm.progress)
        self._canvas.set_view_model(peers)
        self._model.set_peers(peers.peers)
        self._inspector.refresh()
        counts = peers.as_dict()
        connected = counts["connected"]
        self._canvas_panel.set_subtitle(
            f"{connected} connected · {counts['unchoked']} serving · "
            f"{counts['seeds']} seeds · {counts['candidates']} known"
            if connected or counts["candidates"]
            else "No peers yet — waiting for a tracker or a handshake."
        )
        if self._hovered.text().startswith("Selected"):
            self._refresh_selection_note()

    def set_palette(self, palette: Palette) -> None:
        """Recolour, for when the theme changes."""
        self._palette = palette
        self._canvas.set_palette(palette)

    def set_reduced_motion(self, value: bool) -> None:
        """Pass the motion preference down to the canvas."""
        self._canvas.set_reduced_motion(value)

    # ------------------------------------------------------------------ events

    def _on_canvas_hover(self, node: object) -> None:
        """Describe the peer the pointer is over."""
        if node is None:
            self._hovered.setText("")
            return
        assert isinstance(node, SwarmNode)
        self._hovered.setText(_describe(node.peer, self._vm.peers.activity_for(node.peer.key)))

    def _on_canvas_selected(self, node: object) -> None:
        """Select the table row for the peer that was clicked, and inspect it."""
        if node is None:
            return
        assert isinstance(node, SwarmNode)
        row = self._model.row_of(node.peer.key)
        if row >= 0:
            self._table.selectRow(row)
        self._inspector.inspect(node.peer)

    def _on_row_clicked(self, index: QModelIndex) -> None:
        """Inspect the peer whose row was clicked."""
        peer = self._model.peer_at(index.row())
        if peer is not None:
            self._inspector.inspect(peer)

    def _on_selection_changed(self, _selected: QItemSelection, _deselected: QItemSelection) -> None:
        """Describe the peer the table's selection is on."""
        self._refresh_selection_note()

    def _refresh_selection_note(self) -> None:
        """Show the selected peer's numbers under the table."""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        peer = self._model.peer_at(rows[0].row())
        if peer is None:
            return
        activity = self._vm.peers.activity_for(peer.key)
        self._hovered.setText("Selected: " + _describe(peer, activity))


def _describe(peer: PeerView, activity: PeerActivity) -> str:
    """One peer, in a sentence, with the numbers we have."""
    parts = [f"{peer.client} ({peer.label})"]
    parts.append(
        f"holds {peer.pieces_held}/{peer.piece_count}" if peer.piece_count else "pieces unknown"
    )
    parts.append(f"down {human_rate(activity.down_rate)}")
    parts.append(f"up {human_rate(activity.up_rate)}")
    parts.append(f"got {human_bytes(peer.downloaded)}")
    if peer.state == "connected":
        parts.append("serving us" if not peer.choking_us else "choking us")
    else:
        parts.append(peer.state)
    if peer.source:
        parts.append(f"via {peer.source}")
    return " · ".join(parts)
