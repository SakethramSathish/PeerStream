"""The command centre: the whole client on one screen.

This is the screen that answers "is it working?" without a click. It shows what
is measured — rates, peers, progress, share ratio — and it shows completeness:
a panel with no data says so rather than drawing a confident flat line.

The graphs here are fed by the session view model's bounded buffers, so they
redraw at a fixed cadence however fast the swarm is, and they never grow past
the sample window.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from app.services.torrent_service import TorrentView
from app.ui.format import human_bytes, human_duration, human_rate, human_ratio
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.session_vm import SessionSummary, SessionViewModel
from app.ui.widgets.empty_state import EmptyState
from app.ui.widgets.health_meter import HealthMeter
from app.ui.widgets.sparkline import Sparkline
from app.ui.widgets.stat_card import StatGrid
from app.ui.widgets.torrent_row import TorrentRow

# How many torrents the command centre previews. The library is one click away;
# a dashboard is not a list.
PREVIEW_ROWS: int = 4


class _Panel(QFrame):
    """A titled card. Every panel on this screen is one."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("kind", "card")
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(
            TOKENS.spacing.lg, TOKENS.spacing.md, TOKENS.spacing.lg, TOKENS.spacing.lg
        )
        self._column.setSpacing(TOKENS.spacing.md)

        heading = QLabel(title.upper())
        heading.setProperty("role", "small")
        self._column.addWidget(heading)

    @property
    def body(self) -> QVBoxLayout:
        """Where the panel's content goes."""
        return self._column


class CommandCenterView(QWidget):
    """The dashboard.

    Args:
        view_model: The session view model to read from.
        parent: Qt parent.
    """

    def __init__(self, view_model: SessionViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vm = view_model
        self._rows: dict[str, TorrentRow] = {}
        # Rows are created lazily, so their signals are wired through these
        # slots, which the window replaces with the real handlers.
        self.selected = _noop
        self.pause_toggled = _noop
        self.remove_requested = _noop

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._column = QVBoxLayout(inner)
        self._column.setContentsMargins(
            TOKENS.spacing.xl, TOKENS.spacing.lg, TOKENS.spacing.xl, TOKENS.spacing.xl
        )
        self._column.setSpacing(TOKENS.spacing.lg)

        # ---- counters
        self._stats = StatGrid(4, inner)
        self._stats.set_alignment_top()
        for key, label, detail in (
            ("download", "Download", "0 B"),
            ("upload", "Upload", "0 B"),
            ("peers", "Peers", "none connected"),
            ("ratio", "Share ratio", "no data yet"),
        ):
            self._stats.add(key, label, "", detail=detail)
        self._stats["download"].set_value("0 B/s", role="accent")
        self._stats["upload"].set_value("0 B/s", role="success")
        self._column.addWidget(self._stats)

        # ---- throughput graphs
        graphs = QHBoxLayout()
        graphs.setSpacing(TOKENS.spacing.lg)
        self._down_graph, down_panel = self._graph("Download rate", "#4C8DFF")
        self._up_graph, up_panel = self._graph("Upload rate", "#35C48A")
        graphs.addWidget(down_panel, stretch=1)
        graphs.addWidget(up_panel, stretch=1)
        self._column.addLayout(graphs)

        # ---- health
        health = _Panel("Health", inner)
        self._peer_health = HealthMeter("Peers willing to serve")
        self._piece_health = HealthMeter("Pieces verified")
        self._activity_health = HealthMeter("Torrents doing something")
        for meter in (self._peer_health, self._piece_health, self._activity_health):
            health.body.addWidget(meter)
        self._column.addWidget(health)

        # ---- active torrents
        active = _Panel("Active torrents", inner)
        self._active_body = active.body
        self._empty = EmptyState(
            "Nothing is transferring",
            "Add a torrent to see it here. Until then there are no rates to "
            "show, so none are invented.",
            icon_name="library",
        )
        active.body.addWidget(self._empty)
        self._column.addWidget(active)

        self._column.addStretch(1)
        scroll.setWidget(inner)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.refresh()

    # ------------------------------------------------------------------ drawing

    def _graph(self, title: str, colour: str) -> tuple[Sparkline, _Panel]:
        panel = _Panel(title, self)
        graph = Sparkline(colour=colour, parent=panel)
        panel.body.addWidget(graph)
        caption = QLabel("")
        caption.setProperty("role", "faint")
        caption.setStyleSheet(f"font-size: {TOKENS.type.caption}pt;")
        graph.point_hovered.connect(
            lambda sample, caption=caption: caption.setText(
                "" if sample is None else f"{human_rate(sample[1])}"
            )
        )
        panel.body.addWidget(caption)
        return graph, panel

    @property
    def empty(self) -> EmptyState:
        """The placeholder shown when nothing is transferring."""
        return self._empty

    def refresh(self) -> None:
        """Redraw everything from the view model."""
        summary: SessionSummary = self._vm.summary
        self._stats["download"].set_value(human_rate(summary.download_rate))
        self._stats["download"].set_detail(f"{human_bytes(summary.downloaded_bytes)} total")
        self._stats["upload"].set_value(human_rate(summary.upload_rate))
        self._stats["upload"].set_detail(f"{human_bytes(summary.uploaded_bytes)} total")

        self._stats["peers"].set_value(str(summary.peers_connected))
        self._stats["peers"].set_detail(
            f"{summary.peers_unchoked} unchoked" if summary.peers_connected else "none connected"
        )
        self._stats["ratio"].set_value(human_ratio(summary.share_ratio))
        eta = human_duration(summary.eta_seconds)
        self._stats["ratio"].set_detail(
            "eta " + eta if summary.torrent_count else "no torrents yet"
        )

        self._down_graph.set_series(self._vm.download_series)
        self._up_graph.set_series(self._vm.upload_series)

        self._peer_health.set_health(self._vm.peer_health.fraction, note=self._vm.peer_health.note)
        self._piece_health.set_health(
            self._vm.piece_health.fraction, note=self._vm.piece_health.note
        )
        self._activity_health.set_health(
            self._vm.activity_health.fraction, note=self._vm.activity_health.note
        )

        self._sync_rows()

    def _sync_rows(self) -> None:
        """Show at most :data:`PREVIEW_ROWS` torrents, reusing row widgets."""
        wanted = [view for view in self._vm.torrents if view.active][:PREVIEW_ROWS]
        if not wanted:
            self._empty.show()
            for row in self._rows.values():
                row.hide()
            return
        self._empty.hide()

        seen: set[str] = set()
        for view in wanted:
            row = self._rows.get(view.info_hash) or self._make_row(view)
            row.update_view(view)
            row.show()
            seen.add(view.info_hash)
        for key, row in self._rows.items():
            if key not in seen:
                row.hide()

    def _make_row(self, view: TorrentView) -> TorrentRow:
        """Build (and keep) a row for a torrent the dashboard has not shown yet."""
        row = TorrentRow(view, self)
        row.selected.connect(self.selected)
        row.pause_toggled.connect(self.pause_toggled)
        row.remove_requested.connect(self.remove_requested)
        self._rows[view.info_hash] = row
        self._active_body.addWidget(row)
        return row

    # ------------------------------------------------------------------ plumbing

    def connect_rows(self, on_selected, on_pause, on_remove) -> None:  # type: ignore[no-untyped-def]
        """Point the preview rows' signals at the window's handlers."""
        self.selected = on_selected
        self.pause_toggled = on_pause
        self.remove_requested = on_remove


def _noop(info_hash: str) -> None:
    """The default row handler: a row created before anyone is listening."""
    _ = info_hash
