"""Engine wiring, checked with stand-ins instead of sockets.

The engine is the only module that knows how the subsystems fit together, so
the thing worth testing here is the *wiring*: who hears about a block, who
hears about a request, who hears about a hang-up, and what state the engine
reports while all of it happens. Real sockets are the integration test's job
(:mod:`tests.services.test_integration`); this file swaps in doubles so a
broken callback is reported as a broken callback, not as a timeout.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.core.config import Config
from app.core.event_bus import EventBus
from app.core.events import EventType
from app.download.manager import DownloadManager
from app.peer.bitfield import Bitfield
from app.peer.connection import SwarmContext
from app.peer.discovery.peer_manager import PeerManager
from app.services.engine import Engine, ResumeSummary, TorrentState, build_engine, build_have
from app.statistics.metrics import MetricsCollector
from app.storage.manager import StorageManager
from app.torrent import Torrent, parse_torrent
from app.tracker.base import PeerAddress
from app.upload.manager import UploadManager
from tools.make_test_torrent import build_torrent_bytes


class FakePeers:
    """Stand-in for :class:`PeerManager`: records the callbacks it was given."""

    def __init__(self, torrent: Torrent) -> None:
        self.context = SwarmContext.from_torrent(torrent)
        self.peer_id = b"-FAKE00-123456789012"
        self.on_block = None
        self.on_have = None
        self.on_disconnect = None
        self.on_request = None
        self.on_cancel = None
        self.added: list[tuple[tuple[str, int], ...]] = []
        self.stats = type("Stats", (), {"connected": 0, "candidates": 0, "discovered": 0})()
        self.started = 0
        self.stopped = 0

    def add_peers(self, peers, *, source=None) -> int:
        self.added.append(tuple(peers))
        return len(self.added[-1])

    def start(self, **_: object) -> object:
        self.started += 1
        return object()

    async def stop(self) -> None:
        self.stopped += 1

    def unchoked_peers(self) -> tuple[object, ...]:
        return ()


class FakeDownload:
    """Stand-in for :class:`DownloadManager`."""

    def __init__(self, torrent: Torrent, *, complete: bool = False) -> None:
        self.torrent = torrent
        self._complete = complete
        self.started = 0
        self.stopped = 0
        self.stats = type("Stats", (), {"blocks_received": 0, "pieces_verified": 0})()
        self.completed = set()
        self.on_block = None
        self.on_have = None
        self.on_disconnect = None

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def progress(self) -> float:
        return 1.0 if self._complete else 0.0

    @property
    def verified_pieces(self) -> frozenset[int]:
        return frozenset(self.completed)

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def wait_until_complete(self, *, timeout: float = 1.0, interval: float = 0.01) -> bool:
        return self._complete


class FakeUpload:
    """Stand-in for :class:`UploadManager`."""

    def __init__(self, torrent: Torrent) -> None:
        self.torrent = torrent
        self.have = Bitfield(torrent.piece_count)
        self.started = 0
        self.stopped = 0
        self.noted: list[int] = []
        self.cancels: list[tuple[object, object]] = []
        self.stats = type("Stats", (), {"blocks_served": 0, "bytes_uploaded": 0})()
        self.requests: list[tuple[object, object]] = []
        self.on_disconnect = None

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def note_piece_verified(self, index: int) -> None:
        self.noted.append(index)
        self.have.set(index)

    def on_request(self, peer: object, message: object) -> None:
        self.requests.append((peer, message))

    def on_cancel(self, peer: object, message: object) -> None:
        self.cancels.append((peer, message))


def make_engine(
    torrent: Torrent,
    storage: StorageManager,
    *,
    bus: EventBus | None = None,
    complete: bool = False,
    resumed: ResumeSummary | None = None,
) -> tuple[Engine, FakePeers, FakeDownload, FakeUpload]:
    """An engine over stand-ins, plus the stand-ins so tests can inspect them."""
    peers = FakePeers(torrent)
    download = FakeDownload(torrent, complete=complete)
    upload = FakeUpload(torrent)
    metrics = MetricsCollector(
        torrent,
        download=download,
        upload=upload,
        peers=peers,
        event_bus=bus,  # type: ignore[arg-type]
    )
    engine = Engine(
        torrent,
        storage=storage,
        peers=peers,  # type: ignore[arg-type]
        download=download,  # type: ignore[arg-type]
        upload=upload,  # type: ignore[arg-type]
        metrics=metrics,
        event_bus=bus,
        resumed=resumed,
    )
    return engine, peers, download, upload


@pytest.fixture
def torrent(payload: bytes) -> Torrent:
    """A torrent with no trackers: these tests are about wiring, not dialling."""
    return parse_torrent(
        build_torrent_bytes(payload, name="wiring.bin", piece_length=32 * 1024, announce=None)
    )


@pytest.fixture
def storage(torrent: Torrent, tmp_path: Path) -> StorageManager:
    return StorageManager(torrent, tmp_path, config=Config().storage)


class TestBuildHave:
    def test_an_empty_bitfield_claims_nothing(self, torrent: Torrent) -> None:
        have = build_have(torrent)
        assert have.count == 0
        assert have.to_bytes() == bytes((torrent.piece_count + 7) // 8)

    def test_only_the_pieces_named_are_claimed(self, torrent: Torrent) -> None:
        have = build_have(torrent, (0, 3))
        assert have.count == 2
        assert have.has(0) and have.has(3)
        assert not have.has(1)


class TestEngineWiring:
    async def test_starting_wires_every_callback(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, peers, download, upload = make_engine(torrent, storage)
        await engine.start()

        assert peers.on_block == download.on_block
        assert peers.on_request == upload.on_request
        assert peers.on_disconnect == engine._on_disconnect
        assert peers.started == 1
        assert download.started == 1
        assert upload.started == 1

    async def test_a_disconnect_reaches_both_engines(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        """One hang-up, two listeners: the downloader re-plans, the uploader drops its queue."""
        engine, _peers, download, upload = make_engine(torrent, storage)
        await engine.start()

        calls: list[str] = []
        download.on_disconnect = lambda peer, reason="": calls.append(f"down:{reason}")  # type: ignore[method-assign]
        upload.on_disconnect = lambda peer, reason="": calls.append(f"up:{reason}")  # type: ignore[method-assign]
        engine._on_disconnect(object(), "bye")

        assert calls == ["down:bye", "up:bye"]

    async def test_a_verified_piece_is_offered_to_the_swarm(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        bus = EventBus()
        engine, _peers, _download, upload = make_engine(torrent, storage, bus=bus)
        await engine.start()

        bus.emit(_event(EventType.PIECE_VERIFIED, index=4, torrent_id=torrent.hex_info_hash))
        await bus.drain()

        assert upload.noted == [4], "a peer cannot ask for a piece nobody announced"
        assert upload.have.has(4)

    async def test_the_have_bitfield_is_backfilled_from_disk(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        """An engine built without a bus must still seed what it already has."""
        engine, _peers, download, upload = make_engine(torrent, storage)
        download.completed = {0, 1, 2}
        engine.snapshot()

        assert upload.have.count == 3

    async def test_a_cancel_is_passed_straight_through(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, peers, _download, upload = make_engine(torrent, storage)
        await engine.start()

        assert peers.on_cancel is not None
        peers.on_cancel("peer", "message")  # type: ignore[operator]

        assert upload.cancels == [("peer", "message")]


class TestEngineState:
    async def test_a_fresh_engine_is_idle(self, torrent: Torrent, storage: StorageManager) -> None:
        engine, _p, _d, _u = make_engine(torrent, storage)
        assert engine.state == TorrentState.IDLE
        assert not engine.running

    async def test_a_running_engine_that_has_not_moved_is_starting(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, _p, _d, _u = make_engine(torrent, storage)
        await engine.start()
        assert engine.state == TorrentState.STARTING

    async def test_a_byte_on_the_wire_makes_it_downloading(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, _p, download, _u = make_engine(torrent, storage)
        await engine.start()
        download.stats.blocks_received = 1
        assert engine.state == TorrentState.DOWNLOADING

    async def test_a_complete_engine_seeds(self, torrent: Torrent, storage: StorageManager) -> None:
        engine, _p, _d, _u = make_engine(torrent, storage, complete=True)
        await engine.start()
        assert engine.state == TorrentState.SEEDING
        assert engine.progress == 1.0

    async def test_pause_and_resume_round_trip(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, _peers, download, upload = make_engine(torrent, storage)
        await engine.start()

        await engine.pause()
        assert engine.state == TorrentState.PAUSED
        assert download.stopped == 1 and upload.stopped == 1

        await engine.resume()
        assert engine.state == TorrentState.STARTING
        assert download.started == 2

    async def test_stop_leaves_a_stopped_engine_that_can_start_again(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, peers, download, _upload = make_engine(torrent, storage)
        await engine.start()
        await engine.stop()
        assert engine.state == TorrentState.STOPPED

        await engine.start()
        assert engine.state == TorrentState.STARTING
        # The wiring survives a stop: a restarted engine still hears blocks.
        assert peers.on_block == download.on_block

    async def test_stopping_twice_is_a_no_op(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, _p, download, _u = make_engine(torrent, storage)
        await engine.start()
        await engine.stop()
        await engine.stop()
        assert download.stopped == 1

    async def test_a_broken_prepare_is_reported_not_raised_into_the_caller(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        bus = EventBus()
        seen: list[object] = []
        bus.subscribe_all(seen.append)
        engine, _p, _d, _u = make_engine(torrent, storage, bus=bus)

        async def broken() -> None:
            raise OSError("disk on fire")

        engine._prepare = broken  # type: ignore[method-assign]
        with pytest.raises(OSError, match="disk on fire"):
            await engine.start()

        assert engine.state == TorrentState.ERROR
        assert engine.error and "disk on fire" in engine.error
        assert any(event.type == EventType.TORRENT_STOPPED for event in seen)  # type: ignore[attr-defined]


class TestBuildEngine:
    async def test_it_builds_a_real_unstarted_engine(
        self, torrent: Torrent, tmp_path: Path
    ) -> None:
        engine = await build_engine(
            torrent, download_directory=tmp_path, listen=False, resume=False
        )
        try:
            assert isinstance(engine.peers, PeerManager)
            assert isinstance(engine.download, DownloadManager)
            assert isinstance(engine.upload, UploadManager)
            assert engine.state == TorrentState.IDLE
            assert engine.resumed.empty
        finally:
            await engine.aclose()

    async def test_it_adopts_progress_from_a_previous_run(
        self, torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        """Write the payload, save state, rebuild: the pieces come back."""
        directory = tmp_path / "downloads"
        first = await build_engine(torrent, download_directory=directory, listen=False)
        await first.storage.prepare()
        await first.storage.write_piece(0, payload[: torrent.piece_length])
        await first.storage.save_resume(uploaded=0)
        await first.aclose()

        second = await build_engine(torrent, download_directory=directory, listen=False)
        try:
            assert second.resumed.pieces == 1
            assert not second.resumed.empty
            assert 0 in second.download.verified_pieces
        finally:
            await second.aclose()

    async def test_listening_gives_the_engine_a_real_port(
        self, torrent: Torrent, tmp_path: Path
    ) -> None:
        engine = await build_engine(torrent, download_directory=tmp_path, listen=True)
        try:
            assert engine.listener is not None
            await engine.start()
            assert engine.port > 0
        finally:
            await engine.aclose()


def _event(event_type: EventType, **data: object) -> object:
    """Build an event the way the engine's subsystems do."""
    from app.core.events import make_event

    return make_event(event_type, message="test", level=logging.INFO, data=data)


class TestEngineAccessors:
    """The small surface the services and the UI read off an engine."""

    async def test_the_engine_exposes_its_parts(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, peers, download, upload = make_engine(torrent, storage)

        assert engine.torrent is torrent
        assert engine.info_hash == torrent.info_hash
        assert engine.hex_info_hash == torrent.hex_info_hash
        assert engine.storage is storage
        assert engine.peers is peers
        assert engine.download is download
        assert engine.upload is upload
        assert engine.tracker is None
        assert engine.listener is None
        assert engine.metrics is not None
        assert not engine.paused

    async def test_starting_a_running_engine_is_a_no_op(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, _peers, download, _upload = make_engine(torrent, storage)
        await engine.start()
        await engine.start()

        assert download.started == 1, "a second start must not restart the engines"

    async def test_pausing_an_engine_that_is_not_running_does_nothing(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, _peers, download, _upload = make_engine(torrent, storage)
        await engine.pause()

        assert engine.state == TorrentState.IDLE
        assert download.stopped == 0

    async def test_the_context_manager_starts_and_closes(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, _peers, download, _upload = make_engine(torrent, storage)
        async with engine as running:
            assert running is engine
            assert running.running
            assert running.state == TorrentState.STARTING

        assert download.stopped == 1
        assert engine.state == TorrentState.STOPPED


class TestEngineAnnounce:
    async def test_announcing_without_a_tracker_is_not_an_error(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        """A swarm can be assembled by hand; the tracker is optional."""
        engine, _peers, _download, _upload = make_engine(torrent, storage)
        assert await engine.announce() == 0

    async def test_a_tracker_answer_is_fed_back_into_the_swarm(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, peers, _download, _upload = make_engine(torrent, storage)
        outcome = SimpleNamespace(peers=(PeerAddress(host="10.0.0.5", port=6881),))

        engine._adopt(outcome)

        assert peers.added and len(peers.added[0]) == 1

    async def test_an_answer_with_no_peers_adds_nothing(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, peers, _download, _upload = make_engine(torrent, storage)

        assert engine._adopt_peers(SimpleNamespace(peers=())) == 0
        assert peers.added == []

    async def test_engine_reports_state_over_a_tracker_answer(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        """Uploaded and downloaded are what the swarm is owed and has given."""
        engine, _peers, _download, upload = make_engine(torrent, storage)
        upload.stats.bytes_uploaded = 4096

        state = engine._tracker_state()

        assert state["uploaded"] == 4096
        assert state["downloaded"] == 0
        assert state["left"] == torrent.total_length


class FakeTracker:
    """Stand-in for :class:`TrackerManager`: one canned answer, then silence."""

    def __init__(self, peers: tuple[PeerAddress, ...] = ()) -> None:
        self.peers = peers
        self.calls: list[dict[str, int]] = []
        self.closed = 0
        self.outcome = None

    async def announce(self, *, uploaded: int, downloaded: int, left: int, event: object = None):
        self.calls.append({"uploaded": uploaded, "downloaded": downloaded, "left": left})
        return SimpleNamespace(peers=self.peers)

    async def aclose(self) -> None:
        self.closed += 1


class TestEngineWithATracker:
    async def test_announcing_reports_the_truth_and_adopts_the_peers(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, peers, _download, upload = make_engine(torrent, storage)
        upload.stats.bytes_uploaded = 2048
        tracker = FakeTracker((PeerAddress(host="10.0.0.7", port=51413),))
        engine._tracker = tracker  # type: ignore[assignment]

        added = await engine.announce()

        assert added == 1
        assert peers.added[-1][0].address == ("10.0.0.7", 51413)
        assert tracker.calls == [{"uploaded": 2048, "downloaded": 0, "left": torrent.total_length}]

    async def test_closing_the_engine_closes_the_tracker(
        self, torrent: Torrent, storage: StorageManager
    ) -> None:
        engine, _peers, _download, _upload = make_engine(torrent, storage)
        tracker = FakeTracker()
        engine._tracker = tracker  # type: ignore[assignment]

        await engine.aclose()

        assert tracker.closed == 1
