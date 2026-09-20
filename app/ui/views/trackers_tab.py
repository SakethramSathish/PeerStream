"""The trackers tab (PRD §10.8): what the trackers told us, and when.

One row per announce URL with its own health: how many seeders and leechers it
counted, how many peers it handed over, how long the round trip took, how many
times it has failed in a row, and when we will ask again.

The counters are the tracker's, and they are shown as such. A tracker saying
"412 seeders" while three peers are connected is not a contradiction to be
smoothed over — it is how trackers work, and the tab shows both sides of it.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.tracker.base import TrackerStatus
from app.ui.format import human_duration
from app.ui.models.tracker_model import TrackerTableModel
from app.ui.theme.tokens import TOKENS
from app.ui.widgets.empty_state import EmptyState
from app.ui.widgets.panel import Panel


class TrackersTab(QWidget):
    """Every tracker on one torrent.

    Args:
        parent: Qt parent.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(
            TOKENS.spacing.xl, TOKENS.spacing.lg, TOKENS.spacing.xl, TOKENS.spacing.xl
        )
        column.setSpacing(TOKENS.spacing.lg)

        self._panel = Panel("Trackers", "Counters are what each tracker reported.", self)
        self._model = TrackerTableModel(self)
        self._table = QTableView(self._panel)
        self._table.setModel(self._model)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._panel.body.addWidget(self._table)

        self._summary = QLabel("")
        self._summary.setProperty("role", "faint")
        self._summary.setWordWrap(True)
        self._panel.body.addWidget(self._summary)
        column.addWidget(self._panel, stretch=1)

        self._empty = EmptyState(
            "No trackers",
            "This torrent has no announce URLs. Peer exchange and DHT are not "
            "part of it yet, so nothing will be discovered.",
            icon_name="network",
        )
        column.addWidget(self._empty)
        column.addStretch(1)
        self.refresh()

    # ------------------------------------------------------------------ access

    @property
    def model(self) -> TrackerTableModel:
        """The trackers table model."""
        return self._model

    @property
    def table(self) -> QTableView:
        """The trackers table."""
        return self._table

    @property
    def empty(self) -> EmptyState:
        """The placeholder shown when the torrent has no trackers."""
        return self._empty

    # ------------------------------------------------------------------- input

    def set_statuses(self, statuses: tuple[TrackerStatus, ...]) -> None:
        """Show these tracker records."""
        self._model.set_statuses(statuses)
        self.refresh()

    def clear(self) -> None:
        """Forget the trackers."""
        self._model.clear()
        self.refresh()

    # ------------------------------------------------------------------ drawing

    def refresh(self) -> None:
        """Redraw the summary and the placeholder."""
        statuses = self._model.statuses
        self._empty.setVisible(not statuses)
        self._panel.setVisible(bool(statuses))
        if not statuses:
            self._summary.setText("")
            return
        best_seeders = max((status.seeders for status in statuses), default=0)
        best_leechers = max((status.leechers for status in statuses), default=0)
        failures = sum(status.consecutive_failures for status in statuses)
        due = [status.next_announce_at for status in statuses if status.next_announce_at]
        soonest = human_duration(max(0.0, min(due) - _now())) if due else "--"
        self._summary.setText(
            f"{len(statuses)} tracker{'s' if len(statuses) != 1 else ''} · "
            f"best count {best_seeders} seeders / {best_leechers} leechers · "
            f"next announce in {soonest}"
            + (f" · {failures} failure{'s' if failures != 1 else ''} in a row" if failures else "")
        )
        self._panel.set_subtitle(f"{len(statuses)} announce URL{'s' if len(statuses) != 1 else ''}")


def _now() -> float:
    """The wall clock, isolated so tests can freeze it."""
    import time

    return time.time()
