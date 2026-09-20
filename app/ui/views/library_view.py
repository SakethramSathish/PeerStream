"""The library: every torrent the client knows about.

The list is the honest core of the interface. It shows what has been measured
for each torrent — progress, rate, ETA, peers — and it keeps a stable order, so
a torrent does not jump around the screen every time a rate changes.

Rows are reused rather than rebuilt: a client with two hundred torrents should
not allocate two hundred widgets twice a second. Adding and removing torrents
changes the row set; updating one changes only its text.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from app.services.torrent_service import TorrentView
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.session_vm import SessionViewModel
from app.ui.widgets.empty_state import EmptyState
from app.ui.widgets.torrent_row import TorrentRow

# Every state the user can filter by, plus "all". The filter matches the state
# the engine reports, not a guess about it.
ALL: str = "all"


class LibraryView(QWidget):
    """The torrent list, with a filter and an honest empty state.

    Args:
        view_model: The session view model to read from.
        parent: Qt parent.
    """

    torrent_selected = Signal(str)
    pause_toggled = Signal(str)
    remove_requested = Signal(str)
    add_requested = Signal()
    open_location = Signal(str)
    """Emitted with the info hash when the user asks to open the download folder."""

    def __init__(self, view_model: SessionViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vm = view_model
        self._rows: dict[str, TorrentRow] = {}
        self._filter = ALL
        spacing = TOKENS.spacing

        column = QVBoxLayout(self)
        column.setContentsMargins(spacing.xl, spacing.lg, spacing.xl, spacing.xl)
        column.setSpacing(spacing.md)

        header = QHBoxLayout()
        heading = QLabel("Library")
        heading.setProperty("role", "strong")
        heading.setStyleSheet(f"font-size: {TOKENS.type.title}pt;")
        header.addWidget(heading)
        header.addStretch(1)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by name")
        # A placeholder is not a name: it disappears the moment you type, and a
        # screen reader never had it. This is what the control is called.
        self._search.setAccessibleName("Filter torrents by name")
        self._search.setMinimumHeight(TOKENS.geometry.control_height)
        self._search.setMaximumWidth(260)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self.refresh)
        header.addWidget(self._search)
        column.addLayout(header)

        self._count = QLabel("")
        self._count.setProperty("role", "faint")
        self._count.setStyleSheet(f"font-size: {TOKENS.type.caption}pt;")
        column.addWidget(self._count)

        self._list = QFrame(self)
        self._list.setProperty("kind", "card")
        self._column = QVBoxLayout(self._list)
        self._column.setContentsMargins(spacing.sm, spacing.sm, spacing.sm, spacing.sm)
        self._column.setSpacing(spacing.xs)
        self._column.setAlignment(Qt.AlignmentFlag.AlignTop)
        column.addWidget(self._list, stretch=1)

        self._empty = EmptyState(
            "No torrents yet",
            "Add a .torrent file to get started. The client will connect to the "
            "tracker, find peers, and show every step as it happens.",
            icon_name="library",
        )
        self._empty.set_action("Add torrent")
        self._empty.action_requested.connect(self.add_requested.emit)
        self._column.addWidget(self._empty)

        self.refresh()

    # ------------------------------------------------------------------ content

    @property
    def rows(self) -> tuple[TorrentRow, ...]:
        """The row widgets currently in the list."""
        return tuple(self._rows.values())

    @property
    def visible_count(self) -> int:
        """How many rows the filter is showing."""
        return sum(1 for row in self._rows.values() if not row.isHidden())

    def refresh(self) -> None:
        """Rebuild the visible rows from the view model."""
        wanted = self._matching()
        self._empty.setVisible(not wanted)
        self._count.setText(_count_text(len(wanted), len(self._vm.torrents)))

        seen: set[str] = set()
        for view in wanted:
            row = self._rows.get(view.info_hash) or self._make_row(view)
            row.update_view(view)
            row.show()
            seen.add(view.info_hash)
        for key, row in self._rows.items():
            if key not in seen:
                row.hide()

    # ------------------------------------------------------------------ plumbing

    def _matching(self) -> tuple[TorrentView, ...]:
        needle = self._search.text().strip().lower()
        return tuple(
            view
            for view in self._vm.torrents
            if not needle or needle in view.name.lower() or needle in view.info_hash.lower()
        )

    def _make_row(self, view: TorrentView) -> TorrentRow:
        row = TorrentRow(view, self._list)
        row.selected.connect(self.torrent_selected.emit)
        row.pause_toggled.connect(self.pause_toggled.emit)
        row.remove_requested.connect(self.remove_requested.emit)
        row.open_location.connect(self.open_location.emit)
        self._rows[view.info_hash] = row
        self._column.addWidget(row)
        return row

    def forget_all(self) -> int:
        """Drop every row. Used when the window is reused (tests, re-adding)."""
        count = 0
        for hex_info_hash in tuple(self._rows):
            count += self.forget(hex_info_hash)
        return count

    def forget(self, hex_info_hash: str) -> bool:
        """Drop a row entirely, once its torrent has been removed."""
        row = self._rows.pop(hex_info_hash, None)
        if row is None:
            return False
        self._column.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        return True

    # ------------------------------------------------------------------ actions

    def focus_filter(self) -> None:
        """Put the cursor in the filter box (bound to Ctrl+F by the window)."""
        self._search.setFocus()
        self._search.selectAll()


def _count_text(shown: int, total: int) -> str:
    """The line under the heading: how many are shown, and how many exist."""
    if total == 0:
        return "no torrents in the session"
    if shown == total:
        return f"{total} torrent" + ("s" if total != 1 else "")
    return f"showing {shown} of {total}"
