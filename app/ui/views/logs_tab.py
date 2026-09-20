"""The protocol timeline tab (PRD §10.10).

Every event the engine emits, newest first, with the filters the specification
names: ALL, NETWORK, TRACKER, PEER, PIECE, DISK and ERROR.

Two details that make it a tool rather than a scrollbar:

* **ERROR is not a category.** It is a filter over severity, so a disk failure
  and a tracker failure both appear under it even though the engine files them
  under ``DISK`` and ``TRACKER`` respectively.
* **The buffer is bounded, and says so.** Five hundred events by default; when
  old ones fall off the end, the caption counts what is being held rather than
  pretending the list is the whole history.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.ui.models.log_model import LogTableModel
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.logs_vm import FILTERS, LogsViewModel
from app.ui.widgets.panel import Panel


class LogsTab(QWidget):
    """The timeline, filtered and searched.

    Args:
        view_model: The bounded log to show.
        parent: Qt parent.
    """

    def __init__(self, view_model: LogsViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vm = view_model

        column = QVBoxLayout(self)
        column.setContentsMargins(
            TOKENS.spacing.xl, TOKENS.spacing.lg, TOKENS.spacing.xl, TOKENS.spacing.xl
        )
        column.setSpacing(TOKENS.spacing.lg)

        panel = Panel("Protocol timeline", parent=self)
        self._panel = panel

        # ---- filters
        buttons = QHBoxLayout()
        buttons.setSpacing(TOKENS.spacing.sm)
        self._buttons: dict[str, QPushButton] = {}
        for name in FILTERS:
            button = QPushButton(name.title(), panel)
            button.setCheckable(True)
            button.setChecked(name == "ALL")
            button.clicked.connect(lambda _checked=False, wanted=name: self._on_filter(wanted))
            buttons.addWidget(button)
            self._buttons[name] = button
        buttons.addStretch(1)
        panel.body.addLayout(buttons)

        # ---- search and scope
        controls = QHBoxLayout()
        controls.setSpacing(TOKENS.spacing.sm)
        self._search = QLineEdit(panel)
        self._search.setPlaceholderText("Search messages…")
        self._search.setAccessibleName("Search log messages")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_query)
        controls.addWidget(self._search, stretch=1)

        self._scope = QPushButton("This torrent only", panel)
        self._scope.setCheckable(True)
        self._scope.toggled.connect(self._on_scope)
        controls.addWidget(self._scope)
        panel.body.addLayout(controls)

        # ---- table
        self._model = LogTableModel(view_model, self)
        self._table = QTableView(panel)
        # A screen reader announces a table by its name, and "table" alone does
        # not say which of the six it landed on.
        self._table.setAccessibleName("Event log")
        self._table.setAccessibleDescription(
            "Every event this client has recorded, newest last. Filter by category above."
        )
        self._table.setModel(self._model)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        panel.body.addWidget(self._table, stretch=1)

        self._caption = QLabel("")
        self._caption.setProperty("role", "faint")
        panel.body.addWidget(self._caption)
        column.addWidget(panel, stretch=1)

        view_model.changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------ access

    @property
    def model(self) -> LogTableModel:
        """The log table model."""
        return self._model

    @property
    def table(self) -> QTableView:
        """The log table."""
        return self._table

    @property
    def search(self) -> QLineEdit:
        """The search box."""
        return self._search

    # ------------------------------------------------------------------- input

    def set_torrent(self, hex_info_hash: str | None) -> None:
        """Scope the timeline to one torrent (or to everything, with ``None``)."""
        self._vm.set_torrent(hex_info_hash)
        self._scope.setChecked(hex_info_hash is not None)
        self._scope.setEnabled(hex_info_hash is not None)

    def set_filter(self, name: str) -> None:
        """Choose a filter, updating the buttons to match."""
        self._vm.set_filter(name)
        for label, button in self._buttons.items():
            button.setChecked(label == self._vm.filtername)

    # ------------------------------------------------------------------- events

    def _on_filter(self, name: str) -> None:
        self.set_filter(name)

    def _on_query(self, text: str) -> None:
        self._vm.set_query(text)
        self.refresh()

    def _on_scope(self, checked: bool) -> None:
        self._vm.set_torrent(self._vm.torrent if checked else None)
        self.refresh()

    # ------------------------------------------------------------------ drawing

    def refresh(self) -> None:
        """Redraw the caption and the rows."""
        state = self._vm.as_dict()
        visible = state["visible"]
        recorded = state["recorded"]
        self._model.refresh()
        scope = "this torrent" if self._vm.torrent else "the whole session"
        self._caption.setText(
            f"{visible} of {recorded} events shown · {scope} · holding the last {self._vm.capacity}"
        )
        self._panel.set_subtitle(
            f"filter {str(state['filter']).lower()}"
            + (f" · matching “{state['query']}”" if state["query"] else "")
        )
