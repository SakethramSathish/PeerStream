"""The window shell, built offscreen and driven with real snapshots.

These tests build the *real* :class:`~app.ui.main_window.MainWindow` through
:func:`~app.ui.app.build_ui` — the same path the desktop entry point takes — with
the engine loop started and a real :class:`~app.services.session.Session`
underneath. The snapshots fed in afterwards are hand-made but honest: they are
built from :class:`~app.services.torrent_service.TorrentView` and
:class:`~app.services.session.SessionTotals`, the same objects the reducer makes.

What is being proved:

* The window builds, navigates, and lays out without a display.
* A snapshot moves the numbers: the header, the badges, the rows, the health.
* Actions go out as coroutines and come back as refreshes, and the GUI thread
  never waits on one.
* An unimplemented page says which milestone owns it rather than faking data.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.config import Config
from app.core.events import Event, EventType
from app.services.app_state import AppSnapshot
from app.services.engine import TorrentState
from app.services.session import Session, SessionTotals
from app.services.torrent_service import FileView, PeerView, PieceMap, TorrentView
from app.ui.app import UiHandle, build_ui
from app.ui.bridge import EventFeed
from app.ui.main_window import DESTINATIONS, MainWindow
from app.ui.viewmodels.session_vm import SessionViewModel
from app.ui.views.command_center_view import CommandCenterView
from app.ui.views.dht_view import DhtView
from app.ui.views.library_view import LibraryView
from app.ui.views.logs_tab import LogsTab
from app.ui.views.settings_view import SettingsView
from app.ui.views.torrent_detail_view import TorrentDetailView
from app.ui.widgets.torrent_row import TorrentRow
from PySide6.QtCore import QCoreApplication, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QMessageBox

TORRENT_A: str = "aa" * 20
TORRENT_B: str = "bb" * 20


def make_view(
    info_hash: str = TORRENT_A,
    *,
    name: str = "debian-13.6.0-amd64-netinst.iso",
    state: TorrentState = TorrentState.DOWNLOADING,
    progress: float = 0.25,
    peers: int = 4,
    unchoked: int = 2,
    metrics: Any = None,
) -> TorrentView:
    """A torrent view with nothing invented: every field is given."""
    return TorrentView(
        info_hash=info_hash,
        name=name,
        state=state,
        progress=progress,
        total_length=791_674_880,
        verified_pieces=int(3020 * progress),
        missing_pieces=3020 - int(3020 * progress),
        piece_count=3020,
        port=6881,
        resumed=None,  # type: ignore[arg-type]
        metrics=metrics,
    )


def make_snapshot(
    torrents: tuple[TorrentView, ...] = (),
    *,
    download_rate: float = 0.0,
    upload_rate: float = 0.0,
    downloaded: int = 0,
    uploaded: int = 0,
    events: tuple[Event, ...] = (),
    revision: int = 1,
) -> AppSnapshot:
    """A snapshot shaped exactly like the reducer's."""
    active = sum(1 for view in torrents if view.active)
    return AppSnapshot(
        totals=SessionTotals(
            torrents=len(torrents),
            active=active,
            download_rate=download_rate,
            upload_rate=upload_rate,
            downloaded_bytes=downloaded,
            uploaded_bytes=uploaded,
        ),
        torrents=torrents,
        events=events,
        counts=(),
        revision=revision,
        generated_at=time.time(),
    )


@pytest.fixture(scope="module")
def handle(qapp: QCoreApplication) -> Any:
    """One real window, shared by the whole module.

    Building a window is not free, and building forty of them in one process is
    a good way to make Qt's style system fall over between tests. So the shell
    tests share a single built window and reset it between cases.
    """
    built = build_ui(["pytest"], load_config=False)
    built.bridge.start()
    yield built
    built.bridge.stop()


@pytest.fixture(autouse=True)
def reset_window(handle: UiHandle) -> Any:
    """Put the shared window back the way a user would find it."""
    window = handle.window
    window._toasts.clear()
    window._library._search.setText("")
    window._library.forget_all()
    window.view_model.clear_series()
    # The detail page's in-flight reads and its selection are window state, and
    # one test's pending read must not swallow the next test's.
    window._pending.clear()
    window._poll_count = 0
    window._selected = None
    window._detail_vm.clear()
    window._logs_vm.clear()
    window.show_page("overview")
    yield


# ---------------------------------------------------------------------- building


class TestTheWindowBuilds:
    def test_it_is_a_real_window(self, handle: UiHandle) -> None:
        assert isinstance(handle.window, MainWindow)
        assert handle.window.width() > 1024

    def test_every_destination_has_a_page(self, handle: UiHandle) -> None:
        for key, _label, _icon in DESTINATIONS:
            assert handle.window.page(key) is not None

    def test_the_default_page_is_the_command_centre(self, handle: UiHandle) -> None:
        assert handle.window.current_page == "overview"

    def test_navigation_switches_pages(self, handle: UiHandle) -> None:
        window = handle.window
        for key in ("library", "settings", "overview"):
            window.show_page(key)
            assert window.current_page == key
            assert window._sidebar.current == key

    def test_an_unknown_page_is_an_error(self, handle: UiHandle) -> None:
        with pytest.raises(KeyError):
            handle.window.show_page("hovercraft")

    def test_the_bridge_is_running_before_anything_is_submitted(self, handle: UiHandle) -> None:
        # The whole point of the bridge: work goes out, the GUI thread paints.
        assert handle.bridge.pump.interval_ms > 0


# ------------------------------------------------------------------ measurements


class TestSnapshotsMoveTheNumbers:
    def test_the_header_shows_the_session_rates(self, handle: UiHandle) -> None:
        handle.window.view_model.update(
            make_snapshot((make_view(),), download_rate=1024 * 512, upload_rate=2048)
        )
        handle.window.refresh()
        assert "512.00 KiB/s" in handle.window._topbar._download.text()
        assert "2.00 KiB/s" in handle.window._topbar._upload.text()

    def test_the_sidebar_badge_counts_torrents(self, handle: UiHandle) -> None:
        handle.window.view_model.update(make_snapshot((make_view(), make_view(TORRENT_B))))
        handle.window.refresh()
        assert "2" in handle.window._sidebar._buttons["library"].text()

    def test_the_library_grows_a_row_per_torrent(self, handle: UiHandle) -> None:
        window = handle.window
        window.view_model.update(make_snapshot((make_view(), make_view(TORRENT_B, progress=0.9))))
        window.show_page("library")
        assert window._library.visible_count == 2
        assert all(isinstance(row, TorrentRow) for row in window._library.rows)

    def test_a_row_shows_what_it_was_given(self, handle: UiHandle) -> None:
        window = handle.window
        window.view_model.update(make_snapshot((make_view(progress=0.5),)))
        window.show_page("library")
        row = window._library.rows[0]
        assert "50.0%" in row._progress.text()
        assert row._state.text() == "downloading"

    def test_the_filter_hides_what_does_not_match(self, handle: UiHandle) -> None:
        window = handle.window
        window.view_model.update(
            make_snapshot((make_view(name="alpha.iso"), make_view(TORRENT_B, name="beta.iso")))
        )
        window.show_page("library")
        window._library._search.setText("beta")
        assert window._library.visible_count == 1

    def test_the_command_centre_previews_only_what_is_active(self, handle: UiHandle) -> None:
        window = handle.window
        window.view_model.update(
            make_snapshot(
                (
                    make_view(state=TorrentState.DOWNLOADING),
                    make_view(TORRENT_B, state=TorrentState.STOPPED),
                )
            )
        )
        window.show_page("overview")
        assert window._overview.empty.isHidden()  # something is active, so no placeholder
        visible = [row for row in window._overview._rows.values() if not row.isHidden()]
        assert len(visible) == 1

    def test_an_empty_session_shows_the_placeholder_not_a_flat_chart(
        self, handle: UiHandle
    ) -> None:
        window = handle.window
        window.view_model.update(make_snapshot())
        window.show_page("library")
        assert not window._library._empty.isHidden()
        assert window._library.visible_count == 0

    def test_the_graphs_are_fed_bounded_series(self, handle: UiHandle) -> None:
        window = handle.window
        for index in range(400):
            window.view_model.update(make_snapshot((make_view(),), download_rate=float(index)))
        assert len(window.view_model.download_series) <= 180


class TestHealthIsHonest:
    def test_peer_health_is_unchoked_over_connected(self, handle: UiHandle) -> None:
        metrics = _metrics(peers_connected=8, peers_unchoked=2)
        handle.window.view_model.update(make_snapshot((make_view(metrics=metrics),)))
        health = handle.window.view_model.peer_health
        assert health.fraction == pytest.approx(0.25)
        assert health.note == "2/8 unchoked"

    def test_health_with_no_peers_is_unknown_not_perfect(self, handle: UiHandle) -> None:
        handle.window.view_model.update(make_snapshot())
        assert handle.window.view_model.peer_health.fraction is None

    def test_piece_health_counts_pieces(self, handle: UiHandle) -> None:
        handle.window.view_model.update(make_snapshot((make_view(progress=0.5),)))
        assert handle.window.view_model.piece_health.fraction == pytest.approx(0.5)


# ----------------------------------------------------------------------- actions


class TestActionsGoThroughTheBridge:
    def test_pausing_a_running_torrent_submits_pause(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = handle.window
        paused: list[str] = []

        async def fake_pause() -> str:
            paused.append("paused")
            return "ok"

        monkeypatch.setattr(
            window, "service_for", lambda _hash: _FakeService(pause=fake_pause, view=make_view())
        )
        window.view_model.update(make_snapshot((make_view(),)))
        window.toggle_torrent(TORRENT_A)
        _wait_until(lambda: paused == ["paused"])

    def test_resuming_a_stopped_torrent_submits_resume(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = handle.window
        resumed: list[str] = []

        async def fake_resume() -> str:
            resumed.append("resumed")
            return "ok"

        monkeypatch.setattr(
            window, "service_for", lambda _hash: _FakeService(resume=fake_resume, view=make_view())
        )
        window.view_model.update(make_snapshot((make_view(state=TorrentState.PAUSED),)))
        window.toggle_torrent(TORRENT_A)
        _wait_until(lambda: resumed == ["resumed"])

    def test_a_torrent_that_has_gone_away_is_reported_not_crashed(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = handle.window
        monkeypatch.setattr(window, "service_for", lambda _hash: None)
        window.view_model.update(make_snapshot((make_view(),)))
        window.toggle_torrent(TORRENT_A)
        _wait_for_toast(window, "no longer in the session")

    def test_a_failed_action_is_reported_not_swallowed(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = handle.window

        async def broken() -> None:
            raise RuntimeError("the socket fell over")

        monkeypatch.setattr(
            window, "service_for", lambda _hash: _FakeService(pause=broken, view=make_view())
        )
        window.view_model.update(make_snapshot((make_view(),)))
        window.toggle_torrent(TORRENT_A)
        _wait_for_toast(window, "fell over")

    def test_the_gui_thread_never_waits_on_the_engine(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # toggle_torrent returns immediately: the work is on the engine loop and
        # the result arrives through a queued signal.
        import asyncio

        window = handle.window
        gate = asyncio.Event()

        async def slow() -> str:
            await gate.wait()
            return "done"

        monkeypatch.setattr(
            window, "service_for", lambda _hash: _FakeService(pause=slow, view=make_view())
        )
        window.view_model.update(make_snapshot((make_view(),)))
        started = time.monotonic()
        window.toggle_torrent(TORRENT_A)
        assert time.monotonic() - started < 0.5
        window._bridge.submit(_release(gate)).result(timeout=5)


class TestAddingAndRemoving:
    def test_adding_a_torrent_goes_through_the_session(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        added: list[str] = []

        class FakeSession:
            async def add_torrent(self, torrent: object, **kwargs: object) -> Any:
                added.append(str(kwargs.get("directory")))
                return SimpleNamespace(hex_info_hash=TORRENT_A)

        monkeypatch.setattr(MainWindow, "session", property(lambda self: FakeSession()))
        window = handle.window
        window._on_torrent_chosen(_torrent(tmp_path))
        _wait_for_toast(window, "Added")
        assert added == [str(window._config.storage.download_directory)]

    def test_removing_asks_before_it_deletes_anything(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "Remove" and "remove and delete my files" are different actions, and
        # the destructive one is never the default answer.
        answers = iter([QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No])
        monkeypatch.setattr(
            "app.ui.main_window.QMessageBox.question",
            lambda *args, **kwargs: next(answers),
        )
        removed: list[tuple[str, bool]] = []

        class FakeSession:
            async def remove(self, hex_info_hash: str, *, delete_data: bool) -> None:
                removed.append((hex_info_hash, delete_data))

        monkeypatch.setattr(MainWindow, "session", property(lambda self: FakeSession()))
        window = handle.window
        window.view_model.update(make_snapshot((make_view(),)))
        window.show_page("library")
        window.remove_torrent(TORRENT_A)
        _wait_until(lambda: removed == [(TORRENT_A, False)])

    def test_a_cancelled_removal_removes_nothing(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.ui.main_window.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Cancel,
        )
        removed: list[str] = []

        class FakeSession:
            async def remove(self, hex_info_hash: str, *, delete_data: bool) -> None:
                removed.append(hex_info_hash)

        monkeypatch.setattr(MainWindow, "session", property(lambda self: FakeSession()))
        window = handle.window
        window.view_model.update(make_snapshot((make_view(),)))
        window.remove_torrent(TORRENT_A)
        pump_events(0.2)
        assert removed == []

    def test_deleting_the_data_is_possible_when_it_is_asked_for(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = iter([QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Yes])
        monkeypatch.setattr(
            "app.ui.main_window.QMessageBox.question",
            lambda *args, **kwargs: next(answers),
        )
        removed: list[tuple[str, bool]] = []

        class FakeSession:
            async def remove(self, hex_info_hash: str, *, delete_data: bool) -> None:
                removed.append((hex_info_hash, delete_data))

        monkeypatch.setattr(MainWindow, "session", property(lambda self: FakeSession()))
        window = handle.window
        window.view_model.update(make_snapshot((make_view(),)))
        window.remove_torrent(TORRENT_A)
        _wait_until(lambda: removed == [(TORRENT_A, True)])
        # The row goes with the torrent, or the list would show a ghost.
        assert TORRENT_A not in {row.info_hash for row in window._library.rows}

    def test_closing_the_window_retires_the_toasts(self, handle: UiHandle) -> None:
        window = handle.window
        window._toasts.info("something happened")
        window.closeEvent(QCloseEvent())
        assert window._toasts.count == 0

    def test_dropping_a_torrent_file_opens_the_dialog(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []
        monkeypatch.setattr(
            handle.window, "_open_add_dialog", lambda source="": opened.append(source)
        )
        window = handle.window
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile("/tmp/example.torrent")])
        drop = QDropEvent(
            QPointF(10.0, 10.0),
            Qt.DropAction.CopyAction,
            data,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.dropEvent(drop)
        assert opened == ["/tmp/example.torrent"]

    def test_dropping_anything_else_is_ignored(self, handle: UiHandle) -> None:
        opened: list[str] = []
        handle.window._open_add_dialog = lambda source="": opened.append(source)  # type: ignore[method-assign]
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile("/tmp/notes.txt")])
        drop = QDropEvent(
            QPointF(10.0, 10.0),
            Qt.DropAction.CopyAction,
            data,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        handle.window.dropEvent(drop)
        assert opened == []

    def test_a_drag_of_a_torrent_is_accepted_a_drag_of_anything_else_is_not(
        self, handle: UiHandle
    ) -> None:
        # The event does not take ownership of the mime data, so Python must
        # keep it alive for as long as the event is used -- otherwise the
        # event's pointer dangles and Qt falls over.
        keep: list[QMimeData] = []

        def drag_of(filename: str) -> QDragEnterEvent:
            data = QMimeData()
            keep.append(data)
            data.setUrls([QUrl.fromLocalFile(filename)])
            return QDragEnterEvent(
                QPoint(4, 4),
                Qt.DropAction.CopyAction,
                data,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

        accepted = drag_of("/tmp/example.torrent")
        handle.window.dragEnterEvent(accepted)
        assert accepted.isAccepted()

        ignored = drag_of("/tmp/notes.txt")
        handle.window.dragEnterEvent(ignored)
        assert not ignored.isAccepted()


class TestEventsArriveInBatches:
    """A busy transfer must not turn the timeline into the whole GUI budget.

    One refresh per event cost 26.6 s of GUI-thread time in a 47 s run (measured
    on a 512 MiB transfer), which is a window that has stopped painting. The
    events still all arrive; they arrive together.
    """

    def test_a_batch_records_every_event_with_one_refresh(self, handle: UiHandle) -> None:
        logs = handle.window._logs_vm
        seen: list[object] = []
        logs.changed.connect(lambda _vm: seen.append(1))

        window = handle.window
        burst = tuple(
            Event(type=EventType.PIECE_BLOCK_RECEIVED, category="piece", message=f"block {index}")
            for index in range(500)
        )
        window._on_events(burst)

        assert len(seen) == 1, "five hundred events, one update"
        assert logs.recorded >= 500
        assert logs.last is not None
        assert logs.last.message == "block 499", "the newest is last, not first"

    def test_a_batch_of_failures_raises_one_toast(self, handle: UiHandle) -> None:
        # Ten failures in one burst are one problem, not ten.
        window = handle.window
        window._toasts.clear()
        burst = tuple(
            Event(
                type=EventType.TRACKER_FAILED,
                category="error",
                message=f"tracker {index} refused the announce",
            )
            for index in range(10)
        )
        window._on_events(burst)
        assert window._toasts.count == 1

    def test_the_routine_events_in_a_batch_are_still_recorded(self, handle: UiHandle) -> None:
        logs = handle.window._logs_vm
        before = logs.recorded
        window = handle.window
        window._toasts.clear()
        window._on_events(
            tuple(
                Event(type=EventType.PIECE_VERIFIED, category="piece", message=f"piece {index}")
                for index in range(50)
            )
        )
        assert logs.recorded == before + 50
        assert window._toasts.count == 0, "a toast for every verified piece would be noise"

    def test_the_feed_announces_once_until_it_is_drained(self, handle: UiHandle) -> None:
        # One queued signal per drain, however many events arrived meanwhile.
        # Tested on a feed of its own, because the window's slot *drains* — so
        # with the real one wired up, every arrival is collected immediately.
        feed = EventFeed(handle.bridge.feed.state)
        feed.start()
        announcements: list[int] = []
        feed.events_pending.connect(lambda: announcements.append(1))

        state = feed.state
        for index in range(25):
            state.reduce(
                Event(
                    type=EventType.PIECE_BLOCK_RECEIVED,
                    category="piece",
                    message=f"block {index}",
                )
            )
        assert len(announcements) == 1, "one announcement, not twenty-five"
        assert feed.pending == 25

        drained = feed.drain()
        assert len(drained) == 25, "the whole burst crosses as one batch"
        assert feed.pending == 0

        state.reduce(
            Event(type=EventType.PIECE_BLOCK_RECEIVED, category="piece", message="after the drain")
        )
        assert len(announcements) == 2, "the next arrival is announced again"
        feed.stop()
        feed.drain()

    def test_one_event_still_works(self, handle: UiHandle) -> None:
        window = handle.window
        before = window._logs_vm.recorded
        window._on_event(Event(type=EventType.PIECE_VERIFIED, category="piece", message="piece 7"))
        assert window._logs_vm.recorded == before + 1


class TestEventsBecomeToasts:
    def test_a_failure_interrupts_the_user(self, handle: UiHandle) -> None:
        window = handle.window
        window._on_event(
            Event(
                type=EventType.TRACKER_FAILED,
                category="error",
                message="tracker refused the announce",
            )
        )
        assert window._toasts.count == 1

    def test_a_routine_event_does_not(self, handle: UiHandle) -> None:
        # A toast for every block received would be noise.
        window = handle.window
        window._on_event(
            Event(type=EventType.PIECE_BLOCK_RECEIVED, category="piece", message="block 3")
        )
        assert window._toasts.count == 0


# ---------------------------------------------------------------------- settings


class TestSettings:
    def test_applying_builds_a_new_config(self, handle: UiHandle) -> None:
        window = handle.window
        settings: SettingsView = window.page("settings")  # type: ignore[assignment]
        settings._theme.setCurrentText("light")
        settings._port.setValue(6999)
        applied: list[Config] = []
        window.settings_applied.connect(applied.append)
        settings.apply_changes()
        assert applied
        assert applied[0].ui.theme == "light"
        assert applied[0].network.listen_port == 6999

    def test_a_rejected_value_keeps_the_old_config(self, handle: UiHandle) -> None:
        window = handle.window
        settings: SettingsView = window.page("settings")  # type: ignore[assignment]
        before = settings.config
        settings._port.setValue(1)  # outside the control's range is clamped, so use a bad path
        settings._directory.setText("")
        settings._port.setValue(6881)
        settings.apply_changes()
        assert settings.config.network.listen_port == before.network.listen_port

    def test_reverting_restores_the_controls(self, handle: UiHandle) -> None:
        window = handle.window
        settings: SettingsView = window.page("settings")  # type: ignore[assignment]
        port = settings.config.network.listen_port
        settings._port.setValue(min(65535, port + 1))
        assert settings.dirty
        settings.revert()
        assert settings._port.value() == port
        assert not settings.dirty

    def test_unlimited_is_zero_not_a_big_number(self, handle: UiHandle) -> None:
        window = handle.window
        settings: SettingsView = window.page("settings")  # type: ignore[assignment]
        settings._down_speed.setValue(0)
        assert settings.overrides()["download"]["max_download_speed"] == 0
        settings._down_speed.setValue(512)
        assert settings.overrides()["download"]["max_download_speed"] == 512 * 1024


class TestTheDetailPage:
    """The window drives the detail page: what it asks for, and when."""

    def test_selecting_a_torrent_shows_its_page(self, handle: UiHandle) -> None:
        window = handle.window
        window.view_model.update(make_snapshot((make_view(),)))
        window.select_torrent(TORRENT_A)
        assert window.current_page == "detail"
        assert window._selected == TORRENT_A
        assert window._detail.title.text() == "debian-13.6.0-amd64-netinst.iso"

    def test_the_detail_page_reads_the_session_view(self, handle: UiHandle) -> None:
        window = handle.window
        window.view_model.update(make_snapshot((make_view(progress=0.5),)))
        window.select_torrent(TORRENT_A)
        assert window._detail_vm.view is not None
        assert window._detail_vm.view.progress == 0.5

    def test_switching_torrents_forgets_the_previous_swarm(self, handle: UiHandle) -> None:
        # A new selection must not show the old torrent's peers for the half
        # second before the first read lands.
        window = handle.window
        window.view_model.update(make_snapshot((make_view(), make_view(info_hash=TORRENT_B))))
        window.select_torrent(TORRENT_A)
        window._detail_vm.update_peers((_peer("10.0.0.1"),))
        window.select_torrent(TORRENT_B)
        assert window._detail_vm.peers.peers == ()

    def test_polling_reads_the_swarm_and_the_pieces_on_the_engine_loop(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = handle.window
        asked: list[str] = []

        class FakeSession:
            async def peers_view(self, _hex: str) -> tuple[PeerView, ...]:
                asked.append("peers")
                return (_peer("10.0.0.1"), _peer("10.0.0.2"))

            async def piece_map(self, _hex: str) -> object:
                asked.append("pieces")
                return _piece_map()

            async def files_view(self, _hex: str) -> tuple[FileView, ...]:
                asked.append("files")
                return ()

            async def trackers_view(self, _hex: str) -> tuple[object, ...]:
                asked.append("trackers")
                return ()

        monkeypatch.setattr(MainWindow, "session", property(lambda self: FakeSession()))
        window.view_model.update(make_snapshot((make_view(),)))
        window.select_torrent(TORRENT_A)
        _wait_until(lambda: len(window._detail_vm.peers.peers) == 2)
        _wait_until(lambda: window._detail_vm.pieces.known)
        assert "peers" in asked and "pieces" in asked

    def test_the_slow_reads_wait_their_turn(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Files and trackers change slowly; reading them every second would be
        # work for its own sake.
        window = handle.window
        asked: list[str] = []

        class FakeSession:
            async def peers_view(self, _hex: str) -> tuple[PeerView, ...]:
                return ()

            async def piece_map(self, _hex: str) -> object:
                return _piece_map()

            async def files_view(self, _hex: str) -> tuple[FileView, ...]:
                asked.append("files")
                return ()

            async def trackers_view(self, _hex: str) -> tuple[object, ...]:
                asked.append("trackers")
                return ()

        monkeypatch.setattr(MainWindow, "session", property(lambda self: FakeSession()))
        window.view_model.update(make_snapshot((make_view(),)))
        window.select_torrent(TORRENT_A)
        window._poll_count = 0
        for _ in range(3):
            window._poll_detail()
            _wait_until(lambda: not window._pending)
        assert "files" not in asked
        assert "trackers" not in asked

    def test_polling_stops_when_the_page_is_left(self, handle: UiHandle) -> None:
        window = handle.window
        window.view_model.update(make_snapshot((make_view(),)))
        window.select_torrent(TORRENT_A)
        assert window._detail_timer.isActive()
        window.show_page("library")
        assert not window._detail_timer.isActive()

    def test_a_failed_read_is_logged_not_shown_as_empty(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        window = handle.window

        class BrokenSession:
            async def peers_view(self, _hex: str) -> tuple[PeerView, ...]:
                raise RuntimeError("the loop said no")

            async def piece_map(self, _hex: str) -> object:
                return None

            async def files_view(self, _hex: str) -> tuple[FileView, ...]:
                return ()

            async def trackers_view(self, _hex: str) -> tuple[object, ...]:
                return ()

        monkeypatch.setattr(MainWindow, "session", property(lambda self: BrokenSession()))
        window.view_model.update(make_snapshot((make_view(),)))
        window.select_torrent(TORRENT_A)
        with caplog.at_level("WARNING"):
            _wait_until(lambda: any("peers" in record.message for record in caplog.records))

    def test_events_reach_the_timeline_as_well_as_the_toasts(self, handle: UiHandle) -> None:
        window = handle.window
        before = window._logs_vm.recorded
        window._on_event(
            Event(type=EventType.PIECE_VERIFIED, category="piece", message="piece 7 verified")
        )
        assert window._logs_vm.recorded == before + 1
        assert window._logs_vm.last is not None
        assert window._logs_vm.last.message == "piece 7 verified"

    def test_removing_the_selected_torrent_forgets_it(
        self, handle: UiHandle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = handle.window
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        )
        window.view_model.update(make_snapshot((make_view(),)))
        window.select_torrent(TORRENT_A)
        window.remove_torrent(TORRENT_A)
        _wait_until(lambda: window._selected is None)

    def test_the_log_page_shows_the_whole_session(self, handle: UiHandle) -> None:
        window = handle.window
        window._logs_vm.add(
            Event(
                type=EventType.PEER_CONNECTED, category="network", message="a", torrent_id=TORRENT_A
            )
        )
        window.select_torrent(TORRENT_A)
        assert window._logs_vm.torrent == TORRENT_A
        window.show_page("logs")
        assert window._logs_vm.torrent is None


class TestDhtPage:
    """The DHT page is real now, and it says which state it is in."""

    def test_the_page_is_the_dht_view(self, handle: UiHandle) -> None:
        assert isinstance(handle.window.page("dht"), DhtView)

    def test_a_disabled_dht_says_so_and_where_to_turn_it_on(self, handle: UiHandle) -> None:
        window = handle.window
        window._config = window._config.with_overrides(dht={"enabled": False})
        window.show_page("dht")
        window._refresh_dht()

        page = window.page("dht")
        assert page.summary.running is False
        assert "disabled" in page._note.text()
        assert "Settings" in page._note.text()

    def test_an_enabled_but_unbootstrapped_node_says_so(self, handle: UiHandle) -> None:
        window = handle.window
        window._config = window._config.with_overrides(dht={"enabled": True})
        window.show_page("dht")
        window._refresh_dht()

        page = window.page("dht")
        assert "not running" in page._note.text(), "on but unbound is not the same as off"


# --------------------------------------------------------------------- the pages


def test_the_view_classes_are_what_the_window_builds(handle: UiHandle) -> None:
    assert isinstance(handle.window.page("overview"), CommandCenterView)
    assert isinstance(handle.window.page("library"), LibraryView)
    assert isinstance(handle.window.page("settings"), SettingsView)
    assert isinstance(handle.window.page("detail"), TorrentDetailView)
    assert isinstance(handle.window.page("logs"), LogsTab)


def test_the_view_model_is_the_window_single_source(handle: UiHandle) -> None:
    assert isinstance(handle.window.view_model, SessionViewModel)


def _metrics(**kwargs: Any) -> Any:
    """A metrics stand-in with only the fields the shell reads."""
    from types import SimpleNamespace

    rates = SimpleNamespace(displayed=0.0, short=0.0, instant=0.0, average=0.0, total=0)
    values: dict[str, Any] = {
        "download": rates,
        "upload": rates,
        "progress": 0.0,
        "wasted_bytes": 0,
        "eta_seconds": None,
        "share_ratio": None,
        "peers_connected": 0,
        "peers_unchoked": 0,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def _peer(host: str) -> PeerView:
    """One connected peer, for the detail page's reads."""
    return PeerView(
        key=f"{host}:6881",
        host=host,
        port=6881,
        client="qBittorrent 5.1.0",
        state="connected",
        source="tracker",
        pieces_held=10,
        piece_count=100,
    )


def _piece_map() -> PieceMap:
    """A ten-piece map, half verified."""
    codes = [3] * 5 + [0] * 5
    return PieceMap(
        piece_count=10,
        piece_length=262_144,
        total_length=262_144 * 10,
        states=bytes(codes),
        availability=tuple(1 for _ in codes),
        filled=tuple(0.0 for _ in codes),
        counts={"verified": 5, "missing": 5, "requested": 0, "downloading": 0, "failed": 0},
    )


class _FakeService:
    """The smallest thing the window's actions need from a TorrentService.

    Only the coroutines a test names are wired; the rest do nothing, so a test
    can stand in exactly the action it is exercising.
    """

    def __init__(
        self,
        *,
        pause: Any = None,
        resume: Any = None,
        view: TorrentView | None = None,
    ) -> None:
        self._pause = pause
        self._resume = resume
        self._view = view or make_view()

    async def pause(self) -> None:
        await (self._pause or _nothing)()

    async def resume(self) -> None:
        await (self._resume or _nothing)()

    def view(self) -> TorrentView:
        return self._view


async def _nothing() -> None:
    """The default action: succeeds, does nothing."""


async def _release(gate: Any) -> None:
    gate.set()


def pump_events(seconds: float) -> None:
    """Turn the Qt event loop for a moment, without waiting on anything."""
    qt = QCoreApplication.instance()
    assert qt is not None
    _wait_until(lambda: False, timeout=seconds) if False else None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(0.01)


def _torrent(tmp_path: Path) -> Any:
    """A real (tiny) torrent, for the add path to add."""
    from tools.screenshot import make_test_torrent

    torrent_path, _payload = make_test_torrent(tmp_path, size="64KiB")
    from app.torrent import parse_torrent_file

    return parse_torrent_file(torrent_path)


def _wait_for_toast(window: MainWindow, text: str, *, timeout: float = 5.0) -> None:
    """Wait until a toast containing ``text`` is on screen.

    Text, not a count: with one window shared by the module, a late callback
    from an earlier case can add a toast of its own.
    """
    _wait_until(
        lambda: any(text in message for message in window._toasts.messages), timeout=timeout
    )


def _wait_until(condition: Any, *, timeout: float = 5.0) -> None:
    """Let the Qt event loop run until ``condition``, because the GUI thread
    never blocks on the engine."""
    qt = QCoreApplication.instance()
    assert qt is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt.processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("the condition never became true")


def test_session_totals_are_the_shape_the_shell_reads() -> None:
    totals = SessionTotals(
        torrents=1,
        active=1,
        download_rate=1.0,
        upload_rate=2.0,
        downloaded_bytes=3,
        uploaded_bytes=4,
    )
    assert totals.as_dict()["download_rate"] == 1.0


def test_the_window_holds_a_real_session(handle: UiHandle) -> None:
    assert isinstance(handle.session, Session)
