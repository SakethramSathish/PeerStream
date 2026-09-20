"""Session and AppState: bookkeeping, lifetimes, and honest numbers.

These tests build real engines (real storage, real subsystems) but never
transfer a byte: the tracker list is empty and no peers are added. That keeps
them fast while still exercising the code the UI will lean on — add, get,
remove, pause, resume, totals, and the reducer that turns events into a
timeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from app.core.config import Config
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.services import AppState, Session, TorrentState
from app.torrent import Torrent, parse_torrent
from tools.make_test_torrent import build_torrent_bytes


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path / "downloads"


@pytest.fixture
def torrent(payload: bytes) -> Torrent:
    """The shared payload as a torrent with **no trackers**.

    These tests are about bookkeeping, not networking, and a tracker in the
    metainfo would have the engine dialling loopback and logging failures.
    """
    return parse_torrent(build_torrent_bytes(payload, name="one.bin", announce=None))


@pytest.fixture
def second_torrent(payload: bytes) -> Torrent:
    """A second torrent: same payload, different name, therefore a different hash."""
    return parse_torrent(build_torrent_bytes(payload, name="two.bin", announce=None))


async def quiet_session(directory: Path, **kwargs: object) -> Session:
    """A session whose torrents have nowhere to connect to."""
    return Session(Config(), download_directory=directory, **kwargs)  # type: ignore[arg-type]


class TestSessionDirectory:
    def test_the_download_directory_is_taken_from_the_argument(self, tmp_path: Path) -> None:
        session = Session(download_directory=tmp_path / "in")
        assert session.config.storage.download_directory == (tmp_path / "in").resolve()

    def test_the_config_is_left_alone_when_no_directory_is_given(self) -> None:
        config = Config()
        session = Session(config)
        assert session.config.storage.download_directory == config.storage.download_directory

    def test_a_relative_directory_is_resolved(self) -> None:
        session = Session(download_directory="downloads")
        assert session.config.storage.download_directory.is_absolute()


class TestSessionTorrents:
    async def test_adding_a_torrent_builds_and_starts_an_engine(
        self, torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            engine = await session.add_torrent(torrent)

            assert len(session) == 1
            assert session.get(torrent.hex_info_hash) is not None
            assert engine.state in {TorrentState.STARTING, TorrentState.DOWNLOADING}

    async def test_adding_it_twice_is_refused(self, torrent: Torrent, directory: Path) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent)
            with pytest.raises(ValueError, match="already in this session"):
                await session.add_torrent(torrent)
            assert len(session) == 1

    async def test_a_hash_in_capitals_still_finds_it(
        self, torrent: Torrent, directory: Path
    ) -> None:
        """Users paste hashes from magnet links, which are upper-case hex."""
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent)
            assert session.get(torrent.hex_info_hash.upper()) is not None
            assert session.get("deadbeef") is None

    async def test_removing_stops_it_and_keeps_the_data(
        self, torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent)
            removed = await session.remove(torrent.hex_info_hash)

            assert removed is not None
            assert len(session) == 0
            assert session.get(torrent.hex_info_hash) is None

    async def test_removing_something_unknown_is_not_an_error(self, directory: Path) -> None:
        async with await quiet_session(directory) as session:
            assert await session.remove("nope") is None

    async def test_removing_with_delete_data_takes_the_files_too(
        self, torrent: Torrent, payload: bytes, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            engine = await session.add_torrent(torrent)
            await engine.storage.write_piece(0, payload[: torrent.piece_length])
            created = list(engine.storage.files)
            assert (directory / created[0].relative_path).exists()

            await session.remove(torrent.hex_info_hash, delete_data=True)

        assert not (directory / created[0].relative_path).exists()

    async def test_removal_is_announced_on_the_bus(self, torrent: Torrent, directory: Path) -> None:
        bus = EventBus()
        seen: list[EventType] = []
        bus.subscribe_all(lambda event: seen.append(event.type))

        async with Session(Config(), download_directory=directory, event_bus=bus) as session:
            await session.add_torrent(torrent)
            await session.remove(torrent.hex_info_hash)

        assert EventType.TORRENT_ADDED in seen
        assert EventType.TORRENT_REMOVED in seen
        assert EventType.SYSTEM_STARTED in seen


class TestSessionControl:
    async def test_start_all_and_stop_all(
        self, torrent: Torrent, second_torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent, start=False)
            await session.add_torrent(second_torrent, start=False)

            assert await session.start_all() == 2
            assert session.totals().active == 2

            await session.stop_all()
            totals = session.totals()
            assert totals.torrents == 2
            assert totals.active == 0

    async def test_a_torrent_can_pause_and_resume(self, torrent: Torrent, directory: Path) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent)
            service = session.get(torrent.hex_info_hash)
            assert service is not None

            await service.pause()
            assert service.view().state == TorrentState.PAUSED

            await service.resume()
            assert service.view().state in {
                TorrentState.STARTING,
                TorrentState.DOWNLOADING,
            }

    async def test_the_view_reports_measured_numbers(
        self, torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent)
            service = session.get(torrent.hex_info_hash)
            assert service is not None

            view = service.view()
            assert view.piece_count == torrent.piece_count
            assert view.total_length == torrent.total_length
            assert view.progress == 0.0
            assert view.resumed.pieces == 0
            assert view.metrics is not None

            as_dict = view.as_dict()
            assert as_dict["state"] in {"starting", "downloading"}
            assert as_dict["verified_pieces"] == 0

    async def test_closing_a_session_stops_its_torrents(
        self, torrent: Torrent, directory: Path
    ) -> None:
        session = await quiet_session(directory)
        engine = await session.add_torrent(torrent)
        await session.aclose()

        assert engine.state == TorrentState.STOPPED
        assert len(session) == 0
        # Closing twice is how a shutdown handler behaves under pressure.
        await session.aclose()


class TestAppState:
    def test_events_are_reduced_in_order(self) -> None:
        state = AppState(event_capacity=10)
        for index in range(3):
            state.reduce(
                make_event(
                    EventType.PIECE_VERIFIED, message=f"piece {index}", data={"index": index}
                )
            )

        assert [event.message for event in state.events] == ["piece 0", "piece 1", "piece 2"]
        assert state.count(EventType.PIECE_VERIFIED) == 3
        assert state.revision == 3

    def test_the_ring_keeps_only_the_most_recent_events(self) -> None:
        state = AppState(event_capacity=2)
        for index in range(5):
            state.reduce(make_event(EventType.PIECE_DOWNLOADED, message=str(index)))

        assert [event.message for event in state.events] == ["3", "4"]

    def test_counts_are_grouped_and_sorted(self) -> None:
        state = AppState()
        state.reduce(make_event(EventType.PIECE_DOWNLOADED, message="a"))
        state.reduce(make_event(EventType.PIECE_DOWNLOADED, message="b"))
        state.reduce(make_event(EventType.PEER_CONNECTED, message="c"))

        assert state.counts() == {"piece_downloaded": 2, "peer_connected": 1}

    def test_a_listener_hears_every_reduction(self) -> None:
        state = AppState()
        heard: list[int] = []
        remove = state.on_change(heard.append)

        state.reduce(make_event(EventType.LOG, message="one"))
        assert heard == [1]

        remove()
        state.reduce(make_event(EventType.LOG, message="two"))
        assert heard == [1], "an unsubscribed listener must go quiet"

    def test_a_raising_listener_cannot_silence_the_others(self) -> None:
        state = AppState()
        heard: list[int] = []

        def explode(revision: int) -> None:
            raise RuntimeError("widget went away")

        state.on_change(explode)
        state.on_change(heard.append)
        state.reduce(make_event(EventType.LOG, message="boom"))

        assert heard == [1]

    def test_a_non_callable_listener_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="callable"):
            AppState().on_change("not a function")  # type: ignore[arg-type]

    def test_a_snapshot_without_a_session_is_all_zeroes(self) -> None:
        snapshot = AppState().snapshot()
        assert snapshot.totals.torrents == 0
        assert snapshot.torrents == ()
        assert snapshot.generated_at > 0

    async def test_attaching_reduces_the_session_bus(
        self, torrent: Torrent, directory: Path
    ) -> None:
        state = AppState()
        async with await quiet_session(directory) as session:
            state.attach(session)
            await session.add_torrent(torrent)
            snapshot = state.snapshot()

        assert state.count(EventType.TORRENT_ADDED) == 1
        assert snapshot.totals.torrents == 1
        assert [view.info_hash for view in snapshot.torrents] == [torrent.hex_info_hash]

    async def test_detaching_stops_reducing(
        self, torrent: Torrent, second_torrent: Torrent, directory: Path
    ) -> None:
        state = AppState()
        async with await quiet_session(directory) as session:
            state.attach(session)
            await session.add_torrent(torrent)
            before = state.revision
            state.detach()
            await session.add_torrent(
                second_torrent,
            )
            assert state.revision == before

    def test_events_can_be_filtered_by_torrent(self) -> None:
        state = AppState()
        state.reduce(make_event(EventType.LOG, message="a", torrent_id="aa"))
        state.reduce(make_event(EventType.LOG, message="b", torrent_id="bb"))

        assert [event.message for event in state.events_for("bb")] == ["b"]

    def test_clearing_keeps_the_session_but_forgets_the_timeline(self) -> None:
        state = AppState()
        state.reduce(make_event(EventType.LOG, message="a"))
        state.clear()

        assert state.events == ()
        assert state.counts() == {}
        assert state.revision == 2

    def test_a_bad_capacity_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            AppState(event_capacity=0)

    async def test_render_summarises_a_running_session(
        self, torrent: Torrent, directory: Path
    ) -> None:
        state = AppState()
        async with await quiet_session(directory) as session:
            state.attach(session)
            await session.add_torrent(torrent)
            rendered = state.render()

        assert "torrents: 1 (1 active)" in rendered
        assert torrent.hex_info_hash[:8] in rendered
        assert "last event:" in rendered

    async def test_totals_add_up_across_torrents(
        self, torrent: Torrent, second_torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            first = await session.add_torrent(torrent)
            second = await session.add_torrent(second_torrent)
            first.metrics.note_download(1000)  # counted, not invented
            second.metrics.note_download(24)

            totals = session.totals()

        assert totals.torrents == 2
        assert totals.downloaded_bytes >= 1024
        assert totals.as_dict()["downloaded_bytes"] == totals.downloaded_bytes


def test_logging_levels_survive_the_reducer() -> None:
    """An error stays an error: the UI colours events by their level."""
    state = AppState()
    state.reduce(make_event(EventType.PEER_FAILED, message="nope", level=logging.ERROR))
    assert state.events[0].level == logging.ERROR


class TestTorrentServiceSurface:
    """The facade the UI talks to."""

    async def test_it_forwards_the_engine(self, torrent: Torrent, directory: Path) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent, start=False)
            service = session.get(torrent.hex_info_hash)
            assert service is not None

            assert service.engine.storage.root == Path(service.download_directory)
            assert service.snapshot().pieces_total == torrent.piece_count
            assert service.torrent is torrent
            assert service.hex_info_hash == torrent.hex_info_hash

    async def test_it_waits_for_completion(self, torrent: Torrent, directory: Path) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent, start=False)
            service = session.get(torrent.hex_info_hash)
            assert service is not None

            # Nothing is serving, so a short wait must report "not finished"
            # rather than raising or hanging.
            assert await service.wait_until_complete(timeout=0.05) is False


class TestSessionEdges:
    async def test_engines_are_exposed_in_order(
        self, torrent: Torrent, second_torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            first = await session.add_torrent(torrent, start=False)
            second = await session.add_torrent(second_torrent, start=False)

            assert session.engines == (first, second)

    async def test_a_hash_in_the_wrong_case_is_still_found(
        self, torrent: Torrent, directory: Path
    ) -> None:
        """A hash pasted out of a magnet link may arrive in any case."""
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent, start=False)
            service = session.get(torrent.hex_info_hash.upper())

            assert service is not None
            assert service.hex_info_hash == torrent.hex_info_hash

    async def test_one_torrent_failing_to_stop_does_not_strand_the_others(
        self, torrent: Torrent, second_torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            good = await session.add_torrent(torrent)
            broken = await session.add_torrent(second_torrent)

            async def explode() -> None:
                raise RuntimeError("this torrent has come loose")

            service = session.get(second_torrent.hex_info_hash)
            assert service is not None
            service.stop = explode  # type: ignore[method-assign]

            await session.stop_all()  # must not raise

            assert good.state == TorrentState.STOPPED
            assert broken is not None


class TestAppStateEdges:
    def test_active_torrents_are_the_ones_transferring(self) -> None:
        """Built by hand: this is about the snapshot, not about a session."""
        from app.services.app_state import AppSnapshot
        from app.services.engine import ResumeSummary
        from app.services.torrent_service import TorrentView

        def view(name: str, state: TorrentState) -> TorrentView:
            return TorrentView(
                info_hash=name,
                name=name,
                state=state,
                progress=0.5,
                total_length=1024,
                verified_pieces=1,
                missing_pieces=1,
                piece_count=2,
                port=6881,
                resumed=ResumeSummary(),
            )

        snapshot = AppSnapshot(
            totals=Session(Config()).totals(),
            torrents=(
                view("downloading", TorrentState.DOWNLOADING),
                view("stopped", TorrentState.STOPPED),
                view("seeding", TorrentState.SEEDING),
            ),
            events=(),
            counts=(),
            revision=1,
            generated_at=0.0,
        )

        assert [each.name for each in snapshot.active_torrents] == ["downloading", "seeding"]

    def test_attaching_twice_replaces_the_subscription(self) -> None:
        """A state belongs to one bus at a time: the newest one wins."""
        state = AppState()
        first, second = EventBus(), EventBus()

        state.subscribe_to(first)
        state.subscribe_to(second)

        first.emit(make_event(EventType.LOG, message="ignored"))
        second.emit(make_event(EventType.LOG, message="counted"))

        assert state.count(EventType.LOG) == 1
        assert state.events[-1].message == "counted"

    def test_the_session_property_reports_what_is_attached(self) -> None:
        state = AppState()
        assert state.session is None

        session = Session(Config())
        state.attach(session)
        assert state.session is session

    def test_terabytes_are_rendered(self) -> None:
        from app.services.app_state import _size

        assert _size(2 * 1024**4) == "2.0 TiB"
        assert _size(512) == "512 B"
        assert _size(2048) == "2.0 KiB"

    def test_a_state_built_with_a_session_is_wired_immediately(self) -> None:
        """``AppState(session)`` is the whole point of the constructor argument."""
        session = Session(Config())
        state = AppState(session)

        session.event_bus.emit(make_event(EventType.LOG, message="wired"))

        assert state.count(EventType.LOG) == 1
        assert state.session is session


class TestSessionDhtAnnouncing:
    """The session offers its torrents to the DHT, and takes them back.

    M15 gave the client a DHT it could *ask*; this is the other half. What is
    checked here is the wiring — that a torrent added is a torrent registered,
    that a private one is not, and that the loop only runs while a node does.
    :mod:`tests.discovery.test_dht_announcer` covers the loop itself.
    """

    @staticmethod
    def _dht_config() -> Config:
        """DHT on, port chosen by the OS, and no bootstrap: nothing leaves the box."""
        return Config().with_overrides(dht={"enabled": True, "port": 0, "bootstrap_nodes": []})

    async def test_the_announcer_exists_before_any_node_does(self, directory: Path) -> None:
        session = Session(Config(), download_directory=directory)
        assert session.announcer.node is None
        assert session.announcer.registered == ()
        await session.aclose()

    async def test_an_added_torrent_is_offered_to_the_dht(
        self, torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent, start=False)

            assert session.announcer.registered == (torrent.info_hash,)

    async def test_a_private_torrent_is_never_published(
        self, payload: bytes, directory: Path
    ) -> None:
        # BEP 27: private means trackers only. The DHT is a tracker we do not
        # control, so the flag has to stop the announce here.
        private = parse_torrent(
            build_torrent_bytes(payload, name="private.bin", announce=None, private=True)
        )
        assert private.private is True

        async with Session(self._dht_config(), download_directory=directory) as session:
            assert await session.start_dht() is not None, "a bound node, so a pass really runs"
            await session.add_torrent(private, listen=True)

            assert await session.announcer.announce_once() == 0

            status = session.announcer.status(private.info_hash)
            assert status is not None
            assert "private" in status.skipped
            assert session.announcer.published == 0

    async def test_a_torrent_that_is_not_listening_publishes_nothing(
        self, torrent: Torrent, directory: Path
    ) -> None:
        async with Session(self._dht_config(), download_directory=directory) as session:
            assert await session.start_dht() is not None
            await session.add_torrent(torrent, start=False, listen=False)

            assert await session.announcer.announce_once() == 0

            status = session.announcer.status(torrent.info_hash)
            assert status is not None
            assert status.skipped == "not listening: no port to publish"
            assert session.announcer.published == 0

    async def test_without_a_node_no_torrent_is_even_considered(
        self, torrent: Torrent, directory: Path
    ) -> None:
        # No node means no pass: attempts stay at zero, rather than recording a
        # skip for something that was never looked at.
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent, start=False)

            assert await session.announcer.announce_once() == 0

            status = session.announcer.status(torrent.info_hash)
            assert status is not None
            assert status.attempts == 0
            assert status.skipped == ""

    async def test_a_removed_torrent_stops_being_published(
        self, torrent: Torrent, directory: Path
    ) -> None:
        async with await quiet_session(directory) as session:
            await session.add_torrent(torrent, start=False)
            assert session.announcer.registered == (torrent.info_hash,)

            await session.remove(torrent.hex_info_hash)

            assert session.announcer.registered == ()

    async def test_the_loop_runs_only_while_a_node_does(
        self, torrent: Torrent, directory: Path
    ) -> None:
        async with Session(self._dht_config(), download_directory=directory) as session:
            assert session.announcer.running is False, "no node yet, so nothing to publish with"

            node = await session.start_dht()

            assert node is not None
            assert session.announcer.node is node
            assert session.announcer.running is True

            await session.add_torrent(torrent, start=False)
            await session.announcer.stop()
            await session.stop_dht()

            assert session.announcer.running is False
            assert session.announcer.node is None
            assert session.announcer.registered == (torrent.info_hash,), (
                "the torrents stay registered; only the node went away"
            )

    async def test_a_disabled_dht_leaves_the_announcer_alone(self, directory: Path) -> None:
        async with await quiet_session(directory) as session:
            assert await session.start_dht() is None
            assert session.announcer.node is None
            assert session.announcer.running is False

    async def test_the_interval_comes_from_the_configuration(self, directory: Path) -> None:
        config = self._dht_config().with_overrides(dht={"announce_interval": 120.0})

        async with Session(config, download_directory=directory) as session:
            assert session.announcer.interval == 120.0

    async def test_closing_the_session_closes_the_announcer(
        self, torrent: Torrent, directory: Path
    ) -> None:
        session = Session(self._dht_config(), download_directory=directory)
        await session.add_torrent(torrent, start=False)
        await session.start_dht()

        await session.aclose()

        assert session.announcer.registered == ()
        assert session.announcer.running is False
