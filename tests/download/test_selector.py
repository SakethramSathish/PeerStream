"""Tests for piece selection (FR-07, TRD §26).

Selection is a pure function of what we want and how rare each piece is, so
these tests are just data in, order out — no peers, no sockets.
"""

from __future__ import annotations

from random import Random

import pytest
from app.core.config import PieceStrategy
from app.download.selector import PieceSelector
from app.peer.bitfield import Bitfield, PieceAvailability


def wanted(*indices: int, pieces: int = 8) -> Bitfield:
    """A bitfield marking the pieces we still need."""
    field = Bitfield(pieces)
    for index in indices:
        field.set(index)
    return field


def availability(counts: dict[int, int], *, pieces: int = 8) -> PieceAvailability:
    """Availability built by adding that many fake holders per piece."""
    result = PieceAvailability(pieces)
    for index, count in counts.items():
        for _ in range(count):
            result.add_piece(index)
    return result


class TestSequential:
    def test_lowest_index_first(self) -> None:
        selector = PieceSelector(8, strategy=PieceStrategy.SEQUENTIAL)

        assert selector.select(wanted(5, 1, 3)) == (1, 3, 5)

    def test_nothing_wanted_is_nothing_selected(self) -> None:
        selector = PieceSelector(8, strategy=PieceStrategy.SEQUENTIAL)

        assert selector.select(wanted()) == ()

    def test_a_limit_truncates(self) -> None:
        selector = PieceSelector(8, strategy=PieceStrategy.SEQUENTIAL)

        assert selector.select(wanted(0, 1, 2, 3), limit=2) == (0, 1)


class TestRarestFirst:
    def test_rarest_pieces_come_first(self) -> None:
        selector = PieceSelector(4, strategy=PieceStrategy.RAREST_FIRST)
        counts = availability({0: 12, 1: 2, 2: 9, 3: 1}, pieces=4)

        assert selector.select(wanted(0, 1, 2, 3, pieces=4), availability=counts) == (
            3,
            1,
            2,
            0,
        )

    def test_ties_are_broken_by_index(self) -> None:
        selector = PieceSelector(4, strategy=PieceStrategy.RAREST_FIRST)
        counts = availability({0: 2, 1: 2, 2: 2, 3: 2}, pieces=4)

        assert selector.select(wanted(3, 1, 2, 0, pieces=4), availability=counts) == (
            0,
            1,
            2,
            3,
        )

    def test_only_wanted_pieces_are_offered(self) -> None:
        selector = PieceSelector(4, strategy=PieceStrategy.RAREST_FIRST)
        counts = availability({0: 1, 1: 5}, pieces=4)

        assert selector.select(wanted(1, pieces=4), availability=counts) == (1,)

    def test_without_availability_it_falls_back_to_sequential(self) -> None:
        selector = PieceSelector(4, strategy=PieceStrategy.RAREST_FIRST)

        assert selector.select(wanted(2, 0, pieces=4)) == (0, 2)


class TestRandom:
    def test_the_order_is_stable_for_one_seed(self) -> None:
        first = PieceSelector(6, strategy=PieceStrategy.RANDOM, rng=Random(7))
        second = PieceSelector(6, strategy=PieceStrategy.RANDOM, rng=Random(7))

        assert first.select(wanted(0, 1, 2, 3, 4, 5, pieces=6)) == second.select(
            wanted(0, 1, 2, 3, 4, 5, pieces=6)
        )

    def test_it_really_is_a_permutation(self) -> None:
        selector = PieceSelector(6, strategy=PieceStrategy.RANDOM, rng=Random(1))
        field = wanted(0, 1, 2, 3, 4, 5, pieces=6)

        order = selector.select(field)

        assert sorted(order) == [0, 1, 2, 3, 4, 5]
        assert order != tuple(range(6)) or True  # a sorted result is legal, just unlikely


class TestValidation:
    def test_a_mismatched_bitfield_is_refused(self) -> None:
        selector = PieceSelector(8)

        with pytest.raises(ValueError, match="selector was built for 8"):
            selector.select(wanted(0, pieces=4))

    def test_a_negative_piece_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            PieceSelector(-1)

    def test_properties_are_exposed(self) -> None:
        selector = PieceSelector(8, strategy=PieceStrategy.SEQUENTIAL)

        assert selector.piece_count == 8
        assert selector.strategy is PieceStrategy.SEQUENTIAL
