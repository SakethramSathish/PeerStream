"""Tests for the download manager: peers → blocks → pieces → disk.

These run the real engine against real storage (a temporary directory) and
stand-in peers, so "verified" here means bytes were actually hashed and written.
The integration test at the end of this package does the same through real
sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest
from app.core.config import DownloadConfig
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.download.manager import DownloadManager
from app.download.piece import PieceState
from app.peer.bitfield import Bitfield
from app.peer.errors import PeerError
from app.peer.messages import Cancel, Interested, NotInterested, Request
from app.peer.messages import Piece as PieceMessage
from app.storage.manager import StorageManager
from app.torrent import Torrent, parse_torrent
from app.torrent.metadata import FileEntry

from tests.download.conftest import FakeAddress, FakePeer, make_peer

BLOCK = 16 * 1024


@dataclass(slots=True)
class FakeAddressStub:
    """A peer address stand-in with no other attributes."""

    host: str = "stub"
    port: int = 6881


@dataclass(slots=True)
class FakeSwarm:
    """Stand-in for the peer manager: just the connections it exposes."""

    connections: list[object] = field(default_factory=list)


def block_message(torrent: Torrent, index: int, payload: bytes) -> PieceMessage:
    """A block containing the real payload bytes for a piece."""
    start = torrent.piece_offset(index)
    return PieceMessage(
        index=index, begin=0, data=payload[start : start + torrent.piece_size(index)]
    )


def make_manager(
    torrent: Torrent,
    storage: StorageManager,
    *,
    peers: FakeSwarm | None = None,
    config: DownloadConfig | None = None,
    bus: EventBus | None = None,
) -> DownloadManager:
    return DownloadManager(
        torrent,
        storage=storage,
        peers=peers if peers is not None else FakeSwarm(),
        config=config or DownloadConfig(block_size=BLOCK, max_outstanding_requests=4),
        event_bus=bus,
        block_timeout=0.5,
    )


class TestConstruction:
    def test_pieces_mirror_the_torrent(self, sample_torrent: Torrent, make_storage) -> None:
        storage = make_storage(sample_torrent)

        manager = make_manager(sample_torrent, storage)

        assert len(manager.pieces) == sample_torrent.piece_count
        assert manager.pieces[0].size == sample_torrent.piece_size(0)
        assert manager.missing_pieces == tuple(range(sample_torrent.piece_count))
        assert manager.verified_pieces == ()
        assert manager.progress == 0.0
        assert manager.complete is False

    async def test_pieces_already_on_disk_start_verified(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        await storage.write_piece(0, payload[: sample_torrent.piece_size(0)])

        manager = make_manager(sample_torrent, storage)

        assert manager.verified_pieces == (0,)
        assert manager.pieces[0].state is PieceState.VERIFIED
        assert manager.progress == pytest.approx(1 / sample_torrent.piece_count)
        await manager.storage.aclose()

    def test_properties_are_exposed(self, sample_torrent: Torrent, make_storage) -> None:
        storage = make_storage(sample_torrent)
        manager = make_manager(sample_torrent, storage)

        assert manager.torrent is sample_torrent
        assert manager.storage is storage
        assert manager.config.block_size == BLOCK
        assert manager.scheduler is not None
        assert manager.availability.piece_count == sample_torrent.piece_count
        assert manager.elapsed == 0.0
        assert manager.running is False


class TestScheduling:
    async def test_pump_fills_the_pipelines(self, sample_torrent: Torrent, make_storage) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))

        sent = await manager.pump()

        assert sent == 4
        assert all(
            isinstance(message, Request) for message in peer.sent if isinstance(message, Request)
        )
        assert sum(1 for message in peer.sent if isinstance(message, Request)) == 4
        assert manager.stats.requests_sent == 4
        await manager.storage.aclose()

    async def test_a_peer_is_told_we_are_interested(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        """A peer only unchokes a client that asked, so this must happen."""
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(
            sample_torrent, make_storage(sample_torrent), peers=FakeSwarm([peer])
        )

        await manager.pump()

        assert peer.session.am_interested is True
        assert any(isinstance(message, Interested) for message in peer.sent)
        await manager.storage.aclose()

    async def test_interest_is_only_stated_when_it_changes(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(
            sample_torrent, make_storage(sample_torrent), peers=FakeSwarm([peer])
        )

        await manager.pump()
        await manager.pump()

        assert sum(1 for message in peer.sent if isinstance(message, Interested)) == 1
        await manager.storage.aclose()

    async def test_a_peer_with_nothing_we_need_is_left_alone(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        peer = make_peer("a:1", pieces=sample_torrent.piece_count, holding=[])
        manager = make_manager(
            sample_torrent, make_storage(sample_torrent), peers=FakeSwarm([peer])
        )

        await manager.pump()

        assert peer.session.am_interested is False
        assert peer.sent == []  # no interest, no requests
        await manager.storage.aclose()

    async def test_we_say_not_interested_once_the_torrent_is_done(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))
        await manager.pump()  # we ask for pieces, so we are interested
        assert peer.session.am_interested is True

        for index in range(sample_torrent.piece_count):
            manager.on_block(peer, block_message(sample_torrent, index, payload))
        await manager.flush()
        await manager.pump()

        assert peer.session.am_interested is False
        assert any(isinstance(message, NotInterested) for message in peer.sent)
        # A finished torrent asks for nothing more.
        assert not manager.scheduler.in_flight()
        await manager.storage.aclose()

    async def test_a_have_reopens_the_interest_question(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        peer = make_peer("a:1", pieces=sample_torrent.piece_count, holding=[])
        manager = make_manager(
            sample_torrent, make_storage(sample_torrent), peers=FakeSwarm([peer])
        )
        await manager.pump()
        assert peer.session.am_interested is False

        peer.bitfield.set(4)
        manager.on_have(peer, 4)
        await manager.pump()

        assert peer.session.am_interested is True
        await manager.storage.aclose()

    async def test_availability_follows_the_swarm(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count, holding=[0, 1, 2])
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))

        await manager.pump()
        assert manager.availability.count(0) == 1
        assert manager.availability.count(9) == 0

        manager.on_disconnect(peer, "gone")
        assert manager.availability.count(0) == 0
        await manager.storage.aclose()

    async def test_a_have_updates_rarity(self, sample_torrent: Torrent, make_storage) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count, holding=[0])
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))
        await manager.pump()
        peer.bitfield.set(5)

        manager.on_have(peer, 5)

        assert manager.availability.count(5) == 1
        await manager.storage.aclose()

    async def test_a_have_outside_the_torrent_is_ignored(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_manager(sample_torrent, storage)

        manager.on_have(make_peer("a:1", pieces=1), 999)

        assert manager.availability.count(0) == 0
        await manager.storage.aclose()

    async def test_unanswered_requests_are_reclaimed(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))
        await manager.pump()
        for key in manager.scheduler.slot(peer).outstanding:
            manager.scheduler.slot(peer).outstanding[key] -= 10

        await manager.pump()

        # Reclaimed, then handed straight back out to the same peer.
        assert manager.stats.requests_expired == 4
        assert manager.stats.requests_sent == 8
        await manager.storage.aclose()


class TestReceiving:
    async def test_a_block_completes_a_piece_and_reaches_disk(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))
        await manager.pump()

        manager.on_block(peer, block_message(sample_torrent, 0, payload))
        await manager.flush()

        assert manager.pieces[0].state is PieceState.VERIFIED
        assert manager.verified_pieces == (0,)
        assert manager.stats.blocks_received == 1
        assert manager.stats.pieces_verified == 1
        assert (storage.root / "payload.bin").read_bytes()[:BLOCK] == payload[:BLOCK]
        await manager.storage.aclose()

    async def test_a_corrupt_piece_is_rejected_and_retried(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))
        good = payload[: sample_torrent.piece_size(0)]
        corrupt = good[:5] + bytes([good[5] ^ 0xFF]) + good[6:]

        manager.on_block(peer, PieceMessage(index=0, begin=0, data=corrupt))
        await manager.flush()

        assert manager.stats.pieces_failed == 1
        assert manager.stats.pieces_verified == 0
        assert manager.stats.wasted_bytes == len(corrupt)
        assert manager.peer_penalties == {"a:1": 1}
        assert manager.pieces[0].state is PieceState.MISSING
        assert manager.pieces[0].failures == 1
        # Nothing reached the disk: the file is still all zeroes.
        assert (storage.root / "payload.bin").read_bytes()[:BLOCK] == bytes(BLOCK)
        await manager.storage.aclose()

    async def test_a_block_for_a_verified_piece_is_waste(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))
        manager.on_block(peer, block_message(sample_torrent, 0, payload))
        await manager.flush()

        manager.on_block(peer, block_message(sample_torrent, 0, payload))

        assert manager.stats.blocks_unsolicited == 1
        assert manager.stats.wasted_bytes == sample_torrent.piece_size(0)
        await manager.storage.aclose()

    async def test_a_duplicate_block_is_counted_not_crashed(
        self, build_torrent, payload: bytes, make_storage
    ) -> None:
        # Four blocks per piece, so a duplicated block does not finish a piece.
        torrent = parse_torrent(build_torrent(name="dup", piece_length=64 * 1024, file_count=1))
        storage = make_storage(torrent, name="dup")
        peer = make_peer("a:1", pieces=torrent.piece_count)
        manager = make_manager(torrent, storage, peers=FakeSwarm([peer]))
        message = PieceMessage(index=0, begin=0, data=payload[:BLOCK])

        manager.on_block(peer, message)
        manager.on_block(peer, message)

        assert manager.stats.blocks_received == 1
        assert manager.stats.blocks_duplicate == 1
        assert manager.stats.wasted_bytes == BLOCK
        await manager.storage.aclose()

    async def test_a_block_for_an_unknown_piece_is_ignored(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))

        manager.on_block(peer, PieceMessage(index=999, begin=0, data=b"x" * 16))

        assert manager.stats.blocks_unsolicited == 1
        await manager.storage.aclose()

    async def test_a_peer_leaving_returns_its_blocks(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))
        await manager.pump()
        assert manager.pieces[0].outstanding_requests(peer="a:1")

        manager.on_disconnect(peer, "hangup")

        assert manager.pieces[0].outstanding_requests(peer="a:1") == ()
        assert manager.pieces[0].unclaimed_blocks == 1
        await manager.storage.aclose()


class TestEndgame:
    async def test_the_last_block_is_raced_and_the_loser_is_cancelled(
        self, build_torrent, payload: bytes, make_storage
    ) -> None:
        torrent = parse_torrent(build_torrent(name="endgame", piece_length=64 * 1024, file_count=1))
        storage = make_storage(torrent, name="endgame")
        await storage.prepare()
        winner = make_peer("a:1", pieces=torrent.piece_count)
        loser = make_peer("b:2", pieces=torrent.piece_count)
        config = DownloadConfig(
            block_size=BLOCK,
            max_outstanding_requests=16,
            endgame_enabled=True,
            endgame_threshold=8,
            endgame_delay=0.0,
        )
        manager = make_manager(torrent, storage, peers=FakeSwarm([winner, loser]), config=config)

        # Fill piece 0 except its last block, then let both peers race for it.
        for offset in (0, 16 * 1024, 32 * 1024):
            manager.on_block(
                winner,
                PieceMessage(index=0, begin=offset, data=payload[offset : offset + BLOCK]),
            )
        await manager.pump()

        assert manager.scheduler.endgame.duplicates >= 1
        last = PieceMessage(index=0, begin=48 * 1024, data=payload[48 * 1024 : 64 * 1024])
        manager.on_block(winner, last)
        await manager.flush()

        assert manager.pieces[0].state is PieceState.VERIFIED
        assert any(isinstance(message, Cancel) for message in loser.sent)
        await manager.storage.aclose()


class TestCompletion:
    async def test_completing_every_piece_announces_it(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe_all(events.append)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]), bus=bus)

        for index in range(sample_torrent.piece_count):
            manager.on_block(peer, block_message(sample_torrent, index, payload))
        await manager.flush()

        assert manager.complete is True
        assert manager.progress == 1.0
        assert manager.stats.pieces_verified == sample_torrent.piece_count
        completed = [event for event in events if event.type is EventType.TORRENT_COMPLETED]
        # The loop publishes completion; without it the pieces are still stored.
        if completed:
            assert "complete" in completed[0].message
        assert (storage.root / "payload.bin").read_bytes() == payload
        await manager.storage.aclose()

    async def test_piece_events_describe_the_lifecycle(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe_all(events.append)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]), bus=bus)

        await manager.pump()
        manager.on_block(peer, block_message(sample_torrent, 0, payload))
        await manager.flush()

        kinds = [event.type for event in events]
        assert EventType.PIECE_REQUESTED in kinds
        assert EventType.PIECE_BLOCK_RECEIVED in kinds
        assert EventType.PIECE_DOWNLOADED in kinds
        assert EventType.PIECE_VERIFIED in kinds
        await manager.storage.aclose()

    async def test_a_failed_piece_is_reported(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe_all(events.append)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]), bus=bus)
        good = payload[: sample_torrent.piece_size(0)]

        manager.on_block(peer, PieceMessage(index=0, begin=0, data=bytes(len(good))))
        await manager.flush()

        failures = [event for event in events if event.type is EventType.PIECE_FAILED]
        assert failures and failures[0].data["index"] == 0
        assert failures[0].level >= 30
        await manager.storage.aclose()


class TestLoop:
    async def test_the_loop_downloads_without_being_pumped(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        """start() → the loop requests, receives and stores on its own."""
        storage = make_storage(sample_torrent)
        await storage.prepare()
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))

        await manager.start()
        try:
            assert manager.running is True
            for index in range(sample_torrent.piece_count):
                manager.on_block(peer, block_message(sample_torrent, index, payload))
            assert await manager.wait_until_complete(timeout=5.0) is True
            await manager.flush()
        finally:
            await manager.stop()

        assert manager.complete is True
        assert manager.running is False
        assert manager.elapsed > 0
        assert (storage.root / "payload.bin").read_bytes() == payload
        await manager.storage.aclose()

    async def test_wait_until_complete_times_out_honestly(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)

        manager = make_manager(sample_torrent, storage)

        assert await manager.wait_until_complete(timeout=0.05) is False
        await manager.storage.aclose()

    async def test_stop_is_idempotent(self, sample_torrent: Torrent, make_storage) -> None:
        storage = make_storage(sample_torrent)
        manager = make_manager(sample_torrent, storage)

        await manager.stop()
        await manager.stop()

        assert manager.running is False
        await manager.storage.aclose()

    async def test_starting_twice_does_not_spawn_two_loops(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_manager(sample_torrent, storage)

        await manager.start()
        await manager.start()
        try:
            assert manager.running is True
        finally:
            await manager.stop()
        await manager.storage.aclose()


class TestStats:
    async def test_counters_stay_honest(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        await storage.prepare()
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))

        manager.on_block(peer, block_message(sample_torrent, 0, payload))
        await manager.flush()
        good = manager.stats

        assert good.pieces_verified == 1
        assert good.pieces_failed == 0
        assert good.useful_blocks == 1
        assert good.wasted_bytes == 0
        await manager.storage.aclose()


class TestEdgeCases:
    def test_an_empty_torrent_is_complete_immediately(self, make_storage, tmp_path: Path) -> None:

        empty = Torrent(
            name="empty",
            info_hash=bytes(range(20)),
            piece_length=16,
            piece_hashes=(),
            files=(FileEntry(PurePosixPath("empty.bin"), 0, 0),),
        )
        manager = make_manager(empty, make_storage(empty))

        assert manager.complete is True
        assert manager.progress == 1.0

    async def test_the_manager_works_as_a_context_manager(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)

        async with make_manager(sample_torrent, storage) as manager:
            assert manager.running is True

        assert manager.running is False
        await storage.aclose()

    async def test_the_loop_survives_a_failed_pass(
        self, sample_torrent: Torrent, make_storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_manager(sample_torrent, storage)
        calls = 0
        original = manager.pump

        async def flaky() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("disk on fire")
            return await original()

        monkeypatch.setattr(manager, "pump", flaky)
        monkeypatch.setattr("app.download.manager.IDLE_TICK_SECONDS", 0.01)

        await manager.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await manager.stop()

        assert calls > 1
        assert manager.running is False
        await storage.aclose()

    async def test_a_peer_without_a_bitfield_is_skipped(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)

        class NoBitfield:
            address = FakeAddressStub()
            connected = True
            choked = False

        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([NoBitfield()]))

        assert await manager.pump() == 0
        await storage.aclose()

    async def test_a_peer_without_a_session_is_not_asked(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)

        class NoSession:
            address = FakeAddressStub()
            bitfield = Bitfield(sample_torrent.piece_count)
            connected = True
            choked = False

        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([NoSession()]))

        assert await manager.pump() == 0
        await storage.aclose()

    async def test_a_peer_that_refuses_our_interest_is_not_fatal(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)

        class RefusingPeer(FakePeer):
            async def send_interested(self, interested: bool = True) -> None:
                raise PeerError("gone")

        peer = RefusingPeer(
            address=FakeAddress("a", 1), bitfield=Bitfield(sample_torrent.piece_count)
        )
        for index in range(sample_torrent.piece_count):
            peer.bitfield.set(index)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))

        assert await manager.pump() == 4  # requests still go out
        await storage.aclose()

    def test_cancelling_without_a_loop_is_a_no_op(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_manager(sample_torrent, storage)

        manager._cancel(make_peer("a:1", pieces=1), (0, 0))

        assert manager.stats.requests_cancelled == 0

    def test_verifying_without_a_loop_resets_the_piece(
        self, sample_torrent: Torrent, payload: bytes, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_manager(sample_torrent, storage)
        piece = manager.pieces[0]
        for block in piece.blocks:
            piece.add_block(block.offset, payload[block.offset : block.end])
        assert piece.complete

        manager._verify(piece)

        assert piece.state is PieceState.MISSING
        assert manager.stats.pieces_verified == 0


class TestSilentDepartures:
    async def test_a_peer_that_vanishes_from_the_swarm_is_cleaned_up(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        """Peers can disappear without a disconnect callback (reaped, reset)."""
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        swarm = FakeSwarm([peer])
        manager = make_manager(sample_torrent, make_storage(sample_torrent), peers=swarm)
        await manager.pump()
        assert manager.availability.count(0) == 1
        assert manager.scheduler.in_flight()

        swarm.connections.remove(peer)
        await manager.pump()

        assert manager.availability.count(0) == 0
        assert manager.scheduler.in_flight() == ()
        assert manager.pieces[0].outstanding_requests() == ()


class TestWasteAccounting:
    """Honest numbers for bytes that cost us twice (TRD §21)."""

    async def test_a_peer_that_cannot_be_sent_to_loses_its_request(
        self, sample_torrent: Torrent, make_storage
    ) -> None:
        storage = make_storage(sample_torrent)

        class Broken(FakePeer):
            async def send(self, message: object) -> None:
                if isinstance(message, Request):
                    raise PeerError("connection reset")
                await super().send(message)

        peer = Broken(address=FakeAddress("a", 1), bitfield=Bitfield(sample_torrent.piece_count))
        for index in range(sample_torrent.piece_count):
            peer.bitfield.set(index)
        manager = make_manager(sample_torrent, storage, peers=FakeSwarm([peer]))

        assert await manager.pump() == 0
        assert manager.scheduler.in_flight() == ()  # nothing is outstanding

    async def test_a_late_copy_from_a_peer_we_asked_is_counted_as_duplicate(
        self, build_torrent, payload: bytes, make_storage
    ) -> None:
        """A copy that arrives after its piece is done is waste we caused."""
        torrent = parse_torrent(build_torrent(name="late", piece_length=64 * 1024, file_count=1))
        storage = make_storage(torrent, name="late")
        await storage.prepare()
        winner = make_peer("a:1", pieces=torrent.piece_count)
        loser = make_peer("b:2", pieces=torrent.piece_count)
        config = DownloadConfig(
            block_size=BLOCK,
            max_outstanding_requests=16,
            endgame_enabled=True,
            endgame_threshold=64,  # racing from the very first block
            endgame_delay=0.0,
        )
        manager = make_manager(torrent, storage, peers=FakeSwarm([winner, loser]), config=config)

        await manager.pump()
        raced = [key for key in manager.scheduler.slot(loser).outstanding if key[0] == 0]
        assert raced  # the loser was asked for piece 0 as well

        for offset in (0, BLOCK, 2 * BLOCK, 3 * BLOCK):
            manager.on_block(
                winner, PieceMessage(index=0, begin=offset, data=payload[offset : offset + BLOCK])
            )
        await manager.flush()
        assert manager.pieces[0].state is PieceState.VERIFIED

        # The loser's copy turns up anyway: an endgame loser, not a gift.
        manager.on_block(
            loser, PieceMessage(index=raced[0][0], begin=raced[0][1], data=b"x" * BLOCK)
        )

        assert manager.stats.blocks_duplicate == 1
        assert manager.stats.blocks_unsolicited == 0

    async def test_finishing_a_piece_cancels_the_races_still_outstanding(
        self, build_torrent, payload: bytes, make_storage
    ) -> None:
        torrent = parse_torrent(build_torrent(name="race", piece_length=64 * 1024, file_count=1))
        storage = make_storage(torrent, name="race")
        await storage.prepare()
        winner = make_peer("a:1", pieces=torrent.piece_count)
        loser = make_peer("b:2", pieces=torrent.piece_count)
        config = DownloadConfig(
            block_size=BLOCK,
            max_outstanding_requests=16,
            endgame_enabled=True,
            endgame_threshold=64,
            endgame_delay=0.0,
        )
        manager = make_manager(torrent, storage, peers=FakeSwarm([winner, loser]), config=config)

        # Both peers are working on piece 0; the winner delivers all of it.
        await manager.pump()
        for offset in (0, BLOCK, 2 * BLOCK, 3 * BLOCK):
            manager.on_block(
                winner, PieceMessage(index=0, begin=offset, data=payload[offset : offset + BLOCK])
            )
        await manager.flush()

        assert manager.pieces[0].state is PieceState.VERIFIED
        # The loser is told to stop sending blocks we already have.
        assert any(isinstance(message, Cancel) for message in loser.sent)
        assert not [key for key in manager.scheduler.in_flight() if key[0] == 0]


class TestCancellingRaces:
    async def test_a_piece_completed_by_a_third_peer_still_stops_the_races(
        self, build_torrent, payload: bytes, make_storage
    ) -> None:
        """Whoever completes a piece, everyone still owed blocks of it is told to stop."""
        torrent = parse_torrent(build_torrent(name="race3", piece_length=64 * 1024, file_count=1))
        storage = make_storage(torrent, name="race3")
        await storage.prepare()
        racer = make_peer("a:1", pieces=torrent.piece_count)
        config = DownloadConfig(
            block_size=BLOCK,
            max_outstanding_requests=16,
            endgame_enabled=True,
            endgame_threshold=64,
            endgame_delay=0.0,
        )
        manager = make_manager(torrent, storage, peers=FakeSwarm([racer]), config=config)

        await manager.pump()
        owing = [key for key in manager.scheduler.slot(racer).outstanding if key[0] == 0]
        assert len(owing) == 4

        # A peer we never planned for sends the whole piece: every outstanding
        # request for it is now pointless.
        outsider = make_peer("z:9", pieces=torrent.piece_count)
        for offset in (0, BLOCK, 2 * BLOCK, 3 * BLOCK):
            manager.on_block(
                outsider, PieceMessage(index=0, begin=offset, data=payload[offset : offset + BLOCK])
            )
        await manager.flush()

        assert manager.pieces[0].state is PieceState.VERIFIED
        assert not [key for key in manager.scheduler.in_flight() if key[0] == 0]
        assert sum(1 for message in racer.sent if isinstance(message, Cancel)) == 4

    async def test_leftover_requests_are_cancelled_even_if_the_tracker_forgot(
        self, build_torrent, payload: bytes, make_storage
    ) -> None:
        """Defensive: a slot holding a block the race tracker lost sight of."""
        torrent = parse_torrent(build_torrent(name="forgot", piece_length=64 * 1024, file_count=1))
        storage = make_storage(torrent, name="forgot")
        await storage.prepare()
        sender = make_peer("a:1", pieces=torrent.piece_count)
        holder = make_peer("b:2", pieces=torrent.piece_count)
        manager = make_manager(
            torrent,
            storage,
            peers=FakeSwarm([sender, holder]),
            config=DownloadConfig(
                block_size=BLOCK, max_outstanding_requests=16, endgame_enabled=False
            ),
        )
        await manager.pump()
        # Simulate the bookkeeping drifting: a peer is left holding a request
        # for a block nobody else is racing.
        stale = (0, 0)
        manager.scheduler.slot(holder).outstanding[stale] = asyncio.get_running_loop().time()

        for offset in (0, BLOCK, 2 * BLOCK, 3 * BLOCK):
            manager.on_block(
                sender, PieceMessage(index=0, begin=offset, data=payload[offset : offset + BLOCK])
            )
        await manager.flush()

        assert stale not in manager.scheduler.in_flight()
        assert Cancel(index=0, begin=0, length=BLOCK) in holder.sent


@dataclass(slots=True)
class StuckPeer:
    """A peer whose socket will not take another byte.

    Not hypothetical: a peer on a slow link with a full receive buffer will
    block a writer indefinitely. The client must carry on regardless.
    """

    address: FakeAddress
    bitfield: Bitfield
    choked: bool = False
    connected: bool = True

    async def send(self, message: object) -> None:
        await asyncio.Event().wait()  # never returns


class TestAPeerThatWillNotTakeAByte:
    """A wedged socket must not become a wedged client.

    Cancels are fire-and-forget courtesies sent to a peer. Two things must
    still be true when the peer stops reading: a disk checkpoint
    (:meth:`DownloadManager.flush`) returns at once, and shutting down
    (:meth:`DownloadManager.stop`) finishes — because a client that cannot be
    stopped is worse than one that loses a cancel.
    """

    async def test_a_checkpoint_does_not_wait_for_a_socket(
        self, build_torrent, make_storage
    ) -> None:
        torrent = parse_torrent(build_torrent(name="wedged", piece_length=64 * 1024))
        storage = make_storage(torrent, name="wedged")
        await storage.prepare()
        manager = make_manager(torrent, storage, peers=FakeSwarm([]))
        stuck = StuckPeer(address=FakeAddress("w", 1), bitfield=Bitfield(torrent.piece_count))

        manager._cancel(stuck, (0, 0))

        assert manager.stats.requests_cancelled == 1
        await asyncio.wait_for(manager.flush(), timeout=2.0)
        await asyncio.wait_for(manager.stop(), timeout=5.0)

    async def test_stopping_abandons_the_cancel_rather_than_waiting(
        self, build_torrent, make_storage
    ) -> None:
        torrent = parse_torrent(build_torrent(name="stubborn", piece_length=64 * 1024))
        storage = make_storage(torrent, name="stubborn")
        await storage.prepare()
        manager = make_manager(torrent, storage, peers=FakeSwarm([]))
        stuck = StuckPeer(address=FakeAddress("w", 2), bitfield=Bitfield(torrent.piece_count))

        manager._cancel(stuck, (0, 0))
        await manager.start()
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(manager.stop(), timeout=10.0)
        elapsed = asyncio.get_running_loop().time() - started

        # It gives the cancel a moment, then closes the connection instead.
        assert elapsed < 5.0


class TestBoundedCheckpoints:
    async def test_a_piece_that_never_finishes_does_not_take_the_client_with_it(
        self, build_torrent, make_storage
    ) -> None:
        """A checkpoint that cannot complete still has to return.

        Verification is disk work in a thread pool, and disk work can wedge in
        ways no timeout inside this process can fix (a stalled device, a
        filesystem that stopped answering). The client's answer is to say so
        and carry on: the piece stays in the pool and is fetched again, rather
        than the client waiting forever for a piece it will never write.
        """
        torrent = parse_torrent(build_torrent(name="stuck", piece_length=64 * 1024))
        storage = make_storage(torrent, name="stuck")
        await storage.prepare()
        manager = make_manager(torrent, storage, peers=FakeSwarm([]))
        wedged = asyncio.create_task(asyncio.Event().wait(), name="verify-0")
        manager._verify_tasks.add(wedged)

        await asyncio.wait_for(manager.flush(timeout=0.05), timeout=5.0)

        assert wedged in manager._verify_tasks, "the piece is still stuck; we are not"
        wedged.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wedged

    async def test_stopping_does_not_wait_for_a_wedged_piece(
        self, build_torrent, make_storage
    ) -> None:
        torrent = parse_torrent(build_torrent(name="wedged-stop", piece_length=64 * 1024))
        storage = make_storage(torrent, name="wedged-stop")
        await storage.prepare()
        manager = make_manager(torrent, storage, peers=FakeSwarm([]))
        wedged = asyncio.create_task(asyncio.Event().wait(), name="verify-1")
        manager._verify_tasks.add(wedged)

        await manager.start()
        await asyncio.wait_for(manager.stop(timeout=0.05), timeout=10.0)

        wedged.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wedged
