"""Block model: the unit of transfer (TRD §21).

A piece is the unit of **integrity** — it is what the torrent's SHA-1 hashes
describe. A block is the unit of **transfer** — it is what a ``request``
message asks for. Splitting pieces into 16 KiB blocks is what stops one slow
peer from monopolising a piece, and it lets several peers contribute to the
same piece at once.

Blocks are identified by ``(piece_index, offset)``, which is all a
``request``/``piece``/``cancel`` message carries, so the same key works for
bookkeeping and for the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.constants import DEFAULT_BLOCK_SIZE, MAX_BLOCK_SIZE

BlockKey = tuple[int, int]
"""Identity of a block: ``(piece index, offset within the piece)``."""


class BlockState(StrEnum):
    """Where one block is in its short life.

    ``MISSING → REQUESTED → RECEIVED`` is the whole story. A block that is
    requested and then lost (peer hung up, request timed out) goes back to
    ``MISSING``, because from the download's point of view it was never
    received — there is no partial block to remember.
    """

    MISSING = "missing"
    REQUESTED = "requested"
    RECEIVED = "received"


@dataclass(frozen=True, slots=True)
class Block:
    """One block of one piece.

    Attributes:
        index: Piece this block belongs to.
        offset: Offset of the block within the piece.
        length: Size of the block; only the final block of a piece is short.
    """

    index: int
    offset: int
    length: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError(f"piece index must not be negative, got {self.index}")
        if self.offset < 0:
            raise ValueError(f"block offset must not be negative, got {self.offset}")
        if not 0 < self.length <= MAX_BLOCK_SIZE:
            raise ValueError(
                f"block length must be between 1 and {MAX_BLOCK_SIZE}, got {self.length}"
            )

    @property
    def end(self) -> int:
        """Offset one past the last byte of this block."""
        return self.offset + self.length

    @property
    def key(self) -> BlockKey:
        """The ``(index, offset)`` pair used for bookkeeping and cancels."""
        return (self.index, self.offset)

    def __str__(self) -> str:
        return f"Block(piece={self.index}, offset={self.offset}, length={self.length})"


def plan_blocks(
    index: int, piece_size: int, block_size: int = DEFAULT_BLOCK_SIZE
) -> tuple[Block, ...]:
    """Split a piece into blocks.

    Args:
        index: Piece index the blocks belong to.
        piece_size: Size of the piece in bytes.
        block_size: Nominal block size; the last block absorbs the remainder.

    Returns:
        Blocks covering the whole piece, in order. A zero-length piece yields
        no blocks.

    Raises:
        ValueError: If the sizes are not positive.
    """
    if piece_size < 0:
        raise ValueError(f"piece size must not be negative, got {piece_size}")
    if block_size <= 0:
        raise ValueError(f"block size must be positive, got {block_size}")

    blocks: list[Block] = []
    offset = 0
    while offset < piece_size:
        length = min(block_size, piece_size - offset)
        blocks.append(Block(index=index, offset=offset, length=length))
        offset += length
    return tuple(blocks)


def block_at(piece_size: int, offset: int, block_size: int = DEFAULT_BLOCK_SIZE) -> Block:
    """The block that starts at ``offset`` in a piece of ``piece_size`` bytes.

    Args:
        piece_size: Size of the piece.
        offset: Offset within the piece; must be a block boundary.
        block_size: Nominal block size.

    Returns:
        The block starting there.

    Raises:
        ValueError: If ``offset`` is not a block boundary or is outside the piece.
    """
    if not 0 <= offset < piece_size:
        raise ValueError(f"offset {offset} is outside a {piece_size}-byte piece")
    if offset % block_size != 0:
        raise ValueError(f"offset {offset} is not a multiple of the block size {block_size}")
    return Block(index=0, offset=offset, length=min(block_size, piece_size - offset))
