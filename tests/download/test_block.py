"""Tests for blocks: the unit of transfer."""

from __future__ import annotations

import pytest
from app.core.constants import MAX_BLOCK_SIZE
from app.download.block import Block, BlockState, block_at, plan_blocks


class TestBlock:
    def test_geometry(self) -> None:
        block = Block(index=3, offset=16, length=8)

        assert block.end == 24
        assert block.key == (3, 16)
        assert "piece=3" in str(block)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"index": -1, "offset": 0, "length": 8}, "piece index must not be negative"),
            ({"index": 0, "offset": -1, "length": 8}, "block offset must not be negative"),
            ({"index": 0, "offset": 0, "length": 0}, "must be between 1"),
            ({"index": 0, "offset": 0, "length": MAX_BLOCK_SIZE + 1}, "must be between 1"),
        ],
    )
    def test_nonsense_is_refused(self, kwargs: dict[str, int], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            Block(**kwargs)


class TestPlanBlocks:
    def test_a_piece_splits_into_even_blocks(self) -> None:
        blocks = plan_blocks(0, 40, block_size=10)

        assert [(block.offset, block.length) for block in blocks] == [
            (0, 10),
            (10, 10),
            (20, 10),
            (30, 10),
        ]
        assert all(block.index == 0 for block in blocks)

    def test_the_last_block_absorbs_the_remainder(self) -> None:
        blocks = plan_blocks(7, 25, block_size=10)

        assert [(block.offset, block.length) for block in blocks] == [(0, 10), (10, 10), (20, 5)]
        assert blocks[-1].index == 7

    def test_a_piece_smaller_than_a_block_is_one_block(self) -> None:
        assert len(plan_blocks(0, 100, block_size=16_384)) == 1

    def test_an_empty_piece_has_no_blocks(self) -> None:
        assert plan_blocks(0, 0, block_size=16_384) == ()

    @pytest.mark.parametrize(
        ("size", "block_size", "message"),
        [(-1, 16, "must not be negative"), (16, 0, "must be positive")],
    )
    def test_nonsense_sizes_are_refused(self, size: int, block_size: int, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            plan_blocks(0, size, block_size=block_size)


class TestBlockAt:
    def test_the_block_starting_at_an_offset(self) -> None:
        assert block_at(40, 30, block_size=10) == Block(index=0, offset=30, length=10)

    def test_the_last_block_is_short(self) -> None:
        assert block_at(25, 20, block_size=10).length == 5

    def test_an_offset_outside_the_piece_is_refused(self) -> None:
        with pytest.raises(ValueError, match="outside"):
            block_at(40, 40, block_size=10)

    def test_a_non_boundary_offset_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a multiple"):
            block_at(40, 5, block_size=10)


class TestBlockState:
    def test_states_are_readable(self) -> None:
        assert BlockState.MISSING.value == "missing"
        assert BlockState.REQUESTED.value == "requested"
        assert BlockState.RECEIVED.value == "received"
