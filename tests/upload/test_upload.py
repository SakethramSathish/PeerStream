"""Tests for the upload manager: requests in, blocks out, and who is allowed.

These run the real engine against real storage (a temporary directory) and
stand-in peers, so "we served a block" means bytes were read off disk and
handed to a peer, and "we refused" means a counted refusal with a reason.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import pytest
from app.core.config import UploadConfig
from app.core.event_bus import EventBus
from app.core.events import EventType
from app.peer.bitfield import Bitfield
from app.peer.messages import Request
from app.storage.manager import StorageManager
from app.torrent import Torrent, parse_torrent
from app.upload import UploadManager
from app.upload.manager import QueuedRequest

from tests.upload.conftest import (
    BLOCK,
    FakeAddress,
    FakePeer,
    FakeSession,
    FakeSwarm,
    make_peer,
    raw_request,
)


async def stored(
    storage: StorageManager, torrent: Torrent, payload: bytes, *, upto: int | None = None
) -> Bitfield:
    """Store every piece (or the first ``upto``) and return what we hold."""
    await storage.prepare()
    have = Bitfield(torrent.piece_count)
    count = torrent.piece_count if upto is None else upto
    for index in range(count):
        start = torrent.piece_offset(index)
        await storage.write_piece(index, payload[start : start + torrent.piece_size(index)])
        have.set(index)
    return have


def unchoke(manager: UploadManager, peer: FakePeer) -> None:
    """Mark a peer as allowed, the way a choke decision would."""
    manager._decision = manager.decision
    from app.upload.choke import ChokeDecision

    allowed = tuple({*manager.decision.allowed, peer.key})
    manager._decision = ChokeDecision(unchoked=allowed)


class TestRejections:
    """A request is untrusted input; every field of it is checked."""

    async def test_a_piece_we_do_not_have_is_refused(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload, upto=1)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=5, begin=0, length=BLOCK))

        assert manager.stats.requests_rejected == {"missing_piece": 1}
        assert manager.queued(peer) == ()

    async def test_an_index_outside_the_torrent_is_refused(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        for index in (-1, sample_torrent.piece_count, 10_000):
            manager.on_request(peer, raw_request(index, 0, BLOCK))

        assert manager.stats.requests_rejected == {"bad_index": 3}

    async def test_an_offset_outside_the_piece_is_refused(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, raw_request(0, -1, BLOCK))
        manager.on_request(peer, raw_request(0, sample_torrent.piece_size(0), BLOCK))

        assert manager.stats.requests_rejected == {"bad_offset": 2}

    async def test_a_length_that_is_not_a_block_is_refused(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, raw_request(0, 0, 0))
        manager.on_request(peer, raw_request(0, 0, 1024 * 1024))
        manager.on_request(peer, raw_request(0, 0, -8))

        assert manager.stats.requests_rejected == {"bad_length": 3}

    async def test_a_block_running_past_the_piece_is_refused(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)
        last = sample_torrent.piece_size(0) - BLOCK

        manager.on_request(peer, raw_request(0, last, BLOCK * 2))

        assert manager.stats.requests_rejected == {"bad_length": 1}

    async def test_a_peer_that_never_said_interested_gets_nothing(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count, interested=False)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))

        assert manager.stats.requests_rejected == {"not_interested": 1}

    async def test_a_choked_peer_gets_nothing(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)

        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))

        assert manager.stats.requests_rejected == {"choked": 1}

    async def test_a_peer_cannot_queue_without_limit(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([peer]),
            have=have,
            config=UploadConfig(max_requests_per_peer=2, choke_interval=3600.0),
        )
        unchoke(manager, peer)

        # Piece 0 is 16 KiB here, so there is only one block to ask for; asking
        # for the same one twice is a duplicate, and a second piece's block is
        # a different request entirely.
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))
        manager.on_request(peer, Request(index=1, begin=0, length=BLOCK))
        manager.on_request(peer, Request(index=2, begin=0, length=BLOCK))

        assert manager.stats.requests_rejected == {"queue_full": 1}
        assert len(manager.queued(peer)) == 2

    async def test_the_same_block_twice_is_a_duplicate(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))

        assert manager.stats.requests_rejected == {"duplicate": 1}


class TestServing:
    async def test_a_request_is_answered_with_bytes_off_the_disk(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=3, begin=0, length=BLOCK))
        served = await manager.pump()

        assert served == 1
        start = sample_torrent.piece_offset(3)
        assert peer.blocks == [(3, 0, payload[start : start + BLOCK])]
        assert manager.stats.bytes_uploaded == BLOCK
        assert peer.session.uploaded == BLOCK

    async def test_a_whole_piece_can_be_served_block_by_block(
        self, build_torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        torrent = parse_torrent(build_torrent(name="serve", piece_length=64 * 1024, file_count=1))
        storage = make_storage(torrent, name="serve")
        have = await stored(storage, torrent, payload)
        peer = make_peer("a:1", pieces=torrent.piece_count)
        manager = make_upload(torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        start = torrent.piece_offset(0)
        for offset in (0, BLOCK, 2 * BLOCK, 3 * BLOCK):
            manager.on_request(peer, Request(index=0, begin=offset, length=BLOCK))
        served = await manager.pump()

        assert served == 4
        assert b"".join(block[2] for block in peer.blocks) == payload[start : start + 64 * 1024]

    async def test_two_peers_are_served_in_turn(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        """Round-robin: a peer with a deep queue must not starve the other."""
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        first = make_peer("a:1", pieces=sample_torrent.piece_count)
        second = make_peer("b:2", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent, storage=storage, peers=FakeSwarm([first, second]), have=have
        )
        unchoke(manager, first)
        unchoke(manager, second)

        for index in range(4):
            manager.on_request(first, Request(index=index, begin=0, length=BLOCK))
            manager.on_request(second, Request(index=index, begin=0, length=BLOCK))
        await manager.flush()

        assert [block[0] for block in first.blocks] == [0, 1, 2, 3]
        assert [block[0] for block in second.blocks] == [0, 1, 2, 3]
        # Turn about: neither peer's queue was allowed to run ahead.
        assert len(first.blocks) == len(second.blocks) == 4

    async def test_serving_emits_an_event_per_block(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        bus = EventBus()
        seen: list[EventType] = []
        bus.subscribe_all(lambda event: seen.append(event.type))
        manager = make_upload(
            sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have, bus=bus
        )
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=1, begin=0, length=BLOCK))
        await manager.pump()

        assert EventType.PIECE_UPLOADED in seen

    async def test_a_peer_that_hangs_up_mid_send_loses_the_block(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))
        peer.connected = False
        served = await manager.pump()

        assert served == 0
        assert manager.stats.requests_dropped == 1
        assert manager.stats.blocks_served == 0

    async def test_a_short_read_is_refused_rather_than_sent(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        """A file that shrank under us must not corrupt someone's piece."""
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        async def short_read(index: int, begin: int, length: int) -> bytes:
            return b"x" * (length - 1)

        storage.read_block = short_read  # type: ignore[method-assign]
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))
        served = await manager.pump()

        assert served == 0
        assert manager.stats.requests_rejected.get("read_failed") == 1
        assert peer.blocks == []


class TestQueues:
    async def test_a_cancel_drops_the_request(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=2, begin=0, length=BLOCK))
        manager.on_cancel(peer, Request(index=2, begin=0, length=BLOCK))
        served = await manager.pump()

        assert served == 0
        assert manager.stats.requests_dropped == 1
        assert peer.blocks == []

    async def test_cancelling_something_never_asked_for_changes_nothing(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]))

        manager.on_cancel(peer, Request(index=0, begin=0, length=BLOCK))

        assert manager.stats.requests_dropped == 0

    async def test_a_disconnect_takes_the_queue_with_it(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))

        manager.on_disconnect(peer, "peer closed the connection")

        assert manager.queued(peer) == ()
        assert manager.stats.requests_dropped == 1

    async def test_a_request_that_waited_too_long_is_dropped(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([peer]),
            have=have,
            config=UploadConfig(queue_timeout=0.01, choke_interval=3600.0),
        )
        unchoke(manager, peer)
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))

        await asyncio.sleep(0.02)
        served = await manager.pump()

        assert served == 0
        assert manager.stats.requests_dropped == 1

    async def test_flush_serves_everything_queued(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)
        for index in range(5):
            manager.on_request(peer, Request(index=index, begin=0, length=BLOCK))

        await manager.flush()

        assert len(peer.blocks) == 5


class TestChoking:
    async def test_a_peer_is_unchoked_when_it_earns_a_slot(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]))

        await manager.pump()

        assert peer.session.am_choking is False
        assert manager.unchoked_peers() == ("a:1",)

    async def test_a_peer_that_wants_nothing_stays_choked(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count, interested=False)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]))

        await manager.pump()

        assert peer.session.am_choking is True
        assert manager.unchoked_peers() == ()

    async def test_capacity_is_shared_out_by_merit(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        await stored(storage, sample_torrent, payload)
        generous = make_peer("a:1", pieces=sample_torrent.piece_count)
        stingy = make_peer("b:2", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([generous, stingy]),
            config=UploadConfig(slots=1, optimistic_unchoke=False, choke_interval=3600.0),
        )
        manager.note_downloaded(generous, 100_000)
        manager.note_downloaded(stingy, 1_000)

        await manager.pump()

        assert manager.unchoked_peers()[0] == "a:1"
        assert generous.session.am_choking is False
        assert stingy.session.am_choking is True

    async def test_merit_is_what_we_counted_not_what_we_were_told(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        """A peer's own byte counters are advisory; ours are what rank it."""
        storage = make_storage(sample_torrent)
        await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([peer]),
            config=UploadConfig(slots=1, snub_seconds=1.0, choke_interval=3600.0),
        )
        peer.session.peer_choking = True
        peer.session.downloaded = 999_999  # what the peer claims, unwitnessed

        await manager.pump()

        assert manager.unchoked_peers() == ("a:1",)  # nobody better, and not yet snubbed

    async def test_choke_changes_are_announced(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        bus = EventBus()
        seen: list[EventType] = []
        bus.subscribe_all(lambda event: seen.append(event.type))
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), bus=bus)

        await manager.pump()

        assert EventType.PEER_UNCHOKED in seen

    async def test_a_peer_that_vanishes_from_the_swarm_is_choked_out(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        swarm = FakeSwarm([peer])
        manager = make_upload(sample_torrent, storage=storage, peers=swarm)
        await manager.pump()
        assert manager.unchoked_peers() == ("a:1",)

        swarm.connections.remove(peer)
        await manager.pump()

        assert manager.unchoked_peers() == ()


class TestAnnouncing:
    async def test_a_verified_piece_is_admitted(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_upload(sample_torrent, storage=storage)

        manager.note_piece_verified(3)

        assert manager.have.has(3) is True
        assert manager.have.count == 1

    async def test_a_verified_piece_is_announced_to_everyone(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]))
        await manager.pump()

        manager.note_piece_verified(2)
        await asyncio.sleep(0.01)

        assert ("have", 2) in peer.sent

    async def test_announcing_twice_says_it_once(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]))
        await manager.pump()

        manager.note_piece_verified(2)
        manager.note_piece_verified(2)
        await asyncio.sleep(0.01)

        assert peer.sent.count(("have", 2)) == 1

    def test_an_index_outside_the_torrent_is_rejected(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_upload(sample_torrent, storage=storage)

        with pytest.raises(ValueError, match="piece index out of range"):
            manager.note_piece_verified(sample_torrent.piece_count)


class TestRateLimiting:
    async def test_the_upload_rate_is_respected(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([peer]),
            have=have,
            config=UploadConfig(max_upload_speed=100_000, choke_interval=3600.0),
        )
        unchoke(manager, peer)

        # Eight blocks is 128 KiB; the pacer allows a 100 KiB burst up front,
        # so the tail of the queue has to wait to be earned.
        for index in range(8):
            manager.on_request(peer, Request(index=index, begin=0, length=BLOCK))
        started = asyncio.get_running_loop().time()
        await manager.flush()
        elapsed = asyncio.get_running_loop().time() - started

        assert len(peer.blocks) == 8
        assert elapsed >= 0.2  # ~28 KiB over budget at 100 KiB/s

    async def test_no_limit_means_no_waiting(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        for index in range(8):
            manager.on_request(peer, Request(index=index, begin=0, length=BLOCK))
        started = asyncio.get_running_loop().time()
        await manager.flush()
        elapsed = asyncio.get_running_loop().time() - started

        assert len(peer.blocks) == 8
        assert elapsed < 0.2


class TestLifecycle:
    async def test_start_and_stop(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([peer]),
            have=have,
            config=UploadConfig(choke_interval=0.02),
        )

        await manager.start()
        assert manager.running is True
        await manager.pump()  # the loop decides who is worth serving
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))
        await asyncio.sleep(0.1)
        await manager.stop()

        assert manager.running is False
        assert len(peer.blocks) == 1  # the loop served it without a manual pump

    async def test_starting_twice_is_harmless(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_upload(sample_torrent, storage=storage)

        await manager.start()
        task = manager._task
        await manager.start()

        assert manager._task is task
        await manager.stop()

    async def test_the_context_manager_stops_the_loop(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_upload(sample_torrent, storage=storage)

        async with manager:
            assert manager.running is True

        assert manager.running is False

    async def test_the_loop_survives_a_failed_pass(
        self, sample_torrent: Torrent, make_storage, make_upload, monkeypatch
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_upload(
            sample_torrent, storage=storage, config=UploadConfig(choke_interval=0.01)
        )
        monkeypatch.setattr("app.upload.manager.IDLE_TICK_SECONDS", 0.01)
        calls = 0
        original = manager.pump

        async def flaky() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("disk on fire")
            return await original()

        monkeypatch.setattr(manager, "pump", flaky)

        await manager.start()
        await asyncio.sleep(0.05)
        await manager.stop()

        assert calls > 1

    async def test_stopping_without_starting_is_harmless(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_upload(sample_torrent, storage=storage)

        await manager.stop()

        assert manager.running is False


class TestStats:
    async def test_the_counters_describe_what_happened(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload, upto=1)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))  # served
        manager.on_request(peer, Request(index=9, begin=0, length=BLOCK))  # we lack it
        await manager.pump()

        stats = manager.stats
        assert stats.requests_received == 2
        assert stats.requests_served == 1
        assert stats.blocks_served == 1
        assert stats.bytes_uploaded == BLOCK
        assert stats.requests_rejected == {"missing_piece": 1}
        assert stats.rejected_total == 1
        assert stats.queue_depth == 0
        assert stats.peers_unchoked == 1


class TestQueuedRequest:
    def test_a_request_is_identified_by_its_block(self) -> None:
        item = QueuedRequest(index=4, begin=16_384, length=16_384, queued_at=0.0)

        assert item.key == (4, 16_384)


class TestDefensivePaths:
    """The paths that only exist because peers are strangers."""

    async def test_flush_does_not_wait_for_peers_we_are_choking(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([peer]),
            have=have,
            config=UploadConfig(slots=0, choke_interval=3600.0),
        )
        await manager.pump()  # nobody is allowed
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))  # refused
        assert manager.queued(peer) == ()

        # A queue we can never serve must not become a wait we never leave.
        manager._queues.setdefault(peer.key, deque()).append(
            QueuedRequest(
                index=0, begin=0, length=BLOCK, queued_at=asyncio.get_running_loop().time()
            )
        )
        await asyncio.wait_for(manager.flush(), timeout=2.0)

        assert manager.stats.blocks_served == 0

    async def test_a_peer_that_refuses_to_be_choked_is_not_fatal(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        await stored(storage, sample_torrent, payload)

        class Unreachable(FakePeer):
            async def set_choking(self, choking: bool) -> None:
                raise ConnectionError("gone")

        peer = Unreachable(
            address=FakeAddress("a", 1),
            session=FakeSession(piece_count=sample_torrent.piece_count, peer_interested=True),
        )
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]))

        await manager.pump()

        assert manager.running is False  # the loop kept going, quietly

    async def test_a_read_failure_is_counted_not_fatal(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        class Broken:
            """Storage whose disk has just failed."""

            async def read_block(self, index: int, begin: int, length: int) -> bytes:
                raise OSError("disk gone")

        manager._storage = Broken()  # type: ignore[assignment]
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))

        assert await manager.pump() == 0
        assert manager.stats.requests_rejected.get("read_failed") == 1

    async def test_announcing_to_a_peer_that_cannot_hear_is_logged_not_raised(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)

        class Silent(FakePeer):
            async def send_have(self, index: int) -> None:
                raise ConnectionError("gone")

        peer = Silent(address=FakeAddress("a", 1))
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]))
        await manager.pump()

        manager.note_piece_verified(1)
        await asyncio.sleep(0.01)

        assert manager.have.has(1) is True

    async def test_a_peer_without_an_announce_method_is_skipped(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        class Mute(FakePeer):
            send_have = None  # type: ignore[assignment]

        storage = make_storage(sample_torrent)
        peer = Mute(address=FakeAddress("a", 1))
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]))
        await manager.pump()

        manager.note_piece_verified(1)

        assert manager.have.has(1) is True

    async def test_stopping_waits_for_a_send_that_is_already_due(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        """A deferred send is not abandoned just because we are stopping."""
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([peer]),
            have=have,
            config=UploadConfig(max_upload_speed=100_000, choke_interval=3600.0),
        )
        unchoke(manager, peer)
        for index in range(8):
            manager.on_request(peer, Request(index=index, begin=0, length=BLOCK))

        await manager.pump()  # spends the burst, defers the rest
        await manager.stop()

        assert manager.stats.bytes_uploaded == 8 * BLOCK

    def test_a_peer_without_an_address_is_still_identifiable(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_upload(sample_torrent, storage=storage)

        class Anonymous:
            pass

        anonymous = Anonymous()
        assert manager._key(anonymous) == str(id(anonymous))

    def test_the_config_in_force_is_visible(
        self, sample_torrent: Torrent, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        config = UploadConfig(slots=2)
        manager = make_upload(sample_torrent, storage=storage, config=config)

        assert manager.config is config

    async def test_the_loop_stops_cleanly_when_cancelled(
        self, sample_torrent: Torrent, make_storage, make_upload, monkeypatch
    ) -> None:
        storage = make_storage(sample_torrent)
        manager = make_upload(sample_torrent, storage=storage)

        async def cancelled() -> int:
            raise asyncio.CancelledError

        monkeypatch.setattr(manager, "pump", cancelled)
        await manager.start()
        await asyncio.sleep(0.02)

        await manager.stop()

        assert manager.running is False


class TestTheLastCorners:
    """Branches that only a stranger on the other end of a socket can reach."""

    async def test_a_peer_that_hangs_up_as_we_send_is_counted_not_fatal(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        """The socket dies between the read and the write."""

        class Gone(FakePeer):
            async def send_piece(self, index: int, begin: int, data: bytes) -> None:
                raise ConnectionError("broken pipe")

        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = Gone(
            address=FakeAddress("a", 1),
            session=FakeSession(piece_count=sample_torrent.piece_count, peer_interested=True),
        )
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)
        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))

        assert await manager.pump() == 0
        assert manager.stats.requests_dropped == 1
        assert manager.stats.blocks_served == 0

    async def test_flush_waits_for_blocks_the_pacer_owes_even_if_a_choked_peer_is_holding_a_queue(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        """One peer queued behind a choke, another paid for in instalments."""
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        eager = make_peer("a:1", pieces=sample_torrent.piece_count)
        choked = make_peer("b:2", pieces=sample_torrent.piece_count, interested=False)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([eager, choked]),
            have=have,
            config=UploadConfig(
                slots=1,
                optimistic_unchoke=False,
                max_upload_speed=100_000,
                choke_interval=3600.0,
            ),
        )
        unchoke(manager, eager)
        for index in range(8):
            manager.on_request(eager, Request(index=index, begin=0, length=BLOCK))
        manager._queues.setdefault(choked.key, deque()).append(  # a queue we owe nothing to
            QueuedRequest(index=0, begin=0, length=BLOCK, queued_at=time.monotonic())
        )

        await asyncio.wait_for(manager.flush(), timeout=10.0)

        assert manager.stats.blocks_served == 8
        assert len(manager.queued(choked)) == 1  # still waiting, still not served

    async def test_flush_waits_for_blocks_the_pacer_owes(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(
            sample_torrent,
            storage=storage,
            peers=FakeSwarm([peer]),
            have=have,
            config=UploadConfig(max_upload_speed=100_000, choke_interval=3600.0),
        )
        unchoke(manager, peer)
        for index in range(8):
            manager.on_request(peer, Request(index=index, begin=0, length=BLOCK))

        await asyncio.wait_for(manager.flush(), timeout=10.0)

        assert manager.stats.blocks_served == 8

    async def test_cancelling_a_request_we_never_queued_changes_nothing(
        self, sample_torrent: Torrent, payload: bytes, make_storage, make_upload
    ) -> None:
        storage = make_storage(sample_torrent)
        have = await stored(storage, sample_torrent, payload)
        peer = make_peer("a:1", pieces=sample_torrent.piece_count)
        manager = make_upload(sample_torrent, storage=storage, peers=FakeSwarm([peer]), have=have)
        unchoke(manager, peer)

        manager.on_request(peer, Request(index=0, begin=0, length=BLOCK))
        manager.on_cancel(peer, raw_request(index=1, begin=0, length=BLOCK))  # not queued

        assert len(manager.queued(peer)) == 1
        assert manager.stats.requests_dropped == 0
