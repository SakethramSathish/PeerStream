"""Request scheduling: which peer fetches which block next (FR-06, TRD §23).

The scheduler is the engine's traffic controller. Its job is to keep every
willing peer's pipeline full without asking for the same block twice, and to
decide what happens to a request when its peer disappears.

Three rules drive it:

* **Finish what you start.** Pieces already in progress are offered before new
  ones, so a torrent completes pieces steadily instead of leaving a hundred
  90 %-finished pieces in memory.
* **Respect each peer's pipeline.** ``max_outstanding_requests`` blocks are
  kept in flight per peer. Peers with the shortest queue are filled first, so
  work spreads to whoever is fastest rather than piling onto the first peer.
* **Only ask peers for what they have.** The peer's bitfield is the authority;
  asking for a piece a peer does not hold is how clients get choked.

The scheduler is deliberately *not* async and does not send anything:
:meth:`Scheduler.plan` returns a list of
:class:`~app.peer.messages.Request` messages for the caller to send. Every
decision it makes is therefore testable with plain objects.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import DownloadConfig
from app.download.block import Block, BlockKey, BlockState
from app.download.endgame import EndgameTracker
from app.download.piece import Piece, PieceState
from app.download.selector import PieceSelector
from app.peer.bitfield import Bitfield, PieceAvailability
from app.peer.messages import Request

logger = logging.getLogger(__name__)


class PeerView(Protocol):
    """What the scheduler needs from a peer connection.

    :class:`app.peer.connection.PeerConnection` satisfies this; tests use small
    stand-ins with the same three attributes.
    """

    @property
    def address(self) -> Any:
        """Peer endpoint, used to derive a stable identity."""

    @property
    def bitfield(self) -> Bitfield:
        """Pieces the peer holds."""

    @property
    def choked(self) -> bool:
        """Whether the peer is choking us."""

    @property
    def connected(self) -> bool:
        """Whether the socket is still up."""


def peer_key(peer: Any) -> str:
    """A stable identity for a peer, used for request bookkeeping."""
    address = getattr(peer, "address", None)
    host = getattr(address, "host", None)
    port = getattr(address, "port", None)
    if host is not None and port is not None:
        return f"{host}:{port}"
    return str(id(peer))


@dataclass(slots=True)
class PeerSlot:
    """One peer's outstanding requests.

    Attributes:
        key: Stable identity of the peer.
        outstanding: ``block key → when the request was sent``, for stall
            detection.
    """

    key: str
    outstanding: dict[BlockKey, float] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.outstanding)

    def can_request(self, limit: int) -> bool:
        """Whether the pipeline has room for another request."""
        return len(self.outstanding) < limit

    def add(self, key: BlockKey, now: float) -> bool:
        """Record a request. Returns False if the block is already pending."""
        if key in self.outstanding:
            return False
        self.outstanding[key] = now
        return True

    def discard(self, key: BlockKey) -> bool:
        """Remove a request. Returns True if it was there."""
        return self.outstanding.pop(key, None) is not None

    def stamp(self, key: BlockKey) -> float | None:
        """When the request for ``key`` was sent, if it is outstanding."""
        return self.outstanding.get(key)

    def clear(self) -> tuple[BlockKey, ...]:
        """Drop everything outstanding (peer gone). Returns what was dropped."""
        keys = tuple(self.outstanding)
        self.outstanding.clear()
        return keys

    def expired(self, timeout: float, now: float) -> tuple[BlockKey, ...]:
        """Blocks requested more than ``timeout`` seconds ago."""
        return tuple(key for key, sent_at in self.outstanding.items() if now - sent_at >= timeout)


@dataclass(frozen=True, slots=True)
class Assignment:
    """One request, for one peer."""

    peer: Any
    request: Request


class Scheduler:
    """Decides what to request, from whom, and when to give up.

    Args:
        pieces: The torrent's pieces, indexed by piece index.
        config: Pipeline depth, block timeout and endgame settings.
        selector: Piece ordering (sequential / rarest-first / random).
        endgame: Tracker that allows duplicate requests near the end.
    """

    def __init__(
        self,
        pieces: Sequence[Piece],
        *,
        config: DownloadConfig | None = None,
        selector: PieceSelector | None = None,
        endgame: EndgameTracker | None = None,
    ) -> None:
        self._pieces = {piece.index: piece for piece in pieces}
        self._config = config or DownloadConfig()
        self._selector = selector or PieceSelector(
            len(pieces), strategy=self._config.piece_strategy
        )
        self._endgame = endgame or EndgameTracker(
            enabled=self._config.endgame_enabled, threshold=self._config.endgame_threshold
        )
        self._slots: dict[str, PeerSlot] = {}
        # Block keys each peer has been asked for and not yet delivered, so a
        # copy that arrives after a cancel is still recognised as ours.
        self._asked: dict[str, set[BlockKey]] = {}
        self._peers: dict[str, Any] = {}
        self._unclaimed: int | None = None

    # ------------------------------------------------------------- properties

    @property
    def pieces(self) -> dict[int, Piece]:
        """The pieces being downloaded, by index."""
        return self._pieces

    @property
    def selector(self) -> PieceSelector:
        """Piece ordering in force."""
        return self._selector

    @property
    def endgame(self) -> EndgameTracker:
        """Endgame tracker in force."""
        return self._endgame

    @property
    def config(self) -> DownloadConfig:
        """Download settings in force."""
        return self._config

    def slot(self, peer: Any) -> PeerSlot:
        """The peer's pipeline, created on first use."""
        return self._slots.setdefault(peer_key(peer), PeerSlot(key=peer_key(peer)))

    def peer_for(self, key: str) -> Any:
        """The peer object registered under ``key``, if it is still known."""
        return self._peers.get(key)

    def in_flight_for(self, key: str) -> int:
        """How many requests are outstanding with one peer.

        Counted, not estimated: it is the length of that peer's pipeline, which
        is what "how hard is this peer working" means. Unknown peers have no
        pipeline and therefore zero.
        """
        slot = self._slots.get(key)
        return 0 if slot is None else len(slot)

    def register(self, peer: Any) -> None:
        """Start tracking a peer (called when a connection opens)."""
        self._peers[peer_key(peer)] = peer
        self.slot(peer)

    def forget(self, peer: Any) -> None:
        """Stop tracking a peer entirely."""
        key = peer_key(peer)
        self._peers.pop(key, None)
        self._slots.pop(key, None)
        self._asked.pop(key, None)

    # ------------------------------------------------------------------ state

    def outstanding_blocks(self) -> int:
        """Blocks missing from pieces that are in progress."""
        return sum(piece.missing_blocks for piece in self._pieces.values() if piece.in_progress)

    def unclaimed_blocks(self) -> int:
        """Blocks that are missing and nobody has asked for yet.

        This is the number endgame watches. Not "how much is left" but "how
        much *unassigned* work is left": once it falls below the threshold,
        peers are about to run out of things to do, and racing for the last
        blocks beats queueing behind the peer that happens to hold them.

        The count is maintained incrementally, because it is consulted for
        every planned request and recomputing it would mean walking every
        block of the torrent each time.
        """
        if self._unclaimed is None:
            self._unclaimed = sum(
                piece.unclaimed_blocks for piece in self._pieces.values() if not piece.finished
            )
        return self._unclaimed

    def _mark_asked(self, peer: str, key: BlockKey) -> None:
        """Remember that a block was requested, until it arrives or is forgotten."""
        self._asked.setdefault(peer, set()).add(key)

    def _forget_piece(self, index: int) -> None:
        """Stop remembering requests for a piece that is finished or reset."""
        for asked in self._asked.values():
            asked.difference_update(key for key in tuple(asked) if key[0] == index)

    def _prune_asked(self) -> None:
        """Drop history for pieces that are already on disk.

        A copy can arrive a moment after its piece is finished — that is still
        waste we caused and it is still recognised as such — but remembering
        every block ever requested would grow without bound, so the history is
        cleared once the piece is done and the swarm has moved on.
        """
        for asked in self._asked.values():
            for key in tuple(asked):
                piece = self._pieces.get(key[0])
                if piece is not None and piece.finished:
                    asked.discard(key)

    def _stale(self, key: BlockKey, now: float) -> bool:
        """Whether the outstanding request for ``key`` has had its chance.

        Endgame only races a block whose first request has gone unanswered for
        at least the configured delay; racing a fresh request just buys the
        same bytes twice.
        """
        stamps = [stamp for slot in self._slots.values() if (stamp := slot.stamp(key)) is not None]
        if not stamps:
            return True
        return now - min(stamps) >= self._config.endgame_delay

    def note_piece_reset(self, piece: Piece) -> None:
        """A piece went back to nothing (hash failure): recount on demand."""
        self._unclaimed = None
        self._forget_piece(piece.index)

    def _claim(self, piece: Piece, offset: int, peer: str) -> None:
        """Give a block to a peer, adjusting the unclaimed count."""
        if self._unclaimed is not None and piece.block_state(offset) is BlockState.MISSING:
            self._unclaimed -= 1
        piece.mark_requested(offset, peer)

    def _unclaim(self, piece: Piece, offset: int, peer: str) -> None:
        """Take a block back from a peer, adjusting the unclaimed count."""
        if piece.mark_orphaned(offset, peer) and self._unclaimed is not None:
            self._unclaimed += 1

    def wanted(self) -> Bitfield:
        """Bitfield of pieces still needed (not verified, not being verified)."""
        wanted = Bitfield(len(self._pieces))
        for index, piece in self._pieces.items():
            if not piece.finished and piece.state is not PieceState.VERIFYING:
                wanted.set(index)
        return wanted

    def in_flight(self) -> tuple[BlockKey, ...]:
        """Every block currently requested from any peer."""
        return tuple(key for slot in self._slots.values() for key in slot.outstanding)

    # ------------------------------------------------------------------ planning

    def plan(
        self,
        peers: Sequence[Any],
        *,
        availability: PieceAvailability | None = None,
        limit: int | None = None,
    ) -> list[Assignment]:
        """Choose the next requests.

        Args:
            peers: Connected peers to consider; unusable ones are skipped.
            availability: Peer counts per piece, for rarest-first ordering.
            limit: Override the per-peer pipeline depth.

        Returns:
            One assignment per request to send. Peers are filled in order of
            how empty their pipeline is, so work spreads across the swarm.
        """
        now = time.monotonic()
        capacity = limit if limit is not None else self._config.max_outstanding_requests
        assignments: list[Assignment] = []
        self._prune_asked()

        for peer in sorted(peers, key=lambda candidate: len(self.slot(candidate))):
            if not self._usable(peer):
                continue
            # Planning for a peer means tracking it: cancels and timeouts later
            # need the object, not just its key.
            self.register(peer)
            slot = self.slot(peer)
            while slot.can_request(capacity):
                # Re-read the count each time: claiming blocks is what makes
                # endgame activate part-way through a planning pass.
                block = self._next_block(
                    peer,
                    availability=availability,
                    outstanding=self.unclaimed_blocks(),
                )
                if block is None:
                    break
                key = block.key
                if not slot.add(key, now):
                    break  # defensive: the block was already pending for this peer
                self._claim(self._pieces[block.index], block.offset, slot.key)
                self._endgame.note_request(key, slot.key)
                self._mark_asked(slot.key, key)
                assignments.append(
                    Assignment(
                        peer=peer,
                        request=Request(index=block.index, begin=block.offset, length=block.length),
                    )
                )
        return assignments

    def _usable(self, peer: Any) -> bool:
        """A peer is usable when it is connected, unchoking us, and has told
        us what it holds — a peer that has not sent a bitfield cannot be asked
        for a specific piece."""
        if not bool(getattr(peer, "connected", True)):
            return False
        if bool(getattr(peer, "choked", False)):
            return False
        return getattr(peer, "bitfield", None) is not None

    def _next_block(
        self,
        peer: Any,
        *,
        availability: PieceAvailability | None,
        outstanding: int,
    ) -> Block | None:
        """Pick the next block for a peer, or ``None`` if it has nothing to do."""
        key = peer_key(peer)
        bitfield = getattr(peer, "bitfield", None)

        racing = self._endgame.active(outstanding)
        now = time.monotonic()
        for piece in self._candidates(availability=availability):
            if bitfield is not None and not bitfield.has(piece.index):
                continue
            block = piece.requestable_block(key, allow_duplicates=racing)
            if block is None:
                continue
            if self._endgame.requesters(block.key) and not self._stale(block.key, now):
                # Somebody is already fetching this block and has barely had
                # time to answer; racing it now only buys the same bytes twice.
                continue
            return block
        return None

    def _candidates(self, *, availability: PieceAvailability | None) -> list[Piece]:
        """Pieces worth offering to a peer: work in progress first, then new."""
        started: list[Piece] = []
        for piece in self._pieces.values():
            if piece.in_progress or (
                piece.state is PieceState.MISSING and piece.received_blocks > 0
            ):
                started.append(piece)
        # Nearly finished pieces first: completing a piece frees its buffer and
        # gets bytes onto disk sooner.
        started.sort(key=lambda piece: (-piece.received_blocks, piece.index))

        fresh_indices = self._selector.select(self.wanted(), availability=availability)
        fresh = [
            self._pieces[index]
            for index in fresh_indices
            if index in self._pieces and not self._pieces[index].in_progress
        ]
        return started + fresh

    # ------------------------------------------------------------ bookkeeping

    def note_received(self, peer: Any, key: BlockKey) -> tuple[Any, ...]:
        """A block arrived from ``peer``.

        Returns:
            The other peers still waiting for that block, which should now be
            sent a ``cancel``. Empty outside endgame mode.
        """
        self.slot(peer).discard(key)
        self._asked.get(peer_key(peer), set()).discard(key)
        others = self._endgame.note_received(key, peer_key(peer))
        peers: list[Any] = []
        for other in others:
            self._slots.get(other, PeerSlot(key=other)).discard(key)
            peer_object = self._peers.get(other)
            if peer_object is not None:
                peers.append(peer_object)
        return tuple(peers)

    def has_asked(self, peer: Any, key: BlockKey) -> bool:
        """Whether a copy of this block from ``peer`` is one we asked for.

        This is how a late endgame copy is told apart from data nobody asked
        for: both arrive for a piece we have already finished, but only one of
        them is our own request coming home late — possibly after we gave up
        and cancelled it, which is still waste we caused.
        """
        return key in self._asked.get(peer_key(peer), frozenset())

    def cancel_piece(self, piece: Piece) -> tuple[tuple[Any, BlockKey], ...]:
        """Stop asking the swarm for blocks of a piece we have finished.

        Endgame mode deliberately asks several peers for the same block; the
        moment a piece is complete, every remaining request for it is wasted
        bandwidth on both sides, so it is cancelled.

        Returns:
            Pairs of ``(peer, block key)`` the caller should send a ``cancel``
            to, for peers we still hold an object for.
        """
        cancelled: list[tuple[Any, BlockKey]] = []
        for slot_key, slot in self._slots.items():
            for key in tuple(slot.outstanding):
                if key[0] != piece.index:
                    continue
                slot.discard(key)
                self._endgame.note_lost(key, slot_key)
                self._unclaim(piece, key[1], slot_key)
                peer_object = self._peers.get(slot_key)
                if peer_object is not None:
                    cancelled.append((peer_object, key))
        return tuple(cancelled)

    def note_orphaned(self, peer: Any, key: BlockKey) -> bool:
        """Give up on one request (cancel, timeout, or peer about to go)."""
        self.slot(peer).discard(key)
        lost = self._endgame.note_lost(key, peer_key(peer))
        if lost:
            piece = self._pieces.get(key[0])
            if piece is not None:
                self._unclaim(piece, key[1], peer_key(peer))
        return lost

    def drop_peer(self, peer: Any) -> tuple[BlockKey, ...]:
        """A peer is gone: return every block it owed us to the pool.

        Returns:
            The keys that were outstanding, so the caller can account for them.
        """
        keys = self.slot(peer).clear()
        key = peer_key(peer)
        for block_key in keys:
            self._endgame.note_lost(block_key, key)
            piece = self._pieces.get(block_key[0])
            if piece is not None:
                self._unclaim(piece, block_key[1], key)
        self.forget(peer)
        return keys

    def expire(self, timeout: float | None = None) -> dict[str, tuple[BlockKey, ...]]:
        """Recall requests that have gone unanswered for too long.

        Args:
            timeout: Override ``config.block_timeout``.

        Returns:
            ``peer key → keys`` for every request that timed out. The blocks
            are back in the pool, so the next :meth:`plan` can hand them to
            someone else.
        """
        limit = timeout if timeout is not None else self._config.block_timeout
        now = time.monotonic()
        expired: dict[str, tuple[BlockKey, ...]] = {}
        for key, slot in list(self._slots.items()):
            stale = slot.expired(limit, now)
            if not stale:
                continue
            peer = self._peers.get(key, slot)
            for block_key in stale:
                self.note_orphaned(peer, block_key)
            expired[key] = stale
        return expired
