"""Tests for the piece state machine and block bookkeeping (TRD §21).

The piece is where integrity lives, so the behaviours that matter are the ones
about *losing* data honestly: a duplicate block is dropped, a failed piece is
reset completely, and a block whose requester vanished goes back in the pool.
"""

from __future__ import annotations

import pytest
from app.core.constants import DEFAULT_BLOCK_SIZE
from app.download.block import BlockState
from app.download.piece import Piece, PieceState, build_pieces

HASH = bytes(range(20))
PIECE_SIZE = 4 * DEFAULT_BLOCK_SIZE  # four blocks


def make_piece(**kwargs: object) -> Piece:
    fields: dict[str, object] = {
        "index": 0,
        "size": PIECE_SIZE,
        "expected_hash": HASH,
        "block_size": DEFAULT_BLOCK_SIZE,
    }
    fields.update(kwargs)
    return Piece(**fields)  # type: ignore[arg-type]


class TestConstruction:
    def test_blocks_cover_the_piece(self) -> None:
        piece = make_piece()

        assert piece.block_count == 4
        assert [block.offset for block in piece.blocks] == [0, 16_384, 32_768, 49_152]
        assert piece.state is PieceState.MISSING
        assert piece.progress == 0.0

    def test_a_short_last_block(self) -> None:
        piece = Piece(index=0, size=40_000, expected_hash=HASH, block_size=DEFAULT_BLOCK_SIZE)

        assert piece.blocks[-1].length == 40_000 - 32_768

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"index": -1}, "piece index must not be negative"),
            ({"size": -1}, "piece size must not be negative"),
            ({"expected_hash": b"short"}, "piece hash must be 20 bytes"),
        ],
    )
    def test_nonsense_is_refused(self, kwargs: dict[str, object], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            make_piece(**kwargs)


class TestRequesting:
    def test_a_peer_gets_each_block_once(self) -> None:
        piece = make_piece()

        first = piece.requestable_block("a:1")
        assert first is not None
        piece.mark_requested(first.offset, "a:1")

        second = piece.requestable_block("a:1")
        assert second is not None and second.offset != first.offset
        piece.mark_requested(second.offset, "a:1")

        assert piece.requested_blocks == 2

    def test_a_second_peer_does_not_get_the_same_block_by_default(self) -> None:
        piece = make_piece()
        block = piece.requestable_block("a:1")
        assert block is not None
        piece.mark_requested(block.offset, "a:1")

        assert piece.requestable_block("b:2") != block
        assert piece.requestable_block("b:2", allow_duplicates=False) != block

    def test_endgame_lets_a_second_peer_race_for_it(self) -> None:
        piece = make_piece()
        block = piece.requestable_block("a:1")
        assert block is not None
        piece.mark_requested(block.offset, "a:1")

        assert piece.requestable_block("b:2", allow_duplicates=True) == block

    def test_received_blocks_are_never_offered_again(self) -> None:
        piece = make_piece()
        piece.add_block(0, b"x" * DEFAULT_BLOCK_SIZE, source="a:1")

        assert all(block.offset != 0 for block in [piece.requestable_block("b:2")])  # type: ignore[union-attr]

    def test_a_finished_piece_offers_nothing(self) -> None:
        piece = make_piece()
        for block in piece.blocks:
            piece.add_block(block.offset, b"x" * block.length, source="a:1")

        assert piece.state is PieceState.DOWNLOADED
        assert piece.requestable_block("a:1") is None

    def test_a_verifying_piece_offers_nothing(self) -> None:
        piece = make_piece()
        piece.mark_verifying()

        assert piece.requestable_block("a:1") is None

    def test_requesters_are_tracked_per_block(self) -> None:
        piece = make_piece()
        piece.mark_requested(0, "b:2")
        piece.mark_requested(0, "a:1")

        assert piece.requesters(0) == ("a:1", "b:2")
        assert [block.offset for block in piece.outstanding_requests(peer="a:1")] == [0]

    def test_an_orphaned_block_returns_to_the_pool(self) -> None:
        piece = make_piece()
        piece.mark_requested(0, "a:1")
        piece.mark_requested(0, "b:2")

        assert piece.mark_orphaned(0, "a:1") is False  # b still wants it
        assert piece.mark_orphaned(0, "b:2") is True
        assert piece.requestable_block("c:3") == piece.blocks[0]

    def test_orphaning_an_unknown_block_is_a_no_op(self) -> None:
        assert make_piece().mark_orphaned(999, "a:1") is False

    def test_the_state_moves_to_requested(self) -> None:
        piece = make_piece()
        piece.mark_requested(0, "a:1")

        assert piece.state is PieceState.REQUESTED
        assert piece.in_progress is True


class TestAssembly:
    def test_blocks_are_placed_at_their_offset(self) -> None:
        piece = make_piece()

        piece.add_block(0, b"a" * DEFAULT_BLOCK_SIZE, source="a:1")
        piece.add_block(DEFAULT_BLOCK_SIZE, b"b" * DEFAULT_BLOCK_SIZE, source="b:2")

        assert piece.received_blocks == 2
        assert piece.received_bytes == 2 * DEFAULT_BLOCK_SIZE
        assert piece.progress == 0.5
        assert piece.state is PieceState.DOWNLOADING
        assert piece.data() is None

    def test_a_full_piece_assembles_in_order(self) -> None:
        piece = make_piece()
        expected = b""
        for block in piece.blocks:
            data = bytes([block.offset % 256]) * block.length
            expected += data
            piece.add_block(block.offset, data)

        assert piece.complete is True
        assert piece.state is PieceState.DOWNLOADED
        assert piece.data() == expected

    def test_a_block_at_an_unknown_offset_is_refused(self) -> None:
        piece = make_piece()

        assert piece.add_block(7, b"x" * DEFAULT_BLOCK_SIZE) is False
        assert piece.received_blocks == 0

    def test_a_block_of_the_wrong_length_is_refused(self) -> None:
        piece = make_piece()

        assert piece.add_block(0, b"too short") is False

    def test_a_duplicate_block_is_refused(self) -> None:
        piece = make_piece()
        piece.add_block(0, b"x" * DEFAULT_BLOCK_SIZE, source="a:1")

        assert piece.add_block(0, b"y" * DEFAULT_BLOCK_SIZE, source="b:2") is False
        assert piece.sources == ["a:1"]

    def test_sources_record_who_contributed(self) -> None:
        piece = make_piece()
        piece.add_block(0, b"x" * DEFAULT_BLOCK_SIZE, source="a:1")
        piece.add_block(DEFAULT_BLOCK_SIZE, b"y" * DEFAULT_BLOCK_SIZE, source="b:2")
        piece.add_block(32_768, b"z" * DEFAULT_BLOCK_SIZE, source="a:1")

        assert piece.sources == ["a:1", "b:2"]

    def test_a_received_block_is_no_longer_requested(self) -> None:
        piece = make_piece()
        piece.mark_requested(0, "a:1")
        piece.add_block(0, b"x" * DEFAULT_BLOCK_SIZE, source="a:1")

        assert piece.outstanding_requests(peer="a:1") == ()


class TestTransitions:
    def test_verifying_then_verified_releases_the_buffer(self) -> None:
        piece = make_piece()
        for block in piece.blocks:
            piece.add_block(block.offset, b"x" * block.length)
        piece.mark_verifying()

        assert piece.state is PieceState.VERIFYING
        assert piece.data() is not None  # still needed for the write

        piece.mark_verified()

        assert piece.state is PieceState.VERIFIED
        assert piece.finished is True
        assert piece.data() is None  # bytes are on disk now

    def test_a_failed_piece_is_reset_completely(self) -> None:
        piece = make_piece()
        for block in piece.blocks:
            piece.mark_requested(block.offset, "a:1")
            piece.add_block(block.offset, b"x" * block.length, source="a:1")

        piece.mark_failed()

        assert piece.state is PieceState.FAILED
        assert piece.failures == 1
        assert piece.received_blocks == 0
        assert piece.data() is None
        # Every block is up for grabs again, from any peer.
        assert piece.requestable_block("a:1") == piece.blocks[0]

        piece.reset()
        assert piece.state is PieceState.MISSING
        assert piece.requestable_block("a:1") == piece.blocks[0]

    def test_failure_counts_accumulate(self) -> None:
        piece = make_piece()

        piece.mark_failed()
        piece.mark_failed()

        assert piece.failures == 2
        assert piece.state is PieceState.FAILED

    def test_reset_clears_requesters(self) -> None:
        piece = make_piece()
        piece.mark_requested(0, "a:1")
        piece.reset()

        assert piece.requesters(0) == ()
        assert piece.state is PieceState.MISSING

    def test_release_frees_the_buffer(self) -> None:
        piece = make_piece()
        piece.add_block(0, b"x" * DEFAULT_BLOCK_SIZE)
        piece.release()

        assert piece.received_blocks == 1  # bookkeeping survives
        assert piece.data() is None

    def test_progress_of_a_zero_block_piece(self) -> None:
        piece = Piece(index=0, size=0, expected_hash=HASH)

        assert piece.progress == 1.0
        assert piece.complete is True

    def test_block_states_for_the_ui(self) -> None:
        piece = make_piece(index=5)
        piece.mark_requested(0, "a:1")

        states = piece.block_states()

        assert states[(5, 0)] is BlockState.REQUESTED
        assert states[(5, 16_384)] is BlockState.MISSING


class TestBuildPieces:
    def test_pieces_are_built_in_order(self) -> None:
        pieces = build_pieces(sizes=[10, 20, 5], hashes=[HASH] * 3, block_size=8)

        assert [piece.index for piece in pieces] == [0, 1, 2]
        assert [piece.size for piece in pieces] == [10, 20, 5]
        assert pieces[1].block_count == 3

    def test_verified_pieces_start_finished(self) -> None:
        pieces = build_pieces(sizes=[10, 20], hashes=[HASH] * 2, verified={1})

        assert pieces[0].state is PieceState.MISSING
        assert pieces[1].state is PieceState.VERIFIED
        assert pieces[1].finished is True

    def test_mismatched_inputs_are_refused(self) -> None:
        with pytest.raises(ValueError):
            build_pieces(sizes=[10, 20], hashes=[HASH])


class TestViews:
    def test_the_piece_describes_itself(self) -> None:
        piece = make_piece(index=3)
        assert str(piece) == "Piece(index=3, state=missing, 0/4 blocks)"

    def test_block_state_rejects_an_offset_that_is_not_a_block(self) -> None:
        piece = make_piece()

        with pytest.raises(ValueError, match="does not start a block"):
            piece.block_state(7)
