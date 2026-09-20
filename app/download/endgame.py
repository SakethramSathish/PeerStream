"""Endgame mode: racing the last few blocks (TRD §27).

The end of a download is the slowest part. Near completion there is nothing
left to pipeline — a handful of blocks are outstanding, all of them held by
peers that are, statistically, slower than the ones that already finished their
share. Waiting for one slow peer to deliver the final block can double the
total download time.

So when the number of outstanding blocks drops below a threshold, the same
block is requested from several peers at once. The first copy to arrive wins;
the others are cancelled, and the duplicates that still show up are counted and
dropped rather than treated as an error — a duplicate block is endgame working
as designed, not a peer misbehaving.

The threshold is on *outstanding* blocks, not remaining pieces: it is the point
at which there is no longer enough work to keep every peer busy, which is
exactly when racing beats queueing.
"""

from __future__ import annotations

from app.core.constants import DEFAULT_ENDGAME_THRESHOLD
from app.download.block import BlockKey


class EndgameTracker:
    """Remembers who is fetching what, and decides when to duplicate.

    Args:
        enabled: Whether endgame mode may ever activate.
        threshold: Outstanding-block count at or below which blocks are
            requested from more than one peer.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        threshold: int = DEFAULT_ENDGAME_THRESHOLD,
    ) -> None:
        if threshold < 0:
            raise ValueError(f"endgame threshold must not be negative, got {threshold}")
        self._enabled = enabled
        self._threshold = threshold
        self._requesters: dict[BlockKey, set[str]] = {}
        self._duplicates = 0

    @property
    def enabled(self) -> bool:
        """Whether endgame mode may activate."""
        return self._enabled

    @property
    def threshold(self) -> int:
        """Outstanding-block count at which endgame activates."""
        return self._threshold

    @property
    def duplicates(self) -> int:
        """How many duplicate requests have been issued (blocks raced)."""
        return self._duplicates

    @property
    def in_flight(self) -> int:
        """Number of blocks with at least one outstanding request."""
        return len(self._requesters)

    def active(self, outstanding_blocks: int) -> bool:
        """Whether endgame is in force for a given amount of work left.

        Args:
            outstanding_blocks: Blocks still missing from pieces in progress.

        Returns:
            True when blocks may be requested from more than one peer.
        """
        return self._enabled and outstanding_blocks <= self._threshold

    def requesters(self, key: BlockKey) -> tuple[str, ...]:
        """Peers currently fetching a block, in stable order."""
        return tuple(sorted(self._requesters.get(key, set())))

    def allows(self, key: BlockKey, peer: str, *, outstanding_blocks: int) -> bool:
        """Whether ``peer`` may be given this block.

        A peer never gets the same block twice. Whether a *second* peer may
        have it at all depends on endgame being active.
        """
        requesters = self._requesters.get(key, set())
        if peer in requesters:
            return False
        if not requesters:
            return True
        return self.active(outstanding_blocks)

    def note_request(self, key: BlockKey, peer: str) -> None:
        """Record that ``peer`` is fetching ``key``.

        A request for a block somebody else is already fetching is a race —
        counted here, because this is where it actually happens.
        """
        requesters = self._requesters.setdefault(key, set())
        if requesters and peer not in requesters:
            self._duplicates += 1
        requesters.add(peer)

    def note_received(self, key: BlockKey, winner: str) -> tuple[str, ...]:
        """A block arrived: everyone else's request is now pointless.

        Args:
            key: The block that arrived.
            winner: The peer that delivered it.

        Returns:
            The other peers still waiting for the block — the ones that should
            receive a ``cancel``.
        """
        requesters = self._requesters.pop(key, set())
        requesters.discard(winner)
        return tuple(sorted(requesters))

    def note_lost(self, key: BlockKey, peer: str) -> bool:
        """A request was abandoned (peer gone, or timed out).

        Returns:
            True when nobody is fetching the block any more.
        """
        requesters = self._requesters.get(key)
        if requesters is None:
            return False
        requesters.discard(peer)
        if not requesters:
            del self._requesters[key]
            return True
        return False

    def forget(self, key: BlockKey) -> None:
        """Stop tracking a block entirely (its piece was reset or verified)."""
        self._requesters.pop(key, None)

    def clear(self) -> None:
        """Drop all tracking — used when a download is reset."""
        self._requesters.clear()
