"""The detail overview tab (PRD §10.4).

Everything about one torrent that fits on one screen: the rate graph, four
counters, the completion bar, and the table of facts — size, piece length, info
hash, save path, share ratio, ETA.

The tab is a *pull*: it redraws when the window refreshes it, from the torrent
view model, and it holds no timers of its own. A widget that polled for its own
data would keep polling while hidden.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHeaderView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.ui.charts.rate_graph import RateGraph
from app.ui.format import human_bytes, human_duration, human_percent, human_rate, human_ratio
from app.ui.models.torrent_model import TorrentFieldModel
from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.torrent_vm import TorrentViewModel
from app.ui.widgets.empty_state import EmptyState
from app.ui.widgets.panel import Panel
from app.ui.widgets.progress_bar import SegmentedBar
from app.ui.widgets.stat_card import StatGrid


class OverviewTab(QWidget):
    """One torrent, on one screen.

    Args:
        view_model: The torrent to show.
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

        self._stats = StatGrid(4, self)
        for key, label, detail in (
            ("progress", "Progress", "no data yet"),
            ("down", "Download", "0 B/s"),
            ("up", "Upload", "0 B/s"),
            ("eta", "ETA", "unknown"),
        ):
            self._stats.add(key, label, "", detail=detail)
        column.addWidget(self._stats)

        # ---- completion
        self._progress_panel = Panel("Completion", parent=self)
        self._bar = SegmentedBar(24, parent=self)
        self._progress_panel.body.addWidget(self._bar)
        column.addWidget(self._progress_panel)

        # ---- rates
        rates = Panel("Throughput", "Both series are measured; nothing is extrapolated.", self)
        self._graph = RateGraph(palette, parent=rates)
        rates.body.addWidget(self._graph)
        column.addWidget(rates)

        # ---- facts
        facts = Panel("Details", parent=self)
        self._fields = TorrentFieldModel(view_model, facts)
        self._table = QTableView(facts)
        self._table.setAccessibleName("Torrent facts")
        self._table.setAccessibleDescription("Name, size, pieces and trackers for this torrent.")
        self._table.setModel(self._fields)
        self._table.horizontalHeader().setVisible(False)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setShowGrid(False)
        facts.body.addWidget(self._table)
        column.addWidget(facts)

        self._empty = EmptyState(
            "No torrent selected",
            "Choose one in the library to see its rates, pieces and peers.",
            icon_name="library",
        )
        column.addWidget(self._empty)
        column.addStretch(1)
        self.refresh()

    # ------------------------------------------------------------------ drawing

    @property
    def empty(self) -> EmptyState:
        """The placeholder shown when no torrent is selected."""
        return self._empty

    @property
    def graph(self) -> RateGraph:
        """The rate graph, for tests and for hover wiring."""
        return self._graph

    def refresh(self) -> None:
        """Redraw from the view model."""
        view = self._vm.view
        selected = view is not None
        self._empty.setVisible(not selected)
        self._progress_panel.setVisible(selected)
        self._table.setVisible(selected)
        if view is None:
            self._stats["progress"].set_value("--")
            self._stats["down"].set_value("--")
            self._stats["up"].set_value("--")
            self._stats["eta"].set_value("--")
            self._graph.clear()
            return

        metrics = view.metrics
        self._stats["progress"].set_value(human_percent(view.progress))
        self._stats["progress"].set_detail(
            f"{view.verified_pieces}/{view.piece_count} pieces · "
            f"{human_bytes(int(view.total_length * view.progress))} of "
            f"{human_bytes(view.total_length)}"
        )
        self._stats["down"].set_value(human_rate(self._vm.download_rate), role="accent")
        self._stats["down"].set_detail(
            f"{human_bytes(metrics.download.total)} total" if metrics else "no data yet"
        )
        self._stats["up"].set_value(human_rate(self._vm.upload_rate), role="success")
        self._stats["up"].set_detail(
            f"{human_bytes(metrics.upload.total)} total" if metrics else "no data yet"
        )
        self._stats["eta"].set_value(human_duration(self._vm.eta_seconds))
        self._stats["eta"].set_detail(
            human_ratio(metrics.share_ratio) + " shared" if metrics else "no data yet"
        )

        self._bar.set_fraction(view.progress)
        self._progress_panel.set_subtitle(
            f"{view.state.value.replace('_', ' ')} · {human_percent(view.progress)} verified"
        )
        self._graph.set_series(self._vm.download_series, self._vm.upload_series)
        self._fields.refresh()

    def set_palette(self, palette: Palette) -> None:
        """Recolour, for when the theme changes."""
        self._palette = palette
        self._graph.set_palette(palette)
