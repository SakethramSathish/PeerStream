"""Tests for request scheduling (FR-06, TRD §23).

The scheduler decides *who* fetches *which* block. It sends nothing itself, so
every rule here is checked by looking at the list of assignments it returns.
"""

from __future__ import annotations

import time

from app.core.config import DownloadConfig
from app.download.block import BlockKey
from app.download.piece import Piece, PieceState, build_pieces
from app.download.scheduler import PeerSlot, Scheduler, peer_key
from app.peer.bitfield import Bitfield, PieceAvailability
from app.peer.messages import Request

from tests.download.conftest import FakeAddress, FakePeer, make_peer

HASH = bytes(range(20))
PIECE_SIZE = 4 * 16_384  # four 16 KiB blocks


def make_pieces(count: int = 4, *, block_size: int = 16_384) -> tuple[Piece, ...]:
    return build_pieces(sizes=[PIECE_SIZE] * count, hashes=[HASH] * count, block_size=block_size)


def make_scheduler(
    pieces: tuple[Piece, ...] | None = None,
    *,
    config: DownloadConfig | None = None,
    **kwargs: object,
) -> Scheduler:
    from app.download.selector import PieceSelector

    pieces = pieces if pieces is not None else make_pieces()
    settings = config or DownloadConfig(max_outstanding_requests=4, block_size=16_384)
    selector = kwargs.pop("selector", None) or PieceSelector(
        len(pieces), strategy=settings.piece_strategy
    )
    return Scheduler(pieces, config=settings, selector=selector, **kwargs)  # type: ignore[arg-type]


class TestPeerSlot:
    def test_capacity(self) -> None:
        slot = PeerSlot(key="a:1")

        assert slot.can_request(2) is True
        slot.add((0, 0), time.monotonic())
        assert slot.can_request(1) is False
        assert len(slot) == 1

    def test_the_same_block_cannot_be_added_twice(self) -> None:
        slot = PeerSlot(key="a:1")

        assert slot.add((0, 0), 0.0) is True
        assert slot.add((0, 0), 1.0) is False

    def test_discard_and_clear(self) -> None:
        slot = PeerSlot(key="a:1")
        slot.add((0, 0), 0.0)
        slot.add((0, 16_384), 0.0)

        assert slot.discard((0, 0)) is True
        assert slot.discard((0, 0)) is False
        assert slot.clear() == ((0, 16_384),)
        assert len(slot) == 0

    def test_expired_requests(self) -> None:
        now = 100.0
        slot = PeerSlot(key="a:1")
        slot.add((0, 0), now - 31)
        slot.add((0, 16_384), now - 1)

        assert slot.expired(30.0, now) == ((0, 0),)


class TestIdentity:
    def test_an_address_becomes_a_stable_key(self) -> None:
        peer = FakePeer(address=FakeAddress("10.0.0.1", 6881), bitfield=Bitfield(4))

        assert peer_key(peer) == "10.0.0.1:6881"

    def test_an_object_without_an_address_still_gets_a_key(self) -> None:
        class Bare:
            pass

        assert peer_key(Bare()).isdigit()  # id()-based, so it at least exists


class TestPlanning:
    def test_a_peer_gets_a_full_pipeline(self) -> None:
        scheduler = make_scheduler(config=DownloadConfig(max_outstanding_requests=3))
        peer = make_peer("a:1", pieces=4)

        assignments = scheduler.plan([peer])

        assert len(assignments) == 3
        assert all(isinstance(item.request, Request) for item in assignments)
        assert len({item.request.begin for item in assignments}) == 3  # no repeats

    def test_requests_stay_inside_one_piece_until_it_is_full(self) -> None:
        scheduler = make_scheduler(config=DownloadConfig(max_outstanding_requests=4))
        peer = make_peer("a:1", pieces=4)

        assignments = scheduler.plan([peer])

        assert {item.request.index for item in assignments} == {0}

    def test_planning_twice_does_not_repeat_blocks(self) -> None:
        scheduler = make_scheduler(config=DownloadConfig(max_outstanding_requests=2))
        peer = make_peer("a:1", pieces=4)

        first = scheduler.plan([peer])
        second = scheduler.plan([peer])

        begins = [item.request.begin for item in first] + [item.request.begin for item in second]
        assert len(begins) == len(set(begins))

    def test_a_peer_is_only_asked_for_what_it_has(self) -> None:
        scheduler = make_scheduler()
        peer = make_peer("a:1", pieces=4, holding=[2])

        assignments = scheduler.plan([peer])

        assert {item.request.index for item in assignments} == {2}

    def test_a_choked_peer_is_skipped(self) -> None:
        scheduler = make_scheduler()
        peer = make_peer("a:1", pieces=4)
        peer.choked = True

        assert scheduler.plan([peer]) == []

    def test_a_disconnected_peer_is_skipped(self) -> None:
        scheduler = make_scheduler()
        peer = make_peer("a:1", pieces=4)
        peer.connected = False

        assert scheduler.plan([peer]) == []

    def test_two_peers_do_not_share_a_block(self) -> None:
        scheduler = make_scheduler(
            config=DownloadConfig(max_outstanding_requests=4, endgame_enabled=False)
        )
        peers = [make_peer("a:1", pieces=4), make_peer("b:2", pieces=4)]

        assignments = scheduler.plan(peers)

        keys = [(item.request.index, item.request.begin) for item in assignments]
        assert len(keys) == len(set(keys))

    def test_work_spreads_to_the_emptiest_pipeline_first(self) -> None:
        scheduler = make_scheduler(
            config=DownloadConfig(max_outstanding_requests=2, endgame_enabled=False)
        )
        busy = make_peer("a:1", pieces=4)
        idle = make_peer("b:2", pieces=4)
        # busy already has a block in flight, recorded the way plan() does.
        scheduler.pieces[0].mark_requested(0, "a:1")
        scheduler.slot(busy).add((0, 0), time.monotonic())

        assignments = scheduler.plan([busy, idle])

        # The busy peer only gets one more; the idle one gets a full pipeline.
        assert sum(1 for item in assignments if item.peer is busy) == 1
        assert sum(1 for item in assignments if item.peer is idle) == 2

    def test_a_piece_in_progress_is_finished_before_a_new_one(self) -> None:
        pieces = make_pieces(4)
        scheduler = make_scheduler(pieces, config=DownloadConfig(max_outstanding_requests=1))
        peer = make_peer("a:1", pieces=4)
        # Piece 2 already has a block; it must be finished before piece 0.
        pieces[2].add_block(0, b"x" * 16_384, source="a:1")

        assignments = scheduler.plan([peer])

        assert {item.request.index for item in assignments} == {2}
        assert assignments[0].request.begin == 16_384

    def test_a_verified_piece_is_never_requested_again(self) -> None:
        pieces = make_pieces(2)
        pieces[0].mark_verified()
        scheduler = make_scheduler(pieces)
        peer = make_peer("a:1", pieces=2)

        assignments = scheduler.plan([peer])

        assert {item.request.index for item in assignments} == {1}

    def test_availability_decides_which_new_piece_comes_first(self) -> None:
        pieces = make_pieces(3)
        scheduler = make_scheduler(pieces, config=DownloadConfig(max_outstanding_requests=1))
        availability = PieceAvailability(3)
        availability.add_piece(0)
        availability.add_piece(0)
        availability.add_piece(1)  # piece 1 is the rarest that is still held
        # Piece 2 is held by nobody, so it is not an option for this peer.
        peer = make_peer("a:1", pieces=3, holding=[0, 1])

        assignments = scheduler.plan([peer], availability=availability)

        assert assignments[0].request.index == 1

    def test_nothing_left_to_do_returns_nothing(self) -> None:
        pieces = make_pieces(1)
        for block in pieces[0].blocks:
            pieces[0].add_block(block.offset, b"x" * block.length)
        pieces[0].mark_verified()
        scheduler = make_scheduler(pieces)

        assert scheduler.plan([make_peer("a:1", pieces=1)]) == []


class TestEndgameScheduling:
    def test_the_last_blocks_are_raced(self) -> None:
        pieces = make_pieces(1)
        for block in pieces[0].blocks[:-1]:
            pieces[0].add_block(block.offset, b"x" * block.length)
        scheduler = make_scheduler(
            pieces,
            config=DownloadConfig(
                max_outstanding_requests=4,
                endgame_threshold=10,
                endgame_enabled=True,
                endgame_delay=0.0,
            ),
        )
        peers = [make_peer("a:1", pieces=1), make_peer("b:2", pieces=1)]

        assignments = scheduler.plan(peers)

        last_block_keys = [
            (item.request.index, item.request.begin)
            for item in assignments
            if item.request.begin == 49_152
        ]
        assert len(last_block_keys) == 2  # both peers are racing for it
        assert scheduler.endgame.duplicates == 1

    def test_racing_stops_when_there_is_plenty_left(self) -> None:
        scheduler = make_scheduler(
            config=DownloadConfig(max_outstanding_requests=4, endgame_threshold=0)
        )
        peers = [make_peer("a:1", pieces=4), make_peer("b:2", pieces=4)]

        scheduler.plan(peers)

        assert scheduler.endgame.duplicates == 0

    def test_endgame_can_be_disabled(self) -> None:
        pieces = make_pieces(1)
        for block in pieces[0].blocks[:-1]:
            pieces[0].add_block(block.offset, b"x" * block.length)
        scheduler = make_scheduler(
            pieces,
            config=DownloadConfig(
                max_outstanding_requests=4, endgame_enabled=False, endgame_threshold=10
            ),
        )
        peers = [make_peer("a:1", pieces=1), make_peer("b:2", pieces=1)]

        assignments = scheduler.plan(peers)

        last_block = [item for item in assignments if item.request.begin == 49_152]
        assert len(last_block) == 1


class TestBookkeeping:
    def test_a_received_block_frees_the_winner(self) -> None:
        scheduler = make_scheduler(config=DownloadConfig(max_outstanding_requests=1))
        peer = make_peer("a:1", pieces=4)
        (assignment,) = scheduler.plan([peer])
        key: BlockKey = (assignment.request.index, assignment.request.begin)

        assert scheduler.slot(peer).outstanding
        assert scheduler.note_received(peer, key) == ()
        assert not scheduler.slot(peer).outstanding

    def test_a_received_block_tells_us_who_to_cancel(self) -> None:
        pieces = make_pieces(1)
        # Only the final block is left, so endgame is racing it.
        for block in pieces[0].blocks[:-1]:
            pieces[0].add_block(block.offset, b"x" * block.length)
        scheduler = make_scheduler(
            pieces,
            config=DownloadConfig(
                max_outstanding_requests=4,
                endgame_threshold=10,
                endgame_enabled=True,
                endgame_delay=0.0,
            ),
        )
        winner = make_peer("a:1", pieces=1)
        loser = make_peer("b:2", pieces=1)
        scheduler.plan([winner, loser])
        key: BlockKey = (0, 49_152)

        assert scheduler.note_received(winner, key) == (loser,)
        assert not scheduler.slot(loser).outstanding

    def test_losing_a_peer_returns_its_blocks(self) -> None:
        scheduler = make_scheduler(config=DownloadConfig(max_outstanding_requests=2))
        peer = make_peer("a:1", pieces=4)
        planned = scheduler.plan([peer])
        pieces = scheduler.pieces

        keys = scheduler.drop_peer(peer)

        assert len(planned) == 2

        assert len(keys) == 2
        for index, begin in keys:
            assert pieces[index].requesters(begin) == ()
        assert scheduler.peer_for("a:1") is None

    def test_an_unanswered_request_goes_back_in_the_pool(self) -> None:
        scheduler = make_scheduler(
            config=DownloadConfig(max_outstanding_requests=2, block_timeout=1.0)
        )
        peer = make_peer("a:1", pieces=4)
        scheduler.plan([peer])
        for key in scheduler.slot(peer).outstanding:
            scheduler.slot(peer).outstanding[key] -= 10  # pretend it was long ago

        expired = scheduler.expire()

        assert [len(keys) for keys in expired.values()] == [2]
        piece = scheduler.pieces[0]
        assert piece.outstanding_requests() == ()

    def test_expire_touches_nothing_when_requests_are_fresh(self) -> None:
        scheduler = make_scheduler(config=DownloadConfig(max_outstanding_requests=2))
        scheduler.plan([make_peer("a:1", pieces=4)])

        assert scheduler.expire() == {}

    def test_expired_requests_are_reported_per_peer(self) -> None:
        scheduler = make_scheduler(
            config=DownloadConfig(max_outstanding_requests=2, block_timeout=1.0)
        )
        scheduler.plan([make_peer("a:1", pieces=4)])
        for key in scheduler.slot(scheduler.peer_for("a:1")).outstanding:
            scheduler.slot(scheduler.peer_for("a:1")).outstanding[key] -= 10

        assert list(scheduler.expire()) == ["a:1"]

    def test_block_counts_describe_the_work_left(self) -> None:
        pieces = make_pieces(4)
        scheduler = make_scheduler(
            pieces,
            config=DownloadConfig(max_outstanding_requests=2, endgame_enabled=False),
        )

        assert scheduler.outstanding_blocks() == 0  # nothing started yet
        assert scheduler.unclaimed_blocks() == 16  # every block is up for grabs

        scheduler.plan([make_peer("a:1", pieces=4)])

        assert scheduler.outstanding_blocks() == 4  # one piece in progress
        assert scheduler.unclaimed_blocks() == 14  # two blocks are claimed

    def test_unclaimed_blocks_recover_when_a_peer_leaves(self) -> None:
        scheduler = make_scheduler(
            config=DownloadConfig(max_outstanding_requests=2, endgame_enabled=False)
        )
        peer = make_peer("a:1", pieces=4)
        scheduler.plan([peer])

        assert scheduler.unclaimed_blocks() == 14
        scheduler.drop_peer(peer)
        assert scheduler.unclaimed_blocks() == 16

    def test_a_reset_piece_is_recounted(self) -> None:
        pieces = make_pieces(2)
        scheduler = make_scheduler(pieces, config=DownloadConfig(max_outstanding_requests=2))
        pieces[0].mark_requested(0, "a:1")

        assert scheduler.unclaimed_blocks() == 8 - 1
        pieces[0].reset()
        scheduler.note_piece_reset(pieces[0])
        assert scheduler.unclaimed_blocks() == 8

    def test_wanted_marks_the_gaps(self) -> None:
        pieces = make_pieces(3)
        pieces[1].mark_verified()
        scheduler = make_scheduler(pieces)

        wanted = scheduler.wanted()

        assert wanted.has(0) and not wanted.has(1) and wanted.has(2)

    def test_register_and_forget(self) -> None:
        scheduler = make_scheduler()
        peer = make_peer("a:1", pieces=4)

        scheduler.register(peer)
        assert scheduler.peer_for("a:1") is peer

        scheduler.forget(peer)
        assert scheduler.peer_for("a:1") is None

    def test_in_flight_lists_every_outstanding_block(self) -> None:
        scheduler = make_scheduler(config=DownloadConfig(max_outstanding_requests=2))
        scheduler.plan([make_peer("a:1", pieces=4), make_peer("b:2", pieces=4)])

        assert len(scheduler.in_flight()) == 4

    def test_properties_are_exposed(self) -> None:
        pieces = make_pieces(2)
        scheduler = make_scheduler(pieces)

        assert scheduler.pieces == {0: pieces[0], 1: pieces[1]}
        assert scheduler.selector.piece_count == 2
        assert scheduler.endgame.enabled is True
        assert scheduler.config.block_size == 16_384

    def test_a_piece_being_verified_is_not_offered(self) -> None:
        pieces = make_pieces(2)
        pieces[0].mark_verifying()
        scheduler = make_scheduler(pieces)

        assert pieces[0].state is PieceState.VERIFYING
        assert not scheduler.wanted().has(0)


class TestDefensivePaths:
    def test_a_block_already_pending_for_a_peer_stops_the_planning(self) -> None:
        """If the slot and the piece disagree, planning stops rather than looping."""
        scheduler = make_scheduler(config=DownloadConfig(max_outstanding_requests=4))
        peer = make_peer("a:1", pieces=4)
        # The slot holds a block the piece still thinks is unclaimed.
        scheduler.slot(peer).add((0, 0), time.monotonic())

        assert scheduler.plan([peer]) == []


class TestEndgamePatience:
    """Endgame races stale blocks, not fresh ones (TRD §21)."""

    def test_a_fresh_request_is_not_raced(self) -> None:
        pieces = make_pieces(1)
        for block in pieces[0].blocks[:-1]:
            pieces[0].add_block(block.offset, b"x" * block.length)
        scheduler = make_scheduler(
            pieces,
            config=DownloadConfig(
                max_outstanding_requests=4, endgame_threshold=10, endgame_delay=30.0
            ),
        )
        peers = [make_peer("a:1", pieces=1), make_peer("b:2", pieces=1)]

        assignments = scheduler.plan(peers)

        assert len(assignments) == 1  # the second peer waits its turn
        assert scheduler.endgame.duplicates == 0

    def test_a_request_that_has_hung_long_enough_is_raced(self) -> None:
        pieces = make_pieces(1)
        for block in pieces[0].blocks[:-1]:
            pieces[0].add_block(block.offset, b"x" * block.length)
        scheduler = make_scheduler(
            pieces,
            config=DownloadConfig(
                max_outstanding_requests=4, endgame_threshold=10, endgame_delay=1.0
            ),
        )
        winner = make_peer("a:1", pieces=1)
        loser = make_peer("b:2", pieces=1)
        scheduler.plan([winner])
        # The first peer has had its request for ages and not answered.
        for key in list(scheduler.slot(winner).outstanding):
            scheduler.slot(winner).outstanding[key] = time.monotonic() - 5.0

        assignments = scheduler.plan([loser])

        assert len(assignments) == 1
        assert scheduler.endgame.duplicates == 1

    def test_a_peer_that_has_asked_is_remembered_until_the_block_arrives(self) -> None:
        pieces = make_pieces(2)
        scheduler = make_scheduler(pieces, config=DownloadConfig(max_outstanding_requests=2))
        peer = make_peer("a:1", pieces=2)

        scheduler.plan([peer])
        key = next(iter(scheduler.slot(peer).outstanding))

        assert scheduler.has_asked(peer, key) is True

        scheduler.note_received(peer, key)

        assert scheduler.has_asked(peer, key) is False
        assert scheduler.has_asked(make_peer("b:2", pieces=2), key) is False

    def test_forgetting_a_peer_forgets_what_it_was_asked(self) -> None:
        pieces = make_pieces(2)
        scheduler = make_scheduler(pieces, config=DownloadConfig(max_outstanding_requests=2))
        peer = make_peer("a:1", pieces=2)
        scheduler.plan([peer])
        key = next(iter(scheduler.slot(peer).outstanding))

        scheduler.forget(peer)

        assert scheduler.has_asked(peer, key) is False

    def test_a_finished_piece_stops_being_asked_for(self) -> None:
        """Completing a piece cancels the races that are still outstanding."""
        pieces = make_pieces(2)
        scheduler = make_scheduler(
            pieces,
            config=DownloadConfig(
                max_outstanding_requests=4,
                endgame_threshold=10,
                endgame_enabled=True,
                endgame_delay=0.0,
            ),
        )
        first = make_peer("a:1", pieces=2)
        second = make_peer("b:2", pieces=2)
        scheduler.plan([first, second])

        cancelled = scheduler.cancel_piece(pieces[0])

        assert cancelled
        assert all(key[0] == 0 for _, key in cancelled)
        assert not scheduler.slot(first).outstanding
        assert not scheduler.slot(second).outstanding

    def test_history_of_finished_pieces_is_pruned(self) -> None:
        """Requests are remembered only while their piece is still in play."""
        pieces = make_pieces(2)
        scheduler = make_scheduler(pieces, config=DownloadConfig(max_outstanding_requests=1))
        peer = make_peer("a:1", pieces=2)
        scheduler.plan([peer])
        key = (0, 0)
        assert scheduler.has_asked(peer, key) is True

        for block in pieces[0].blocks:
            pieces[0].add_block(block.offset, b"x" * block.length)
        pieces[0].mark_verifying()
        pieces[0].mark_verified()

        scheduler.plan([peer])  # planning is when the history is tidied

        assert scheduler.has_asked(peer, key) is False


class TestStaleness:
    def test_a_block_nobody_has_asked_for_cannot_be_stale(self) -> None:
        scheduler = make_scheduler(make_pieces(1), config=DownloadConfig(endgame_delay=60.0))

        assert scheduler._stale((0, 0), time.monotonic()) is True
