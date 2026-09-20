"""Piece availability bitfields and swarm availability counts.

A peer's bitfield is the single most important thing it tells us: one bit per
piece, set when the peer has it. Everything downstream depends on reading it
correctly — rarest-first selection (M7), whether a peer is interesting at all,
and what the UI draws in the piece matrix.

Two rules that bite in practice:

**Spare bits are tolerated on input, cleared on output.** With 10 pieces the
bitfield is two bytes, so six bits mean nothing. Real peers set them anyway,
so rejecting a bitfield for having junk in the spare bits would drop perfectly
good peers. We mask them off on parse and always write zeros.

**Availability is maintained incrementally.** Rarest-first needs "how many
peers hold piece N" for every N, and recomputing that by OR-ing every peer's
bitfield on each decision is O(peers x pieces) per decision. Keeping a counter
per piece and adjusting it when a peer connects, sends ``have`` or leaves keeps
selection cheap.
"""

from __future__ import annotations

from typing import Final

from app.peer.errors import MessageError

_BITS_PER_BYTE: Final[int] = 8


def _byte_count(piece_count: int) -> int:
    """Bytes needed to hold one bit per piece."""
    return (piece_count + _BITS_PER_BYTE - 1) // _BITS_PER_BYTE


class Bitfield:
    """Which pieces a peer (or we) hold.

    Args:
        piece_count: Number of pieces in the torrent.
    """

    __slots__ = ("_bits", "_piece_count")

    def __init__(self, piece_count: int) -> None:
        if piece_count < 0:
            raise ValueError(f"piece_count must not be negative, got {piece_count}")
        self._piece_count = piece_count
        self._bits = bytearray(_byte_count(piece_count))

    # ---------------------------------------------------------- constructors

    @classmethod
    def from_bytes(cls, data: bytes, piece_count: int) -> Bitfield:
        """Parse a wire bitfield, tolerating trailing bytes and spare bits.

        Raises:
            MessageError: If the payload is too short for the piece count.
        """
        expected = _byte_count(piece_count)
        if len(data) < expected:
            raise MessageError(
                f"bitfield of {len(data)} bytes is too short for {piece_count} "
                f"pieces ({expected} bytes required)"
            )
        instance = cls(piece_count)
        instance._bits = bytearray(data[:expected])
        instance._mask_spare_bits()
        return instance

    @classmethod
    def from_hex(cls, value: str, piece_count: int) -> Bitfield:
        """Rebuild a bitfield from its hex form (used by resume state, M6)."""
        try:
            data = bytes.fromhex(value)
        except ValueError as exc:
            raise MessageError(f"bitfield hex is malformed: {exc}") from exc
        return cls.from_bytes(data, piece_count)

    @classmethod
    def full(cls, piece_count: int) -> Bitfield:
        """A bitfield with every piece set — a seeder."""
        instance = cls(piece_count)
        instance._bits = bytearray(b"\xff" * _byte_count(piece_count))
        instance._mask_spare_bits()
        return instance

    @classmethod
    def from_indices(cls, indices: list[int], piece_count: int) -> Bitfield:
        """Build a bitfield from a list of completed piece indices."""
        instance = cls(piece_count)
        for index in indices:
            instance.set(index)
        return instance

    # ------------------------------------------------------------- accessors

    @property
    def piece_count(self) -> int:
        """Number of pieces this bitfield describes."""
        return self._piece_count

    @property
    def count(self) -> int:
        """How many pieces are set."""
        return int.from_bytes(self._bits, "big").bit_count()

    @property
    def complete(self) -> bool:
        """Whether every piece is set."""
        return self.count == self._piece_count

    @property
    def empty(self) -> bool:
        """Whether no piece is set."""
        return self.count == 0

    def has(self, index: int) -> bool:
        """Whether piece ``index`` is set.

        Raises:
            IndexError: If the index is outside the torrent's range. Callers
                handling peer input must range-check first, which is what
                :meth:`app.peer.state.PeerSession.apply` does.
        """
        self._check(index)
        byte, bit = divmod(index, _BITS_PER_BYTE)
        return bool(self._bits[byte] & (0x80 >> bit))

    def set(self, index: int, value: bool = True) -> None:
        """Set or clear one piece."""
        self._check(index)
        byte, bit = divmod(index, _BITS_PER_BYTE)
        if value:
            self._bits[byte] |= 0x80 >> bit
        else:
            self._bits[byte] &= ~(0x80 >> bit) & 0xFF

    def clear(self, index: int) -> None:
        """Clear one piece."""
        self.set(index, False)

    def indices(self) -> list[int]:
        """Every set piece index, ascending."""
        return [index for index in range(self._piece_count) if self.has(index)]

    def missing_from(self, other: Bitfield) -> list[int]:
        """Indices ``other`` holds that we do not — what we can request."""
        if other.piece_count != self._piece_count:
            raise ValueError("bitfields describe torrents of different sizes")
        return [
            index for index in range(self._piece_count) if other.has(index) and not self.has(index)
        ]

    def is_interesting(self, other: Bitfield) -> bool:
        """Whether ``other`` holds at least one piece we lack.

        Being "interesting" is what decides whether we send ``interested``; a
        peer with nothing we need is not worth a slot.
        """
        if other.piece_count != self._piece_count:
            raise ValueError("bitfields describe torrents of different sizes")
        return any(other.has(index) and not self.has(index) for index in range(self._piece_count))

    # ---------------------------------------------------------------- serial

    def to_bytes(self) -> bytes:
        """The wire form, with spare bits forced to zero."""
        self._mask_spare_bits()
        return bytes(self._bits)

    def to_hex(self) -> str:
        """Hex form for resume state (M6)."""
        return self.to_bytes().hex()

    # -------------------------------------------------------------- internals

    def _mask_spare_bits(self) -> None:
        """Zero the bits that belong to no piece."""
        if not self._bits:
            return
        spare = self._piece_count % _BITS_PER_BYTE
        if spare:
            self._bits[-1] &= (0xFF << (_BITS_PER_BYTE - spare)) & 0xFF

    def _check(self, index: int) -> None:
        if not 0 <= index < self._piece_count:
            raise IndexError(f"piece index {index} is outside 0-{self._piece_count - 1}")

    # ----------------------------------------------------------- comparison

    def __len__(self) -> int:
        return self._piece_count

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Bitfield):
            return NotImplemented
        return self._piece_count == other._piece_count and self._bits == other._bits

    def __repr__(self) -> str:
        return f"<Bitfield {self.count}/{self._piece_count} pieces>"


class PieceAvailability:
    """How many connected peers hold each piece: rarest-first's input.

    Args:
        piece_count: Number of pieces in the torrent.
    """

    __slots__ = ("_counts",)

    def __init__(self, piece_count: int) -> None:
        if piece_count < 0:
            raise ValueError(f"piece_count must not be negative, got {piece_count}")
        self._counts = [0] * piece_count

    @property
    def piece_count(self) -> int:
        """Number of pieces tracked."""
        return len(self._counts)

    def add(self, bitfield: Bitfield) -> None:
        """Account for a peer's pieces (on connect, or on a ``have``)."""
        self._apply(bitfield, +1)

    def remove(self, bitfield: Bitfield) -> None:
        """Stop accounting for a peer's pieces (on disconnect)."""
        self._apply(bitfield, -1)

    def add_piece(self, index: int) -> None:
        """Account for one newly announced piece (``have``)."""
        self._counts[index] += 1

    def remove_piece(self, index: int) -> None:
        """Stop accounting for one piece (peer disconnect, piece dropped)."""
        self._counts[index] -= 1

    def count(self, index: int) -> int:
        """How many peers hold piece ``index``."""
        return self._counts[index]

    def rarest(self, wanted: Bitfield, *, limit: int | None = None) -> list[int]:
        """Pieces we want, rarest first, ties broken by index.

        Args:
            wanted: Pieces we still need (a bitfield of our gaps).
            limit: Optional maximum number of indices to return.

        Returns:
            Candidate indices ordered by how few peers hold them.
        """
        candidates = [index for index in range(len(self._counts)) if wanted.has(index)]
        candidates.sort(key=lambda index: (self._counts[index], index))
        return candidates[:limit] if limit is not None else candidates

    def _apply(self, bitfield: Bitfield, delta: int) -> None:
        if bitfield.piece_count != len(self._counts):
            raise ValueError("bitfield describes a torrent of a different size")
        for index in range(len(self._counts)):
            if bitfield.has(index):
                self._counts[index] += delta

    def __len__(self) -> int:
        return len(self._counts)

    def __repr__(self) -> str:
        return f"<PieceAvailability {len(self._counts)} pieces>"
