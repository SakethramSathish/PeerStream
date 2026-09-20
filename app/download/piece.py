"""Piece model and state machine (TRD §21, FR-06, FR-08).

A piece is the unit the torrent's hashes speak about, so it is the unit this
class tracks: its blocks, the buffer those blocks are assembled into, and where
the piece is in its life::

    MISSING → REQUESTED → DOWNLOADING → DOWNLOADED → VERIFYING → VERIFIED
                                                          └────→ FAILED → MISSING

Two details matter more than the rest:

**Block bookkeeping remembers who asked for what.** ``requestable_block(peer)``
will not hand the same block to the same peer twice, but in endgame mode it
*will* hand a block to a second peer while the first request is still out —
that duplication is the point of endgame, and the tracker of who-asked-for-what
is what lets the winner's arrival cancel the losers.

**A failed piece is reset, not half-kept.** When a piece's SHA-1 does not match,
every block goes back to ``MISSING`` and the buffer is dropped: keeping any of
it would mean the retry could re-assemble the same corrupt bytes.
"""

from __future__ import annotations

from collections.abc import Container, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.constants import DEFAULT_BLOCK_SIZE, INFO_HASH_SIZE
from app.download.block import Block, BlockKey, BlockState, plan_blocks


class PieceState(StrEnum):
    """Where a piece is in its life.

    The path back from ``FAILED`` to ``MISSING`` is deliberate: a hash failure
    costs a re-download, and the peer that sent the bad bytes is penalised by
    whoever owns peer reputation (the download manager, in M7).
    """

    MISSING = "missing"
    REQUESTED = "requested"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass(slots=True)
class Piece:
    """One piece being downloaded.

    Args:
        index: Piece index within the torrent.
        size: Size of the piece in bytes (the last piece may be shorter).
        expected_hash: The 20-byte SHA-1 the metainfo promises.
        block_size: Nominal block size for requests.
    """

    index: int
    size: int
    expected_hash: bytes
    block_size: int = DEFAULT_BLOCK_SIZE
    state: PieceState = PieceState.MISSING
    failures: int = 0
    sources: list[str] = field(default_factory=list)
    blocks: tuple[Block, ...] = field(init=False)
    _states: dict[int, BlockState] = field(init=False, repr=False, default_factory=dict)
    _requesters: dict[int, set[str]] = field(init=False, repr=False, default_factory=dict)
    _buffer: bytearray | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError(f"piece index must not be negative, got {self.index}")
        if self.size < 0:
            raise ValueError(f"piece size must not be negative, got {self.size}")
        if len(self.expected_hash) != INFO_HASH_SIZE:
            raise ValueError(
                f"piece hash must be {INFO_HASH_SIZE} bytes, got {len(self.expected_hash)}"
            )
        self.blocks = plan_blocks(self.index, self.size, self.block_size)
        self._states = {block.offset: BlockState.MISSING for block in self.blocks}
        self._requesters = {block.offset: set() for block in self.blocks}

    # ------------------------------------------------------------- geometry

    @property
    def block_count(self) -> int:
        """Number of blocks in this piece."""
        return len(self.blocks)

    @property
    def received_blocks(self) -> int:
        """Blocks whose data has arrived."""
        return sum(1 for state in self._states.values() if state is BlockState.RECEIVED)

    @property
    def unclaimed_blocks(self) -> int:
        """Blocks nobody has asked for yet.

        This — not the number of blocks still missing — is what endgame mode
        watches: a block that is already requested is not work a peer could
        otherwise be doing.
        """
        return sum(1 for state in self._states.values() if state is BlockState.MISSING)

    @property
    def requested_blocks(self) -> int:
        """Blocks currently requested from at least one peer."""
        return sum(1 for requesters in self._requesters.values() if requesters)

    @property
    def missing_blocks(self) -> int:
        """Blocks with no data and no request outstanding."""
        return self.block_count - self.received_blocks

    @property
    def received_bytes(self) -> int:
        """Bytes of block data held in memory."""
        return sum(
            block.length
            for block in self.blocks
            if self._states[block.offset] is BlockState.RECEIVED
        )

    @property
    def complete(self) -> bool:
        """True when every block has arrived."""
        return all(state is BlockState.RECEIVED for state in self._states.values())

    @property
    def in_progress(self) -> bool:
        """True while the piece is being assembled, verified, or stored."""
        return self.state in {
            PieceState.REQUESTED,
            PieceState.DOWNLOADING,
            PieceState.DOWNLOADED,
            PieceState.VERIFYING,
        }

    @property
    def finished(self) -> bool:
        """True when the piece needs no more work (verified and stored)."""
        return self.state is PieceState.VERIFIED

    @property
    def progress(self) -> float:
        """Fraction of the piece's blocks that have arrived, in ``[0.0, 1.0]``."""
        if self.block_count == 0:
            return 1.0
        return self.received_blocks / self.block_count

    # ------------------------------------------------------------- selection

    def requestable_block(self, peer: str, *, allow_duplicates: bool = False) -> Block | None:
        """Pick a block for ``peer`` to fetch.

        Args:
            peer: Identity of the asking peer, so a peer never gets the same
                block twice.
            allow_duplicates: Endgame mode. When true, a block already
                requested from *another* peer is offered too, so the last few
                blocks can race instead of waiting on one slow peer.

        Returns:
            The block to request, or ``None`` when this peer has nothing left
            to contribute to the piece.
        """
        if self.state in {PieceState.VERIFIED, PieceState.VERIFYING, PieceState.DOWNLOADED}:
            return None
        for block in self.blocks:
            state = self._states[block.offset]
            if state is BlockState.RECEIVED:
                continue
            requesters = self._requesters[block.offset]
            if peer in requesters:
                continue
            if requesters and not allow_duplicates:
                continue
            return block
        return None

    def outstanding_requests(self, *, peer: str | None = None) -> tuple[Block, ...]:
        """Blocks currently requested, optionally only those from one peer."""
        return tuple(
            block
            for block in self.blocks
            if self._states[block.offset] is BlockState.REQUESTED
            and (peer is None or peer in self._requesters[block.offset])
        )

    def block_state(self, offset: int) -> BlockState:
        """State of the block starting at ``offset``."""
        state = self._states.get(offset)
        if state is None:
            raise ValueError(f"offset {offset} does not start a block of piece {self.index}")
        return state

    def requesters(self, offset: int) -> tuple[str, ...]:
        """Peers with an outstanding request for the block at ``offset``."""
        return tuple(sorted(self._requesters.get(offset, set())))

    # ------------------------------------------------------------ bookkeeping

    def mark_requested(self, offset: int, peer: str) -> None:
        """Record that ``peer`` now has a request out for this block."""
        self._requesters[offset].add(peer)
        if self._states[offset] is BlockState.MISSING:
            self._states[offset] = BlockState.REQUESTED
        if self.state is PieceState.MISSING:
            self.state = PieceState.REQUESTED

    def mark_orphaned(self, offset: int, peer: str) -> bool:
        """Drop ``peer``'s request for a block (timeout or disconnect).

        Returns:
            True if no peer is asking for the block any more, in which case it
            goes back to ``MISSING`` and can be handed out again.
        """
        requesters = self._requesters.get(offset)
        if requesters is None:
            return False
        requesters.discard(peer)
        if requesters or self._states[offset] is not BlockState.REQUESTED:
            return False
        self._states[offset] = BlockState.MISSING
        return True

    def add_block(self, offset: int, data: bytes, *, source: str | None = None) -> bool:
        """Accept a block of data.

        Args:
            offset: Offset within the piece the data belongs at.
            data: The block bytes.
            source: Identity of the peer that sent it, for statistics.

        Returns:
            True if the block was stored, False if it was not wanted: unknown
            offset, wrong length, or a duplicate of a block already received.
            Duplicates are normal in endgame mode and are simply dropped.
        """
        block = self._block_starting_at(offset)
        if block is None or len(data) != block.length:
            return False
        if self._states[offset] is BlockState.RECEIVED:
            return False

        if self._buffer is None:
            self._buffer = bytearray(self.size)
        self._buffer[offset : offset + len(data)] = data
        self._states[offset] = BlockState.RECEIVED
        self._requesters[offset].clear()
        if source is not None and source not in self.sources:
            self.sources.append(source)
        if self.state in {PieceState.MISSING, PieceState.REQUESTED}:
            self.state = PieceState.DOWNLOADING
        if self.complete:
            self.state = PieceState.DOWNLOADED
        return True

    def data(self) -> bytes | None:
        """The assembled piece, or ``None`` while blocks are still missing."""
        if not self.complete or self._buffer is None:
            return None
        return bytes(self._buffer)

    def release(self) -> None:
        """Free the assembly buffer once the piece has been stored."""
        self._buffer = None

    # ------------------------------------------------------------ transitions

    def mark_verifying(self) -> None:
        """Move to ``VERIFYING`` (the hash is about to be checked)."""
        self.state = PieceState.VERIFYING

    def mark_verified(self) -> None:
        """Move to ``VERIFIED`` and drop the buffer — the bytes are on disk."""
        self.state = PieceState.VERIFIED
        self._buffer = None

    def mark_failed(self) -> None:
        """Move to ``FAILED`` and throw every block away.

        The data is dropped completely, not partially: a retry that reused any
        of these bytes could rebuild exactly the same corrupt piece. ``FAILED``
        is observable for one moment — long enough to report it and to penalise
        the peer that sent it — before :meth:`reset` puts the piece back in the
        pool as ``MISSING``.
        """
        self.failures += 1
        self._clear()
        self.state = PieceState.FAILED

    def reset(self) -> None:
        """Put the piece back in the pool: no blocks, no requesters, ``MISSING``."""
        self._clear()
        self.state = PieceState.MISSING

    def _clear(self) -> None:
        """Drop the buffer and forget every block's progress and requesters."""
        self._buffer = None
        for offset in self._states:
            self._states[offset] = BlockState.MISSING
            self._requesters[offset].clear()

    # ------------------------------------------------------------------ views

    def block_states(self) -> dict[BlockKey, BlockState]:
        """State of every block, for the UI's piece matrix."""
        return {(self.index, offset): state for offset, state in self._states.items()}

    def _block_starting_at(self, offset: int) -> Block | None:
        for block in self.blocks:
            if block.offset == offset:
                return block
        return None

    def __str__(self) -> str:
        return (
            f"Piece(index={self.index}, state={self.state.value}, "
            f"{self.received_blocks}/{self.block_count} blocks)"
        )


def build_pieces(
    *,
    sizes: Iterable[int],
    hashes: Iterable[bytes],
    block_size: int = DEFAULT_BLOCK_SIZE,
    verified: Container[int] = (),
) -> tuple[Piece, ...]:
    """Build the piece map for a torrent.

    Args:
        sizes: Size of each piece, in order.
        hashes: Expected SHA-1 of each piece, in order.
        block_size: Nominal block size for requests.
        verified: Indices already verified on disk (from resume state); those
            pieces start out ``VERIFIED`` instead of ``MISSING``.

    Returns:
        The pieces, in torrent order.
    """
    pieces: list[Piece] = []
    for index, (size, piece_hash) in enumerate(zip(sizes, hashes, strict=True)):
        piece = Piece(index=index, size=size, expected_hash=piece_hash, block_size=block_size)
        if index in verified:
            piece.mark_verified()
        pieces.append(piece)
    return tuple(pieces)
