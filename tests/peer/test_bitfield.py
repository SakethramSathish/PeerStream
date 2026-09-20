"""Unit tests for bitfields and swarm availability counts."""

from __future__ import annotations

import pytest
from app.peer.bitfield import Bitfield, PieceAvailability
from app.peer.errors import MessageError


class TestBasics:
    def test_new_bitfield_is_empty(self) -> None:
        field = Bitfield(16)
        assert len(field) == 16
        assert field.count == 0
        assert field.empty
        assert not field.complete

    def test_set_and_test(self) -> None:
        field = Bitfield(16)
        field.set(3)
        assert field.has(3)
        assert not field.has(4)
        assert field.count == 1

    def test_clear(self) -> None:
        field = Bitfield(16)
        field.set(3)
        field.clear(3)
        assert not field.has(3)
        assert field.count == 0

    def test_set_with_explicit_false(self) -> None:
        field = Bitfield(16)
        field.set(1, True)
        field.set(1, False)
        assert not field.has(1)

    def test_full(self) -> None:
        field = Bitfield.full(16)
        assert field.count == 16
        assert field.complete

    def test_indices(self) -> None:
        field = Bitfield.from_indices([0, 3, 7], 16)
        assert field.indices() == [0, 3, 7]

    def test_repr(self) -> None:
        assert repr(Bitfield.from_indices([0, 1], 16)) == "<Bitfield 2/16 pieces>"

    def test_equality(self) -> None:
        assert Bitfield.from_indices([0, 1], 16) == Bitfield.from_indices([1, 0], 16)
        assert Bitfield.from_indices([0], 16) != Bitfield.from_indices([0], 32)
        assert Bitfield(8) != "not a bitfield"

    def test_out_of_range_access_raises(self) -> None:
        field = Bitfield(8)
        with pytest.raises(IndexError, match="outside 0-7"):
            field.has(8)
        with pytest.raises(IndexError):
            field.set(-1)

    def test_negative_piece_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            Bitfield(-1)


class TestSerialisation:
    @pytest.mark.parametrize(
        ("piece_count", "expected_bytes"),
        [(0, 0), (1, 1), (7, 1), (8, 1), (9, 2), (16, 2), (17, 3)],
    )
    def test_wire_size(self, piece_count: int, expected_bytes: int) -> None:
        assert len(Bitfield(piece_count).to_bytes()) == expected_bytes

    def test_round_trip(self) -> None:
        field = Bitfield.from_indices([0, 5, 9], 12)
        restored = Bitfield.from_bytes(field.to_bytes(), 12)
        assert restored == field

    def test_bit_order_is_most_significant_first(self) -> None:
        field = Bitfield(8)
        field.set(0)
        assert field.to_bytes() == b"\x80"
        field.set(7)
        assert field.to_bytes() == b"\x81"

    def test_spare_bits_are_masked_off(self) -> None:
        """A 10-piece bitfield is 2 bytes; the last 6 bits mean nothing."""
        field = Bitfield.from_bytes(b"\xff\xff", 10)
        assert field.to_bytes() == b"\xff\xc0"
        assert field.count == 10

    def test_extra_bytes_are_tolerated(self) -> None:
        field = Bitfield.from_bytes(b"\xff\xff\xff\xff", 8)
        assert field.count == 8

    def test_too_short_is_rejected(self) -> None:
        with pytest.raises(MessageError, match="too short for 16 pieces"):
            Bitfield.from_bytes(b"\xff", 16)

    def test_empty_bitfield_for_zero_pieces(self) -> None:
        assert Bitfield.from_bytes(b"", 0).to_bytes() == b""

    def test_hex_round_trip(self) -> None:
        field = Bitfield.from_indices([0, 7], 9)
        assert field.to_hex() == "8100"  # nine pieces need two bytes
        assert Bitfield.from_hex(field.to_hex(), 9) == field

    def test_malformed_hex_is_rejected(self) -> None:
        with pytest.raises(MessageError, match="hex is malformed"):
            Bitfield.from_hex("zz", 8)


class TestComparisons:
    def test_missing_from(self) -> None:
        ours = Bitfield.from_indices([0, 1], 16)
        theirs = Bitfield.from_indices([1, 2, 3], 16)
        assert ours.missing_from(theirs) == [2, 3]

    def test_is_interesting(self) -> None:
        ours = Bitfield.from_indices([0, 1], 16)
        assert ours.is_interesting(Bitfield.from_indices([5], 16))
        assert not ours.is_interesting(Bitfield.from_indices([0, 1], 16))

    def test_mismatched_sizes_are_rejected(self) -> None:
        ours = Bitfield(16)
        theirs = Bitfield(32)
        with pytest.raises(ValueError, match="different sizes"):
            ours.missing_from(theirs)
        with pytest.raises(ValueError, match="different sizes"):
            ours.is_interesting(theirs)


class TestPieceAvailability:
    def test_counts_peers_holding_each_piece(self) -> None:
        availability = PieceAvailability(4)
        availability.add(Bitfield.from_indices([0, 1], 4))
        availability.add(Bitfield.from_indices([1, 2], 4))

        assert availability.count(0) == 1
        assert availability.count(1) == 2
        assert availability.count(2) == 1
        assert availability.count(3) == 0

    def test_remove(self) -> None:
        availability = PieceAvailability(4)
        field = Bitfield.from_indices([0, 1], 4)
        availability.add(field)
        availability.remove(field)
        assert availability.count(0) == 0

    def test_single_piece_updates(self) -> None:
        availability = PieceAvailability(4)
        availability.add_piece(2)
        availability.add_piece(2)
        assert availability.count(2) == 2
        availability.remove_piece(2)
        assert availability.count(2) == 1

    def test_rarest_first_with_ties_broken_by_index(self) -> None:
        availability = PieceAvailability(4)
        availability.add(Bitfield.from_indices([0, 1, 2, 3], 4))
        availability.add(Bitfield.from_indices([0], 4))
        wanted = Bitfield.from_indices([0, 1, 2, 3], 4)

        # Piece 0 is held by two peers; 1..3 by one, ordered by index.
        assert availability.rarest(wanted) == [1, 2, 3, 0]

    def test_rarest_ignores_unwanted_pieces(self) -> None:
        availability = PieceAvailability(4)
        wanted = Bitfield.from_indices([2], 4)
        assert availability.rarest(wanted) == [2]

    def test_rarest_limit(self) -> None:
        availability = PieceAvailability(8)
        wanted = Bitfield.from_indices([0, 1, 2, 3], 8)
        assert availability.rarest(wanted, limit=2) == [0, 1]

    def test_mismatched_size_is_rejected(self) -> None:
        availability = PieceAvailability(8)
        with pytest.raises(ValueError, match="different size"):
            availability.add(Bitfield(4))

    def test_negative_piece_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            PieceAvailability(-1)

    def test_length_and_repr(self) -> None:
        availability = PieceAvailability(8)
        assert len(availability) == 8
        assert repr(availability) == "<PieceAvailability 8 pieces>"


class TestAvailabilityIntrospection:
    def test_piece_count(self) -> None:
        assert PieceAvailability(12).piece_count == 12
