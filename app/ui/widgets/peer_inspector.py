"""The peer inspector: everything one peer has told us, in words.

The swarm canvas answers "what is this swarm doing?" in one look, and the table
answers "who is in it?". This answers the question in between: *that* one, the
node you just clicked — what is it, what has it sent, will it serve us, how did
we find it.

It reads from a :class:`~app.ui.viewmodels.peers_vm.PeersViewModel` at the
moment it is asked, so an inspector left open on a peer that disconnects shows
the peer's last known state rather than a stale copy. The "Clear" button is the
only control: an inspector that could not be dismissed would be a panel that
never lets you look at the swarm again.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.services.torrent_service import PeerView
from app.ui.format import UNKNOWN, human_bytes, human_duration, human_percent, human_rate
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.peers_vm import PeerActivity, PeersViewModel


class PeerInspector(QFrame):
    """One peer, in a box, with a way out.

    Args:
        view_model: Source of the peer's rates.
        parent: Qt parent.
    """

    def __init__(self, view_model: PeersViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vm = view_model
        self._peer: PeerView | None = None

        self.setProperty("kind", "card")
        column = QVBoxLayout(self)
        column.setContentsMargins(
            TOKENS.spacing.lg, TOKENS.spacing.md, TOKENS.spacing.lg, TOKENS.spacing.md
        )
        column.setSpacing(TOKENS.spacing.xs)

        header = QHBoxLayout()
        self._title = QLabel("No peer selected")
        self._title.setProperty("role", "small")
        header.addWidget(self._title, stretch=1)
        self._clear = QPushButton("Clear", self)
        self._clear.setFlat(True)
        self._clear.clicked.connect(self.clear)
        header.addWidget(self._clear)
        column.addLayout(header)

        self._body = QLabel("Click a node to inspect it.")
        self._body.setProperty("role", "faint")
        self._body.setWordWrap(True)
        self._body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        column.addWidget(self._body)

    # ------------------------------------------------------------------ access

    @property
    def peer(self) -> PeerView | None:
        """The peer being inspected, or ``None``."""
        return self._peer

    @property
    def title(self) -> QLabel:
        return self._title

    @property
    def body(self) -> QLabel:
        return self._body

    @property
    def clear_button(self) -> QPushButton:
        return self._clear

    # ------------------------------------------------------------------ control

    def inspect(self, peer: PeerView | None) -> None:
        """Show this peer, or nothing at all with ``None``."""
        self._peer = peer
        self.refresh()

    def clear(self) -> None:
        """Stop inspecting: the swarm is more interesting than one node."""
        self._peer = None
        self.refresh()

    def refresh(self) -> None:
        """Re-read the peer's rates and redraw."""
        peer = self._peer
        if peer is None:
            self._title.setText("No peer selected")
            self._body.setText("Click a node to inspect it.")
            return
        activity = self._vm.activity_for(peer.key)
        self._title.setText(f"{peer.client} · {peer.label}")
        self._body.setText("\n".join(_lines(peer, activity)))


def _lines(peer: PeerView, activity: PeerActivity) -> list[str]:
    """Everything worth saying about one peer, one fact per line."""
    lines = [
        _state_line(peer),
        f"down {human_rate(activity.down_rate)} · up {human_rate(activity.up_rate)}",
        f"got {human_bytes(peer.downloaded)} · sent {human_bytes(peer.uploaded)}",
    ]
    if peer.piece_count:
        lines.append(
            f"holds {peer.pieces_held}/{peer.piece_count} pieces ({human_percent(peer.share)})"
        )
    details: list[str] = []
    if peer.latency_ms is not None:
        details.append(f"{peer.latency_ms:.0f} ms round trip")
    if peer.state == "connected":
        details.append(f"{peer.blocks_in_flight} request(s) in flight")
        details.append(f"idle {human_duration(peer.idle_for)}")
    if peer.source:
        details.append(f"found via {peer.source}")
    if details:
        lines.append(" · ".join(details))
    return lines


def _state_line(peer: PeerView) -> str:
    """What the peer is doing, which is the first thing a person asks."""
    if peer.state != "connected":
        return f"{peer.state} — not connected yet" if peer.state else UNKNOWN
    if peer.complete:
        return "seed" + (" · serving us" if not peer.choking_us else " · choking us")
    return "leech" + (" · serving us" if not peer.choking_us else " · choking us")
