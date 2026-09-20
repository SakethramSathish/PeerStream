"""Coordinates peers, requests, verification and storage for one torrent.

This is the class that turns the parts into a download. It owns the piece map,
watches the swarm through the peer manager, asks the scheduler what to request,
hands arriving blocks to the pieces they belong to, and pushes completed pieces
into storage — where the SHA-1 check decides whether they are kept.

The loop is deliberately small:

::

    pump()  →  ask the scheduler for requests → send them
    block arrives → piece.add_block() → piece complete? → verify + store
    peer leaves → its requests go back into the pool
    request unanswered for block_timeout → same

Everything else (selection strategy, endgame, pipelining, verification) lives
in the modules that do one job each, so this one stays readable.

Concurrency notes that matter:

* ``on_block`` is called **synchronously** from a peer's read loop, so it only
  does bookkeeping and schedules the slow part (hashing + writing) as a task.
* Verification tasks run concurrently, bounded by the verifier's thread pool
  and by the fact that only whole pieces are ever in flight.
* The loop wakes on events (block arrived, peer gone, piece stored) rather than
  polling on a timer, so an idle swarm costs nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, replace
from typing import Final

from app.core.config import DownloadConfig, NetworkConfig
from app.core.constants import DEFAULT_BLOCK_TIMEOUT
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.download.block import BlockKey
from app.download.endgame import EndgameTracker
from app.download.piece import Piece, PieceState, build_pieces
from app.download.scheduler import Scheduler, peer_key
from app.download.selector import PieceSelector
from app.peer.bitfield import Bitfield, PieceAvailability
from app.peer.errors import PeerError
from app.peer.messages import Cancel
from app.peer.messages import Piece as PieceMessage
from app.storage.errors import PieceHashMismatch
from app.storage.manager import StorageManager
from app.torrent.metadata import Torrent

logger = logging.getLogger(__name__)

# How often the loop runs when nothing else wakes it. Long enough to be free,
# short enough that a peer unchoking is noticed promptly.
IDLE_TICK_SECONDS: Final[float] = 0.5
# How long pump() waits after sending requests before looking again when the
# swarm is busy — the loop is event-driven, so this is only a backstop.
BUSY_TICK_SECONDS: Final[float] = 0.05
# How long :meth:`stop` waits for cancels that are still in flight. A cancel is
# a courtesy to a peer, not a debt: if the socket will not take it, closing the
# connection says the same thing, and a client that cannot stop is broken.
SEND_DRAIN_TIMEOUT: Final[float] = 1.0
# How long a checkpoint waits for verification before giving up waiting. Long
# enough to finish any piece, short enough that a stalled one cannot take the
# client with it: a checkpoint that never returns is worse than a piece that
# has to be fetched again.
FLUSH_TIMEOUT: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class DownloadStats:
    """What a download has actually done.

    Every field is counted, never estimated. ``wasted_bytes`` is the honest
    cost of the network: bytes that arrived and were thrown away, either
    because another peer won the race (endgame) or because a piece failed its
    hash.
    """

    blocks_received: int = 0
    blocks_duplicate: int = 0
    blocks_unsolicited: int = 0
    requests_sent: int = 0
    requests_expired: int = 0
    requests_cancelled: int = 0
    pieces_verified: int = 0
    pieces_failed: int = 0
    wasted_bytes: int = 0
    peer_penalties: int = 0

    @property
    def useful_blocks(self) -> int:
        """Blocks that ended up in a verified piece."""
        return self.blocks_received - self.blocks_duplicate - self.blocks_unsolicited


class DownloadManager:
    """Downloads one torrent from a swarm into storage.

    Args:
        torrent: The torrent to download.
        storage: Where verified pieces are written.
        peers: Peer manager holding the live connections.
        config: Download behaviour (pipelining, timeouts, strategy, endgame).
        network: Network settings; only ``request_timeout`` is consulted, as a
            fallback block timeout.
        event_bus: Optional bus that receives piece and torrent events.
        selector: Piece ordering; built from ``config`` when omitted.
        block_timeout: Override for how long a request may go unanswered.

    Example:
        >>> manager = DownloadManager(torrent, storage=storage, peers=peers)
        >>> await manager.start()                      # doctest: +SKIP
        >>> await manager.wait_until_complete()        # doctest: +SKIP
        True
    """

    def __init__(
        self,
        torrent: Torrent,
        *,
        storage: StorageManager,
        peers: object,
        config: DownloadConfig | None = None,
        network: NetworkConfig | None = None,
        event_bus: EventBus | None = None,
        selector: PieceSelector | None = None,
        block_timeout: float | None = None,
    ) -> None:
        self._torrent = torrent
        self._storage = storage
        self._peers = peers
        self._config = config or DownloadConfig()
        self._network = network
        self._event_bus = event_bus
        self._block_timeout = (
            block_timeout
            if block_timeout is not None
            else (network.request_timeout if network else DEFAULT_BLOCK_TIMEOUT)
        )

        self._pieces = build_pieces(
            sizes=[torrent.piece_size(index) for index in range(torrent.piece_count)],
            hashes=[torrent.piece_hash(index) for index in range(torrent.piece_count)],
            block_size=self._config.block_size,
            verified=set(storage.completed_pieces),
        )
        self._availability = PieceAvailability(torrent.piece_count)
        self._selector = selector or PieceSelector(
            torrent.piece_count, strategy=self._config.piece_strategy
        )
        self._endgame = EndgameTracker(
            enabled=self._config.endgame_enabled, threshold=self._config.endgame_threshold
        )
        self._scheduler = Scheduler(
            self._pieces, config=self._config, selector=self._selector, endgame=self._endgame
        )
        self._stats = DownloadStats()
        self._penalties: dict[str, int] = {}
        self._tracked: dict[str, Bitfield] = {}
        # Peer key → (pieces the peer held, pieces we had verified) at the time
        # interest was last decided, so a steady swarm is not re-scanned on
        # every pass. Entries are dropped when a peer announces a new piece.
        self._interest: dict[str, tuple[int, int]] = {}
        self._verify_tasks: set[asyncio.Task[None]] = set()
        # Fire-and-forget network sends (cancels). Deliberately *not* in
        # ``_verify_tasks``: a checkpoint that waits for the disk must never
        # find itself waiting for a socket.
        self._send_tasks: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._started_at: float | None = None
        self._completed_at: float | None = None

    # ------------------------------------------------------------- properties

    @property
    def torrent(self) -> Torrent:
        """The torrent being downloaded."""
        return self._torrent

    @property
    def storage(self) -> StorageManager:
        """Where verified pieces go."""
        return self._storage

    @property
    def config(self) -> DownloadConfig:
        """Download settings in force."""
        return self._config

    @property
    def pieces(self) -> tuple[Piece, ...]:
        """Every piece, in torrent order."""
        return self._pieces

    @property
    def scheduler(self) -> Scheduler:
        """The request scheduler."""
        return self._scheduler

    @property
    def availability(self) -> PieceAvailability:
        """How many connected peers hold each piece."""
        return self._availability

    @property
    def stats(self) -> DownloadStats:
        """Counted, never estimated, progress and waste."""
        return self._stats

    @property
    def peer_penalties(self) -> dict[str, int]:
        """Bad pieces attributed to each peer (by ``host:port``)."""
        return dict(self._penalties)

    @property
    def verified_pieces(self) -> tuple[int, ...]:
        """Pieces verified and on disk."""
        return tuple(piece.index for piece in self._pieces if piece.finished)

    @property
    def missing_pieces(self) -> tuple[int, ...]:
        """Pieces still to download."""
        return tuple(piece.index for piece in self._pieces if not piece.finished)

    @property
    def complete(self) -> bool:
        """True when every piece is verified and stored."""
        return all(piece.finished for piece in self._pieces)

    @property
    def progress(self) -> float:
        """Fraction of the torrent's bytes verified, in ``[0.0, 1.0]``."""
        if self._torrent.total_length == 0:
            return 1.0
        return self._storage.downloaded_bytes / self._torrent.total_length

    @property
    def elapsed(self) -> float:
        """Seconds since the download started (0 before :meth:`start`)."""
        if self._started_at is None:
            return 0.0
        end = self._completed_at or time.monotonic()
        return end - self._started_at

    @property
    def running(self) -> bool:
        """Whether the scheduling loop is alive."""
        return self._loop_task is not None and not self._loop_task.done()

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start the scheduling loop."""
        if self._loop_task is not None:
            return
        self._started_at = time.monotonic()
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._loop(), name="download")

    async def stop(self, *, timeout: float = FLUSH_TIMEOUT) -> None:
        """Stop the loop and wait for outstanding verification to finish.

        Args:
            timeout: How long verification may take before stopping anyway.
                Stopping is never allowed to hang: a client that cannot be
                stopped is worse than one that loses a piece.
        """
        self._stop.set()
        self._wake.set()
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.flush(timeout=timeout)
        await self._drain_sends()

    async def _drain_sends(self) -> None:
        """Give outstanding cancels a moment, then abandon them."""
        if not self._send_tasks:
            return
        _done, pending = await asyncio.wait(tuple(self._send_tasks), timeout=SEND_DRAIN_TIMEOUT)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def __aenter__(self) -> DownloadManager:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def _loop(self) -> None:
        """Keep the swarm's pipelines full until the torrent is done."""
        while not self._stop.is_set():
            try:
                sent = await self.pump()
            except Exception as exc:  # noqa: BLE001 - the loop must not die
                # CancelledError is not an Exception, so shutdown still works.
                logger.warning("scheduling pass failed: %s", exc)
                sent = 0
            if self.complete:
                await self._finish()
                return
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=BUSY_TICK_SECONDS if sent else IDLE_TICK_SECONDS
                )
            except TimeoutError:
                continue
            finally:
                self._wake.clear()

    async def _finish(self) -> None:
        """Wrap up: persist progress and announce completion."""
        self._completed_at = time.monotonic()
        path = await self._storage.save_resume()
        self._emit(
            EventType.TORRENT_COMPLETED,
            f"download complete: {self._torrent.name} in {self.elapsed:.1f}s",
            data={
                "pieces": len(self._pieces),
                "elapsed": round(self.elapsed, 3),
                "wasted_bytes": self._stats.wasted_bytes,
                "resume_path": str(path),
            },
        )
        logger.info(
            "download complete: %s (%d pieces, %.1fs, %d wasted bytes)",
            self._torrent.name,
            len(self._pieces),
            self.elapsed,
            self._stats.wasted_bytes,
        )

    async def pump(self) -> int:
        """Run one scheduling pass.

        Returns:
            How many requests were sent.
        """
        live = self._live_peers()
        self._sync_peers(live)
        self._reclaim_expired()
        await self._update_interests(live)

        assignments = self._scheduler.plan(live, availability=self._availability)
        sent = 0
        for assignment in assignments:
            try:
                await assignment.peer.send(assignment.request)
            except PeerError as exc:
                logger.debug("could not send to %s: %s", peer_key(assignment.peer), exc)
                self._scheduler.note_orphaned(
                    assignment.peer, (assignment.request.index, assignment.request.begin)
                )
                continue
            sent += 1
            self._emit(
                EventType.PIECE_REQUESTED,
                f"requested {assignment.request}",
                level=logging.DEBUG,
                data={
                    "index": assignment.request.index,
                    "begin": assignment.request.begin,
                    "length": assignment.request.length,
                    "peer": peer_key(assignment.peer),
                },
            )
        if sent:
            self._stats = replace(self._stats, requests_sent=self._stats.requests_sent + sent)
        return sent

    def _live_peers(self) -> list[object]:
        """Connected peers from the peer manager."""
        connections = getattr(self._peers, "connections", ())
        return [peer for peer in connections if bool(getattr(peer, "connected", False))]

    def _sync_peers(self, live: list[object]) -> None:
        """Track arrivals and departures, keeping availability honest."""
        live_keys = {peer_key(peer) for peer in live}
        for key in list(self._tracked):
            if key not in live_keys:
                self._availability.remove(self._tracked.pop(key))
                gone = self._scheduler.peer_for(key)
                if gone is not None:
                    self._scheduler.drop_peer(gone)
        for peer in live:
            key = peer_key(peer)
            if key in self._tracked:
                continue
            bitfield = getattr(peer, "bitfield", None)
            if bitfield is None:
                continue
            self._tracked[key] = bitfield
            self._availability.add(bitfield)
            self._scheduler.register(peer)

    async def _update_interests(self, live: list[object]) -> None:
        """Tell each peer whether we want anything it has.

        A peer only unchokes a client that has said ``interested``, so without
        this no download would ever start. The reverse matters too: telling a
        peer we are not interested is how it stops wasting a slot on us.
        """
        wanted = self._scheduler.wanted()
        verified = len(self.verified_pieces)
        for peer in live:
            bitfield = getattr(peer, "bitfield", None)
            session = getattr(peer, "session", None)
            if bitfield is None or session is None:
                continue
            key = peer_key(peer)
            signature = (bitfield.count, verified)
            if self._interest.get(key) == signature:
                continue
            interesting = any(wanted.has(index) for index in bitfield.indices())
            if interesting != bool(getattr(session, "am_interested", False)):
                try:
                    await peer.send_interested(interesting)  # type: ignore[attr-defined]
                except PeerError as exc:
                    logger.debug("could not update interest for %s: %s", key, exc)
                    continue
            self._interest[key] = signature

    def _reclaim_expired(self) -> None:
        """Take back blocks no peer answered in time."""
        expired = self._scheduler.expire(self._block_timeout)
        for keys in expired.values():
            self._stats = replace(
                self._stats, requests_expired=self._stats.requests_expired + len(keys)
            )
        if expired:
            logger.debug("reclaimed %d unanswered request(s)", sum(map(len, expired.values())))

    # ------------------------------------------------------------- peer events

    def on_block(self, peer: object, message: PieceMessage) -> None:
        """Handle a ``piece`` message from a peer.

        Called synchronously from the peer's read loop, so this never does IO:
        the block is stored in its piece, endgame duplicates are cancelled, and
        only a finished piece is handed to :meth:`_verify` as a task.

        Args:
            peer: The connection the block came from, so the block can be
                credited and (if it turns out to be corrupt) blamed.
            message: The decoded block.
        """
        if message.index >= len(self._pieces):
            self._count_unsolicited(message)
            return
        piece = self._pieces[message.index]
        if piece.finished or piece.state is PieceState.VERIFYING:
            # The piece is already stored, or being stored. A copy we asked for
            # is an endgame loser; anything else arrived uninvited.
            key = (message.index, message.begin)
            if self._scheduler.has_asked(peer, key):
                self._scheduler.note_received(peer, key)
                self._count_duplicate(message)
            else:
                self._count_unsolicited(message)
            return

        if not piece.add_block(message.begin, message.data, source=peer_key(peer)):
            # A duplicate (endgame lost the race) or data at a block boundary
            # we never asked about. Either way it is waste, not progress.
            self._count_duplicate(message)
            return

        self._stats = replace(self._stats, blocks_received=self._stats.blocks_received + 1)
        self._emit(
            EventType.PIECE_BLOCK_RECEIVED,
            f"piece {message.index} block at {message.begin} ({len(message.data)} bytes)",
            level=logging.DEBUG,
            data={
                "index": message.index,
                "begin": message.begin,
                "length": len(message.data),
                "peer": peer_key(peer),
            },
        )

        for loser in self._scheduler.note_received(peer, (message.index, message.begin)):
            self._cancel(loser, (message.index, message.begin))

        if piece.complete:
            self._emit(
                EventType.PIECE_DOWNLOADED,
                f"piece {piece.index} complete ({piece.size} bytes)",
                data={"index": piece.index, "size": piece.size},
            )
            # Endgame asked several peers for the same blocks; now that the
            # piece is whole the losers are pure waste, so cancel them.
            for loser, stale in self._scheduler.cancel_piece(piece):
                self._cancel(loser, stale)
            self._verify(piece)
        self._wake.set()

    def _count_duplicate(self, message: PieceMessage) -> None:
        """Account for a copy of a block we already have."""
        self._stats = replace(
            self._stats,
            blocks_duplicate=self._stats.blocks_duplicate + 1,
            wasted_bytes=self._stats.wasted_bytes + len(message.data),
        )

    def _count_unsolicited(self, message: PieceMessage) -> None:
        """Account for a block nobody asked for."""
        self._stats = replace(
            self._stats,
            blocks_unsolicited=self._stats.blocks_unsolicited + 1,
            wasted_bytes=self._stats.wasted_bytes + len(message.data),
        )

    def on_disconnect(self, peer: object, reason: str = "") -> None:
        """A peer went away: its outstanding requests go back in the pool."""
        keys = self._scheduler.drop_peer(peer)
        key = peer_key(peer)
        bitfield = self._tracked.pop(key, None)
        if bitfield is not None:
            self._availability.remove(bitfield)
        self._interest.pop(key, None)
        if keys:
            logger.debug("peer %s left with %d block(s) outstanding", key, len(keys))
        self._wake.set()

    def on_have(self, peer: object, index: int) -> None:
        """A peer announced a piece it did not have before."""
        if 0 <= index < len(self._availability):
            self._availability.add_piece(index)
        # The peer's piece set changed, so its interest must be re-decided.
        self._interest.pop(peer_key(peer), None)
        self._wake.set()

    def _cancel(self, peer: object, key: BlockKey) -> None:
        """Send a cancel for a block we no longer want from this peer."""
        index, begin = key
        length = min(self._config.block_size, self._torrent.piece_size(index) - begin)
        message = Cancel(index=index, begin=begin, length=length)
        pending = self._send_cancel(peer, message)
        try:
            task = asyncio.create_task(pending)
        except RuntimeError:  # no running loop (synchronous use in tests)
            pending.close()
            return
        task.add_done_callback(self._send_tasks.discard)
        self._send_tasks.add(task)
        self._stats = replace(self._stats, requests_cancelled=self._stats.requests_cancelled + 1)

    async def _send_cancel(self, peer: object, message: Cancel) -> None:
        with contextlib.suppress(PeerError, OSError):
            await peer.send(message)  # type: ignore[attr-defined]

    # ----------------------------------------------------------- verification

    def _verify(self, piece: Piece) -> None:
        """Schedule hashing and storing a completed piece."""
        piece.mark_verifying()
        pending = self._verify_piece(piece)
        try:
            task = asyncio.create_task(pending, name=f"verify-{piece.index}")
        except RuntimeError as exc:  # no running loop
            logger.warning("cannot verify piece %d: %s", piece.index, exc)
            pending.close()
            piece.reset()
            return
        self._verify_tasks.add(task)
        task.add_done_callback(self._verify_tasks.discard)

    async def _verify_piece(self, piece: Piece) -> None:
        """Hash a piece and write it to disk, or throw it away."""
        data = piece.data()
        if data is None:  # pragma: no cover - complete implies a full buffer
            piece.reset()
            return
        try:
            await self._storage.write_piece(piece.index, data)
        except PieceHashMismatch as exc:
            self._stats = replace(
                self._stats,
                pieces_failed=self._stats.pieces_failed + 1,
                wasted_bytes=self._stats.wasted_bytes + piece.size,
            )
            for source in piece.sources:
                self._penalties[source] = self._penalties.get(source, 0) + 1
                self._stats = replace(self._stats, peer_penalties=self._stats.peer_penalties + 1)
            logger.warning("piece %d failed verification: %s", piece.index, exc)
            self._emit(
                EventType.PIECE_FAILED,
                f"piece {piece.index} failed its hash; re-downloading",
                level=logging.WARNING,
                data={"index": piece.index, "sources": list(piece.sources)},
            )
            piece.mark_failed()
            piece.reset()  # back in the pool; the retry starts from nothing
            self._scheduler.note_piece_reset(piece)
        else:
            self._stats = replace(self._stats, pieces_verified=self._stats.pieces_verified + 1)
            piece.mark_verified()
            self._emit(
                EventType.PIECE_VERIFIED,
                f"piece {piece.index} verified ({piece.size} bytes)",
                data={"index": piece.index, "size": piece.size},
            )
            logger.debug("piece %d verified", piece.index)
        finally:
            piece.release()
            self._wake.set()

    # ------------------------------------------------------------------ waiting

    async def flush(self, *, timeout: float = FLUSH_TIMEOUT) -> None:
        """Wait for every piece currently being verified or stored.

        Used at checkpoints (shutdown, tests, progress snapshots) so that
        "verified" never lags behind "downloaded" by an unknown amount. It is a
        *disk* checkpoint: outstanding cancels are network traffic and are
        drained by :meth:`stop`, not by this.

        The wait is bounded. A piece that cannot be verified is reported and
        left for the scheduler to fetch again; a checkpoint that never returned
        would take the whole client with it.
        """
        deadline = time.monotonic() + timeout
        while self._verify_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "checkpoint timed out with %d piece(s) still verifying: %s",
                    len(self._verify_tasks),
                    ", ".join(sorted(task.get_name() for task in self._verify_tasks)),
                )
                return
            _done, _pending = await asyncio.wait(tuple(self._verify_tasks), timeout=remaining)

    async def wait_until_complete(self, *, timeout: float = 60.0, interval: float = 0.02) -> bool:
        """Wait for the download to finish.

        Args:
            timeout: Seconds to wait before giving up.
            interval: Poll interval.

        Returns:
            True if the torrent completed, False on timeout.
        """
        deadline = time.monotonic() + timeout
        while not self.complete:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(interval)
        return True

    # ------------------------------------------------------------------ events

    def _emit(
        self,
        event_type: EventType,
        message: str,
        *,
        level: int = logging.INFO,
        data: dict[str, object] | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        self._event_bus.emit(
            make_event(
                event_type,
                message=message,
                torrent_id=self._torrent.hex_info_hash,
                level=level,
                data=data,
            )
        )
