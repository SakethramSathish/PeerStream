"""Main window: navigation, live data, and the actions a user can take.

This is the only class that knows about every screen. What it does *not* know
about is the engine: it holds an :class:`~app.ui.bridge.EngineBridge`, and every
action is a coroutine handed to the bridge and forgotten — no widget in this
client can block on a socket, because a widget that blocks stops painting.

The wiring has three moving parts and they are deliberately separate:

1. **Measurements** arrive by pull: the bridge's pump emits a snapshot every
   200 ms, the view model reduces it, and the visible page redraws. Pages that
   are not visible are not redrawn — a hidden widget has nothing to show for
   the work.
2. **Events** arrive by push: the bridge's feed emits each event as it happens.
   Every one is recorded in the timeline view model; only the ones a user would
   want interrupted for also become toasts. A toast for every block received
   would be noise; a toast for "tracker refused us" is information.
3. **Actions** go out through :meth:`MainWindow._run`, which submits a coroutine
   and arranges a refresh when it settles. Nothing awaits on the GUI thread.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QResizeEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.config import Config
from app.core.events import Event, EventType
from app.discovery.magnet_resolver import MagnetResolution
from app.services.app_state import AppSnapshot
from app.services.session import Session
from app.services.torrent_service import TorrentService
from app.torrent import Torrent
from app.torrent.magnet import MagnetUri
from app.ui.bridge import EngineBridge
from app.ui.format import human_bytes, human_duration
from app.ui.theme import palette_for
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.dht_vm import DhtViewModel
from app.ui.viewmodels.logs_vm import LogsViewModel
from app.ui.viewmodels.session_vm import SessionSummary, SessionViewModel
from app.ui.viewmodels.torrent_vm import TorrentViewModel
from app.ui.views.command_center_view import CommandCenterView
from app.ui.views.dht_view import DhtView
from app.ui.views.library_view import LibraryView
from app.ui.views.logs_tab import LogsTab
from app.ui.views.settings_view import SettingsView
from app.ui.views.torrent_detail_view import TorrentDetailView
from app.ui.widgets.add_torrent_dialog import AddTorrentDialog
from app.ui.widgets.sidebar import Sidebar
from app.ui.widgets.toast import ToastHost
from app.ui.widgets.topbar import TopBar

logger = logging.getLogger(__name__)

#: The sidebar's destinations: key, label, icon.
DESTINATIONS: tuple[tuple[str, str, str], ...] = (
    ("overview", "Command centre", "overview"),
    ("library", "Library", "library"),
    ("detail", "Torrent detail", "network"),
    ("logs", "Protocol log", "logs"),
    ("dht", "DHT", "dht"),
    ("settings", "Settings", "settings"),
)

#: How often the detail page re-reads the swarm and the pieces. Slower than the
#: 200 ms snapshot, because a swarm does not change meaningfully five times a
#: second and every read is a round trip to the engine loop.
DETAIL_POLL_MS: int = 1000

#: Files and trackers change far more slowly than peers and pieces, so they are
#: read on every nth poll rather than every time.
SLOW_POLL_EVERY: int = 5

#: Which event types are worth interrupting the user for, and with what colour.
#: Everything else goes to the timeline and nowhere else.
NOTABLE: dict[EventType, str] = {
    EventType.TORRENT_COMPLETED: "success",
    EventType.TRACKER_FAILED: "error",
    EventType.TRACKER_WARNING: "warning",
    EventType.PEER_FAILED: "warning",
    EventType.PIECE_FAILED: "warning",
    EventType.DISK_ERROR: "error",
    EventType.TORRENT_ADDED: "info",
    EventType.TORRENT_REMOVED: "info",
}


class MainWindow(QMainWindow):
    """The application window.

    Args:
        bridge: The Qt ↔ asyncio bridge.
        config: The configuration, shown by the settings screen.
    """

    #: Emitted after every snapshot, for tests that want to watch the shell.
    refreshed = Signal(object)

    #: Emitted, *queued*, when engine work settles. The future's own callback
    #: runs on the engine thread, which has no Qt event loop — so the result is
    #: handed to the GUI thread through this signal instead of a QTimer that
    #: would never fire.
    work_finished = Signal(object, object)

    #: Emitted with the new configuration when the user applies settings. The
    #: application listens and repaints; the window does not paint itself,
    #: because the stylesheet belongs to the QApplication.
    settings_applied = Signal(object)

    def __init__(self, bridge: EngineBridge, config: Config) -> None:
        super().__init__()
        self._bridge = bridge
        self._config = config
        self._vm = SessionViewModel(self)
        self._detail_vm = TorrentViewModel(self)
        self._logs_vm = LogsViewModel(parent=self)
        self._dht_vm = DhtViewModel(self)
        self._page_keys: dict[str, int] = {}
        self._selected: str | None = None
        self._save_config: Callable[[Config], object] | None = None

        self.resize(1360, 860)
        self.setMinimumSize(1024, 640)
        self.setWindowTitle("PeerStream")
        self.setAcceptDrops(True)

        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- sidebar
        self._sidebar = Sidebar(DESTINATIONS, parent=central)
        self._sidebar.navigated.connect(self.show_page)
        outer.addWidget(self._sidebar)

        # ---- content
        content = QWidget(central)
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        outer.addWidget(content, stretch=1)

        self._topbar = TopBar(content)
        self._topbar.set_theme(config.ui.theme)
        self._topbar.add_requested.connect(self.add_torrent)
        self._topbar.mode_toggled.connect(self.toggle_theme_mode)
        column.addWidget(self._topbar)

        self._pages = QStackedWidget(content)
        column.addWidget(self._pages, stretch=1)

        self._overview = CommandCenterView(self._vm, self._pages)
        self._overview.connect_rows(self.select_torrent, self.toggle_torrent, self.remove_torrent)
        self._library = LibraryView(self._vm, self._pages)
        self._library.torrent_selected.connect(self.select_torrent)
        self._library.pause_toggled.connect(self.toggle_torrent)
        self._library.remove_requested.connect(self.remove_torrent)
        self._library.add_requested.connect(self.add_torrent)
        self._library.open_location.connect(self.open_file_location)
        self._settings = SettingsView(config, self._pages)
        self._settings.applied.connect(self._on_settings_applied)
        self._detail = TorrentDetailView(
            self._detail_vm, self._logs_vm, palette_for(config.ui.theme), self._pages
        )
        self._detail.connect_actions(self.toggle_torrent, self.remove_torrent)
        self._logs = LogsTab(self._logs_vm, self._pages)
        self._detail_timer = QTimer(self)
        self._detail_timer.setInterval(DETAIL_POLL_MS)
        self._detail_timer.timeout.connect(self._poll_detail)
        self._pending: set[str] = set()
        self._last_resolution: MagnetResolution | None = None
        self._poll_count = 0
        self._dht = DhtView(self._dht_vm, palette_for(config.ui.theme), self._pages)

        for key, page in (
            ("overview", self._overview),
            ("library", self._library),
            ("detail", self._detail),
            ("logs", self._logs),
            ("dht", self._dht),
            ("settings", self._settings),
        ):
            self._page_keys[key] = self._pages.addWidget(page)

        # ---- toasts, floating over the pages
        self._toasts = ToastHost(central, palette=palette_for(config.ui.theme))
        self._toasts.setGeometry(*self._page_rect())

        self._sidebar.set_current("overview")
        self._sidebar.set_footer(self._footer_text())
        bridge.pump.snapshot.connect(self._on_snapshot)
        bridge.feed.events_pending.connect(self._on_events_pending)
        self.work_finished.connect(self._on_work_finished, Qt.ConnectionType.QueuedConnection)

        self._vm.changed.connect(self._on_view_model_changed)
        self.refresh()

    # ------------------------------------------------------------------ pages

    @property
    def view_model(self) -> SessionViewModel:
        return self._vm

    @property
    def session(self) -> Session:
        """The session this window drives.

        A property, and used through ``self.session`` everywhere, so a test can
        stand a session in without standing in a bridge.
        """
        return self._bridge.session

    @property
    def current_page(self) -> str:
        """The key of the page being shown."""
        widget = self._pages.currentWidget()
        for key, index in self._page_keys.items():
            if self._pages.widget(index) is widget:
                return key
        return "overview"

    def show_page(self, key: str) -> None:
        """Switch to a page and refresh it immediately."""
        index = self._page_keys.get(key)
        if index is None:
            raise KeyError(f"no such page: {key!r}")
        self._pages.setCurrentIndex(index)
        self._sidebar.set_current(key)
        if key == "logs":
            # The session-wide timeline: the detail tab scopes it to one
            # torrent, and that scope must not follow you here.
            self._logs.set_torrent(None)
        self._set_detail_polling(key == "detail" and self._selected is not None)
        self.refresh()

    def page(self, key: str) -> QWidget:
        """The page registered under ``key``."""
        widget = self._pages.widget(self._page_keys[key])
        assert widget is not None
        return widget

    # --------------------------------------------------------------- refreshing

    def refresh(self) -> None:
        """Redraw the header, the badges, and the visible page."""
        summary = self._vm.summary
        self._topbar.set_rates(summary.download_rate, summary.upload_rate)
        self._topbar.set_summary(_summary_text(summary))
        self._sidebar.set_badges(
            {
                "library": summary.torrent_count,
                "overview": summary.active_count,
            }
        )
        key = self.current_page
        if key == "overview":
            self._overview.refresh()
        elif key == "library":
            self._library.refresh()
        elif key == "detail":
            self._refresh_detail()
        elif key == "dht":
            self._refresh_dht()
        self.refreshed.emit(key)

    def _on_snapshot(self, snapshot: AppSnapshot) -> None:
        """A new snapshot arrived from the pump."""
        redraw = self._vm.update(snapshot)
        if redraw or self._vm.revision <= 1:
            self.refresh()

    def _on_view_model_changed(self, _view_model: object) -> None:
        """The view model changed; the pages that care are refreshed above."""

    def _on_events_pending(self) -> None:
        """The feed has events waiting: take the whole batch in one go."""
        self._on_events(self._bridge.feed.drain())

    def _on_event(self, event: Event) -> None:
        """One event arrived. Kept for callers that hold exactly one."""
        self._on_events((event,))

    def _on_events(self, events: Sequence[Event]) -> None:
        """Record a batch of events, and toast at most one of them.

        Two rules, both learned by measurement:

        * **One refresh per batch.** Recording them one at a time refreshed the
          timeline once each, and a few hundred events a second turned the
          window into a log pane that had stopped painting.
        * **One toast per batch.** Ten failures in one burst are one problem,
          not ten, and ten toasts would hide the window they are meant to
          interrupt. The last notable event in the batch is the one shown.
        """
        if not events:
            return
        self._logs_vm.add_many(events)
        notable = next(
            (event for event in reversed(tuple(events)) if event.type in NOTABLE),
            None,
        )
        if notable is None:
            return
        kind = NOTABLE[notable.type]
        notifier = getattr(self._toasts, kind, None)
        if notifier is None:
            return
        notifier(
            notable.message or notable.type.value,
            title=notable.type.value.replace("_", " ").title(),
        )

    # ------------------------------------------------------------------ actions

    def add_torrent(self) -> None:
        """Open the add-torrent dialog and, if accepted, add the torrent."""
        self._open_add_dialog()

    def _open_add_dialog(self, source: str = "") -> AddTorrentDialog:
        """Build the dialog, pre-filled with ``source`` when given."""
        dialog = AddTorrentDialog(
            default_directory=self._config.storage.download_directory, parent=self
        )
        dialog.torrent_accepted.connect(self._on_torrent_chosen)
        dialog.magnet_accepted.connect(self._on_magnet_chosen)
        if source:
            dialog.set_source(source)
        dialog.exec()
        return dialog

    def _on_torrent_chosen(self, torrent: Torrent, directory: str) -> None:
        dialog = self.sender()
        # Use the directory the user selected in the dialog, falling back to
        # the configured default if the field was left blank.
        save_dir = directory or str(self._config.storage.download_directory)

        async def add() -> str:
            engine = await self.session.add_torrent(
                torrent,
                directory=save_dir,
                listen=self._config.network.accept_incoming_connections,
            )
            return engine.hex_info_hash

        self._run(add, on_done=lambda future: self._on_added(future, torrent.name, dialog))

    def _on_magnet_chosen(self, magnet: MagnetUri, directory: str) -> None:
        """Resolve a magnet link, then add the torrent it describes.

        Resolution talks to peers, which is engine work, so it runs on the
        engine loop like every other session call. It is also the one "add" that
        can fail for reasons outside our control, so the toast names the
        failure the user can act on: nobody answered.
        """
        save_dir = directory or str(self._config.storage.download_directory)
        name = magnet.display_name or "Unknown Magnet Torrent"

        async def add() -> str:
            engine, resolution = await self.session.add_magnet(
                magnet,
                directory=save_dir,
                listen=self._config.network.accept_incoming_connections,
            )
            self._last_resolution = resolution
            return engine.hex_info_hash

        self._toasts.info(f"Resolving magnet link: {name}...")
        self._run(add, on_done=lambda future: self._on_added(future, name, None))

    def _on_added(self, future: Future[Any], name: str, dialog: Any = None) -> None:
        error = future.exception()
        if error is not None:
            if dialog is not None:
                dialog.show_error(str(error))
            else:
                self._toasts.error(f"Could not add {name}: {error}")
            return
        self._toasts.success(f"Added {name}")
        self.refresh()
        if dialog is not None:
            dialog.accept()

    def select_torrent(self, hex_info_hash: str) -> None:
        """Remember which torrent the user picked and show its page.

        The detail view model is cleared first: a new selection must not show
        the previous torrent's swarm for the half second before the first read
        lands. Showing stale peers under a new name would be a lie by delay.
        """
        if hex_info_hash != self._selected:
            self._detail_vm.clear()
            self._detail.files.clear()
            self._detail.trackers.clear()
        self._selected = hex_info_hash
        self._detail.log.set_torrent(hex_info_hash)
        self.show_page("detail")
        self._poll_detail()

    def service_for(self, hex_info_hash: str) -> TorrentService | None:
        """The session's service for a torrent, if it still has one.

        A seam as much as a convenience: the window asks the session through
        this one method, so a test can stand in a service without standing in a
        whole session.
        """
        return self.session.get(hex_info_hash)

    def toggle_torrent(self, hex_info_hash: str) -> None:
        """Pause a running torrent, or resume a paused one."""
        service = self.service_for(hex_info_hash)
        if service is None:
            self._toasts.warning("That torrent is no longer in the session")
            return
        view = self._vm.torrent(hex_info_hash)
        wanted = "resume" if view is None or not view.active else "pause"

        async def toggle() -> None:
            assert service is not None
            if wanted == "pause":
                await service.pause()
            else:
                await service.resume()

        self._run(toggle, on_done=lambda future: self._on_action_done(future, wanted))

    def remove_torrent(self, hex_info_hash: str) -> None:
        """Ask what removal means, then remove.

        Removing a torrent and deleting its data are different actions, and the
        destructive one is never the default answer.
        """
        view = self._vm.torrent(hex_info_hash)
        name = view.name if view is not None else hex_info_hash
        answer = QMessageBox.question(
            self,
            "Remove torrent",
            f"Remove {name} from the client?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        delete_data = (
            QMessageBox.question(
                self,
                "Delete data?",
                f"Also delete the downloaded files for {name}? This cannot be undone.",
                QMessageBox.StandardButton.No | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

        async def remove() -> None:
            await self.session.remove(hex_info_hash, delete_data=delete_data)

        def done(future: Future[Any]) -> None:
            self._library.forget(hex_info_hash)
            if self._selected == hex_info_hash:
                # The page would otherwise keep the name of a torrent the
                # session has already dropped, and poll for a swarm that
                # belongs to nothing.
                self._selected = None
                self._detail_vm.clear()
                self._detail.files.clear()
                self._detail.trackers.clear()
                self._detail.log.set_torrent(None)
                self._set_detail_polling(False)
            self._on_action_done(future, "remove")

        self._run(remove, on_done=done)

    def open_file_location(self, hex_info_hash: str) -> None:
        """Open the torrent's download directory in the system file manager.

        The download directory is read from the session service so it reflects
        the directory the user actually chose, not the config default.
        """
        service = self.service_for(hex_info_hash)
        if service is None:
            self._toasts.warning("Cannot open location: torrent no longer in session")
            return
        directory = service.download_directory
        target = directory if directory.exists() else directory.parent
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(target)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            self._toasts.error(f"Could not open folder: {exc}")

    def _on_action_done(self, future: Future[Any], action: str) -> None:
        """Report the outcome of a pause/resume/remove, then redraw."""
        error = future.exception()
        if error is not None:
            self._toasts.error(f"Could not {action}: {error}")
        else:
            self._toasts.info(f"{action.capitalize()}d")
        self.refresh()

    # ----------------------------------------------------------------- settings

    def set_config_saver(self, saver: Callable[[Config], object] | None) -> None:
        """Install the callback that persists an applied configuration.

        The window does not own the application, so it cannot save one; the
        composition root (:func:`app.ui.app.build_ui`) wires this to the real
        application's save.
        """
        self._save_config = saver

    def _on_settings_applied(self, config: Config) -> None:
        """Remember the new configuration and tell the application to repaint."""
        self._config = config
        palette = palette_for(config.ui.theme)
        self._toasts.set_colours(palette)
        self._detail.set_palette(palette)
        self._detail.set_reduced_motion(config.ui.reduced_motion)
        self._sidebar.set_footer(self._footer_text())
        self._topbar.set_theme(config.ui.theme)
        if self._save_config is not None:
            self._save_config(config)
        self.settings_applied.emit(config)
        self._toasts.success("Settings applied")

    def toggle_theme_mode(self) -> None:
        """Cycle the theme through light, dark, and amoled."""
        current = self._config.ui.theme
        if current == "light":
            next_theme = "dark"
        elif current == "dark":
            next_theme = "amoled"
        else:
            next_theme = "light"
        
        try:
            updated = self._config.with_overrides(ui={"theme": next_theme})
        except (ValueError, TypeError) as error:
            self._toasts.error(f"Could not change theme: {error}")
            return
            
        self._settings.set_config(updated)
        self._on_settings_applied(updated)
        self._toasts.success(f"Mode toggled to {next_theme.capitalize()}")

    # ------------------------------------------------------------------ plumbing

    def _run(
        self,
        work: Callable[[], Coroutine[Any, Any, Any]],
        *,
        on_done: Callable[[Future[Any]], None] | None = None,
    ) -> Future[Any]:
        """Run a coroutine on the engine loop and refresh when it settles.

        ``work`` is a *factory*, not a coroutine: a coroutine object can only be
        awaited once, and building it on the GUI thread before it is needed
        would be one more way for a widget to touch the engine's world.

        The GUI thread never awaits, and the callback the future runs is on the
        engine's thread — so the outcome crosses back as a queued signal
        (:attr:`work_finished`) and is handled while Qt is painting.
        """
        future = self._bridge.submit(work())
        future.add_done_callback(lambda completed: self.work_finished.emit(completed, on_done))
        return future

    # ------------------------------------------------------------ detail page

    def _refresh_detail(self) -> None:
        """Push the session's view of the selected torrent into its view model."""
        selected = self._selected
        self._detail_vm.update_torrent(self._vm.torrent(selected) if selected else None)
        self._detail.refresh()

    def _set_detail_polling(self, enabled: bool) -> None:
        """Poll the detail page's slow views only while it is on screen.

        A timer that ran regardless of the visible page would spend the whole
        session reading swarms nobody is looking at.
        """
        if enabled and not self._detail_timer.isActive():
            self._detail_timer.start()
        elif not enabled and self._detail_timer.isActive():
            self._detail_timer.stop()

    def _refresh_dht(self) -> None:
        """Re-read the DHT node, if the client has one.

        The routing table changes on its own — nodes answer, go quiet, get
        replaced — so the panel is polled rather than waiting for an event that
        would have to be invented.
        """
        self._dht_vm.update(
            self.session.dht,
            enabled=self._config.dht.enabled,
            published=self.session.announcer.published,
        )
        self._dht.refresh()

    def _poll_detail(self) -> None:
        """Re-read the swarm and the pieces for the selected torrent.

        Each read is a coroutine submitted to the engine loop, because the
        state it reads is owned by that loop. Reads already in flight are not
        re-issued: a slow engine must not turn into a queue of duplicate work.
        """
        selected = self._selected
        if selected is None:
            self._set_detail_polling(False)
            return
        self._poll_count += 1
        self._read_peers(selected)
        self._read_pieces(selected)
        if self._poll_count % SLOW_POLL_EVERY == 0:
            self._read_files(selected)
            self._read_trackers(selected)

    def _read_peers(self, hex_info_hash: str) -> None:
        """Read the swarm, then hand it to the detail view model."""
        if "peers" in self._pending:
            return
        self._pending.add("peers")
        self._run(
            lambda: self.session.peers_view(hex_info_hash),
            on_done=lambda future: self._on_detail_read(future, "peers"),
        )

    def _read_pieces(self, hex_info_hash: str) -> None:
        """Read the piece map, then hand it to the detail view model."""
        if "pieces" in self._pending:
            return
        self._pending.add("pieces")
        self._run(
            lambda: self.session.piece_map(hex_info_hash),
            on_done=lambda future: self._on_detail_read(future, "pieces"),
        )

    def _read_files(self, hex_info_hash: str) -> None:
        """Read the file list, then hand it to the files tab."""
        if "files" in self._pending:
            return
        self._pending.add("files")
        self._run(
            lambda: self.session.files_view(hex_info_hash),
            on_done=lambda future: self._on_detail_read(future, "files"),
        )

    def _read_trackers(self, hex_info_hash: str) -> None:
        """Read tracker health, then hand it to the trackers tab."""
        if "trackers" in self._pending:
            return
        self._pending.add("trackers")
        self._run(
            lambda: self.session.trackers_view(hex_info_hash),
            on_done=lambda future: self._on_detail_read(future, "trackers"),
        )

    def _on_detail_read(self, future: Future[Any], what: str) -> None:
        """Fold one completed engine read into the detail page."""
        self._pending.discard(what)
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            logger.warning("could not read %s for the detail page: %s", what, error)
            return
        result = future.result()
        if what == "peers":
            self._detail_vm.update_peers(result)
        elif what == "pieces":
            if result is not None:
                self._detail_vm.update_pieces(result)
        elif what == "files":
            self._detail.set_files(result)
        else:
            self._detail.set_trackers(result)
        if self.current_page == "detail":
            self._detail.refresh()

    def _on_work_finished(self, future: object, on_done: object) -> None:
        """Handle a finished engine action on the GUI thread."""
        assert isinstance(future, Future)
        if callable(on_done):
            on_done(future)
        else:
            self.refresh()

    def _page_rect(self) -> tuple[int, int, int, int]:
        """Where the toast host sits: the bottom-right corner of the pages."""
        margin = TOKENS.spacing.xl
        width, height = 380, 300
        return (
            self.width() - width - margin,
            self.height() - height - margin,
            width,
            height,
        )

    def _footer_text(self) -> str:
        """Small print in the sidebar: where data goes, and what build this is."""
        return f"saving to {self._config.storage.download_directory}"

    # ------------------------------------------------------------------- events

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the toast host pinned to the corner."""
        super().resizeEvent(event)
        self._toasts.setGeometry(*self._page_rect())

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept a file dragged onto the window, if it looks like a torrent."""
        mime = event.mimeData()
        if mime.hasUrls() and any(url.toLocalFile().endswith(".torrent") for url in mime.urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        """Open the add dialog pre-filled with the first dropped torrent."""
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.endswith(".torrent"):
                self._open_add_dialog(path)
                return

    def closeEvent(self, event: QCloseEvent) -> None:
        """Closing the window retires the toasts; the bridge belongs to the app."""
        self._toasts.clear()
        super().closeEvent(event)


def _summary_text(summary: SessionSummary) -> str:
    """The header's context line: counts, and the slowest ETA if there is one."""
    if summary.torrent_count == 0:
        return "no torrents"
    parts = [
        f"{summary.active_count} of {summary.torrent_count} active",
        f"{summary.peers_connected} peers",
    ]
    if summary.seeding_count:
        parts.append(f"{summary.seeding_count} seeding")
    parts.append(f"{human_bytes(summary.downloaded_bytes)} down")
    if summary.eta_seconds is not None:
        parts.append(f"eta {human_duration(summary.eta_seconds)}")
    return "   ".join(parts)
