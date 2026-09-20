"""The torrent detail page: one torrent, six tabs (PRD §10.4-10.10).

The shell is deliberately thin. It owns the tab widget, a header with the
torrent's name and state, and the job of refreshing whichever tab is visible —
because refreshing six tabs ten times a second would be the sort of thing that
makes an interface feel slow for no reason.

Each tab is fed by the same :class:`~app.ui.viewmodels.torrent_vm.TorrentViewModel`,
so the numbers on the overview, the colours in the matrix and the rows in the
peers table are the same measurement read three ways. The slower reads — the
swarm, the pieces, the files, the trackers — are pushed in from outside, on the
engine's loop, by whoever owns the session.

Args (constructor): see :meth:`TorrentDetailView.__init__`.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTabWidget, QVBoxLayout, QWidget

from app.services.torrent_service import FileView
from app.tracker.base import TrackerStatus
from app.ui.format import human_percent
from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.logs_vm import LogsViewModel
from app.ui.viewmodels.torrent_vm import TorrentViewModel
from app.ui.views.files_tab import FilesTab
from app.ui.views.logs_tab import LogsTab
from app.ui.views.overview_tab import OverviewTab
from app.ui.views.peers_tab import PeersTab
from app.ui.views.pieces_tab import PiecesTab
from app.ui.views.trackers_tab import TrackersTab

TABS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("peers", "Peers"),
    ("pieces", "Pieces"),
    ("files", "Files"),
    ("trackers", "Trackers"),
    ("log", "Log"),
)


class TorrentDetailView(QWidget):
    """One torrent, in six views.

    Args:
        view_model: The torrent to show.
        logs: The session's bounded, filtered timeline.
        palette: Colours.
        parent: Qt parent.
    """

    def __init__(
        self,
        view_model: TorrentViewModel,
        logs: LogsViewModel,
        palette: Palette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._vm = view_model
        self._logs = logs
        self._palette = palette
        self.pause_requested = _noop
        self.remove_requested = _noop

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        # ---- header
        header = QWidget(self)
        row = QHBoxLayout(header)
        row.setContentsMargins(
            TOKENS.spacing.xl, TOKENS.spacing.lg, TOKENS.spacing.xl, TOKENS.spacing.md
        )
        row.setSpacing(TOKENS.spacing.md)
        self._title = QLabel("No torrent selected")
        self._title.setProperty("role", "title")
        self._state = QLabel("")
        self._state.setProperty("role", "faint")
        row.addWidget(self._title, stretch=1)
        row.addWidget(self._state)

        self._pause = QPushButton("Pause", header)
        self._pause.clicked.connect(lambda: self.pause_requested(self._vm.info_hash or ""))
        row.addWidget(self._pause)
        self._remove = QPushButton("Remove", header)
        self._remove.setProperty("kind", "danger")
        self._remove.clicked.connect(lambda: self.remove_requested(self._vm.info_hash or ""))
        row.addWidget(self._remove)
        column.addWidget(header)

        # ---- tabs
        self._tabs = QTabWidget(self)
        self._overview = OverviewTab(view_model, palette, self._tabs)
        self._peers = PeersTab(view_model, palette, self._tabs)
        self._pieces = PiecesTab(view_model, palette, self._tabs)
        self._files = FilesTab(self._tabs)
        self._trackers = TrackersTab(self._tabs)
        self._log = LogsTab(logs, self._tabs)
        self._pages: dict[str, QWidget] = {}
        for key, label in TABS:
            page = {
                "overview": self._overview,
                "peers": self._peers,
                "pieces": self._pieces,
                "files": self._files,
                "trackers": self._trackers,
                "log": self._log,
            }[key]
            self._pages[key] = page
            self._tabs.addTab(page, label)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        column.addWidget(self._tabs, stretch=1)

        self.refresh()

    # ------------------------------------------------------------------ access

    @property
    def tabs(self) -> QTabWidget:
        """The tab widget."""
        return self._tabs

    @property
    def current_tab(self) -> str:
        """The key of the visible tab."""
        index = self._tabs.currentIndex()
        return TABS[index][0] if 0 <= index < len(TABS) else "overview"

    @property
    def overview(self) -> OverviewTab:
        return self._overview

    @property
    def peers(self) -> PeersTab:
        return self._peers

    @property
    def pieces(self) -> PiecesTab:
        return self._pieces

    @property
    def files(self) -> FilesTab:
        return self._files

    @property
    def trackers(self) -> TrackersTab:
        return self._trackers

    @property
    def log(self) -> LogsTab:
        return self._log

    @property
    def title(self) -> QLabel:
        """The header's title label."""
        return self._title

    # ------------------------------------------------------------------- input

    def set_files(self, files: tuple[FileView, ...]) -> None:
        """Push a fresh file list (read on the engine's loop)."""
        self._files.set_files(files)

    def set_trackers(self, statuses: tuple[TrackerStatus, ...]) -> None:
        """Push fresh tracker health records."""
        self._trackers.set_statuses(statuses)

    def set_palette(self, palette: Palette) -> None:
        """Recolour, for when the theme changes."""
        self._palette = palette
        self._overview.set_palette(palette)
        self._peers.set_palette(palette)
        self._pieces.set_palette(palette)

    def set_reduced_motion(self, value: bool) -> None:
        """Pass the motion preference to the tabs that animate."""
        self._peers.set_reduced_motion(value)

    def show_tab(self, key: str) -> None:
        """Switch to one tab by key."""
        for index, (name, _label) in enumerate(TABS):
            if name == key:
                self._tabs.setCurrentIndex(index)
                return
        raise KeyError(f"no such tab: {key!r}")

    # ----------------------------------------------------------------- drawing

    def refresh(self) -> None:
        """Redraw the header and the visible tab."""
        view = self._vm.view
        if view is None:
            self._title.setText("No torrent selected")
            self._state.setText("")
            self._pause.setEnabled(False)
            self._remove.setEnabled(False)
        else:
            self._title.setText(view.name)
            self._state.setText(
                f"{view.state.value.replace('_', ' ')} · {human_percent(view.progress)} · "
                f"{view.verified_pieces}/{view.piece_count} pieces"
            )
            self._pause.setEnabled(True)
            self._remove.setEnabled(True)
            self._pause.setText("Pause" if view.active else "Resume")

        visible = {
            "overview": self._overview.refresh,
            "peers": self._peers.refresh,
            "pieces": self._pieces.refresh,
            "files": self._files.refresh,
            "trackers": self._trackers.refresh,
            "log": self._log.refresh,
        }
        visible[self.current_tab]()

    def _on_tab_changed(self, _index: int) -> None:
        """Refresh only what was just revealed: nothing was updating while hidden."""
        self.refresh()

    # ---------------------------------------------------------------- plumbing

    def connect_actions(self, on_pause, on_remove) -> None:  # type: ignore[no-untyped-def]
        """Point the header buttons at the window's handlers."""
        self.pause_requested = on_pause
        self.remove_requested = on_remove


def _noop(hex_info_hash: str) -> None:
    """The default handler: a button pressed before anyone is listening."""
    _ = hex_info_hash
