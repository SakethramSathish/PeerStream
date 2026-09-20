"""The files tab (PRD §10.7): what is on disk, file by file.

A torrent is a byte stream; people think in files. This tab translates: every
file in the torrent with its size and how much of it has verified, counted from
the pieces it spans.

Per-file progress is deliberately by pieces and not by bytes. A 10 GiB file
whose first piece verified is not "0.01 % done" in any sense a person would
recognise — it is one piece in, and saying so is both truer and more useful
when deciding what to wait for.
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

from app.services.torrent_service import FileView
from app.ui.format import human_bytes, human_percent
from app.ui.models.file_model import FileTableModel
from app.ui.theme.tokens import TOKENS
from app.ui.widgets.empty_state import EmptyState
from app.ui.widgets.panel import Panel


class FilesTab(QWidget):
    """Every file in one torrent.

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

        self._panel = Panel("Files", "Progress is counted from verified pieces.", self)
        self._model = FileTableModel(self)
        self._table = QTableView(self._panel)
        self._table.setAccessibleName("Files")
        self._table.setAccessibleDescription(
            "Every file in this torrent, with how much of each is on disk."
        )
        self._table.setModel(self._model)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._panel.body.addWidget(self._table)

        self._total = QLabel("")
        self._total.setProperty("role", "faint")
        self._panel.body.addWidget(self._total)
        column.addWidget(self._panel, stretch=1)

        self._empty = EmptyState(
            "No files listed",
            "Select a torrent to see the files it contains.",
            icon_name="folder",
        )
        column.addWidget(self._empty)
        column.addStretch(1)
        self.refresh()

    # ------------------------------------------------------------------ access

    @property
    def model(self) -> FileTableModel:
        """The files table model."""
        return self._model

    @property
    def table(self) -> QTableView:
        """The files table."""
        return self._table

    @property
    def empty(self) -> EmptyState:
        """The placeholder shown when there are no files."""
        return self._empty

    # ------------------------------------------------------------------- input

    def set_files(self, files: tuple[FileView, ...]) -> None:
        """Show these files."""
        self._model.set_files(files)
        self.refresh()

    def clear(self) -> None:
        """Forget the files."""
        self._model.clear()
        self.refresh()

    # ------------------------------------------------------------------ drawing

    def refresh(self) -> None:
        """Redraw the caption and the placeholder."""
        files = self._model.files
        self._empty.setVisible(not files)
        self._panel.setVisible(bool(files))
        if not files:
            self._total.setText("")
            return
        complete = sum(1 for row in files if row.complete)
        self._total.setText(
            f"{len(files)} files · {human_bytes(self._model.total_length)} total · "
            f"{complete} complete · {human_percent(self._model.progress)} verified"
        )
        self._panel.set_subtitle(f"{len(files)} file{'s' if len(files) != 1 else ''}")
