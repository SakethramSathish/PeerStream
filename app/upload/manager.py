"""Upload engine: serving blocks, and deciding who deserves them (PRD FR-14).

Downloading is only half of BitTorrent. A client that never uploads is a
leech, and the protocol's answer to leeches is choking: every peer decides
who it serves, and reciprocity is what makes the whole thing work.

This module is the upload half. It does four jobs:

1. **Validate** every request. A request is untrusted input: the index, the
   offset and the length all come from a stranger, and all three are checked
   against the torrent before a single byte is read from disk.
2. **Queue** what passes, per peer, and drop it when the peer cancels, hangs
   up, or simply asks for more than one peer can reasonably hold.
3. **Serve** it — read from storage, pace it through the rate limiter, send it
   — and count every byte, because the choking policy is arithmetic on those
   counts.
4. **Choke** the rest, on a timer, via the pure policy in
   :mod:`~app.upload.choke`.

The manager never opens a socket. It is handed peer objects by the peer
manager and calls back into them, which is why the whole thing can be tested
against stand-ins.

Example::

    upload = UploadManager(torrent, storage=storage, peers=peers)
    peers.on_request = upload.on_request
    peers.on_cancel = upload.on_cancel
    peers.on_disconnect = upload.on_disconnect
    await upload.start()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from app.core.config import UploadConfig
from app.core.constants import MAX_BLOCK_SIZE
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.peer.bitfield import Bitfield
from app.peer.messages import Request
from app.storage.manager import StorageManager
from app.torrent import Torrent
from app.upload.choke import ChokeDecision, ChokePolicy, PeerAccounting
from app.upload.rate import TokenBucket

logger = logging.getLogger(__name__)

IDLE_TICK_SECONDS: float = 0.5


@dataclass(frozen=True, slots=True)
class UploadStats:
    """Everything the upload side has counted.

    Every field is a count of something that actually happened; nothing is
    extrapolated from a rate or estimated from a window.

    Attributes:
        blocks_served: Blocks sent to peers.
        bytes_uploaded: Payload bytes sent (excluding protocol overhead).
        requests_received: Requests that arrived, valid or not.
        requests_served: Requests that were answered with data.
        requests_dropped: Requests removed by a cancel, a timeout or a hang-up.
        requests_rejected: Requests refused, by reason.
        peers_unchoked: Peers currently allowed to download.
        queue_depth: Requests waiting to be served.
    """

    blocks_served: int = 0
    bytes_uploaded: int = 0
    requests_received: int = 0
    requests_served: int = 0
    requests_dropped: int = 0
    requests_rejected: dict[str, int] = field(default_factory=dict)
    peers_unchoked: int = 0
    queue_depth: int = 0

    @property
    def rejected_total(self) -> int:
        """Every request we refused, whatever the reason."""
        return sum(self.requests_rejected.values())


@dataclass(frozen=True, slots=True)
class QueuedRequest:
    """One request waiting to be served.

    Args:
        index: Piece index.
        begin: Offset within the piece.
        length: Bytes requested.
        queued_at: When it arrived, so a queue can be reaped.
    """

    index: int
    begin: int
    length: int
    queued_at: float

    @property
    def key(self) -> tuple[int, int]:
        """Identity of the block, for deduplication and cancels."""
        return (self.index, self.begin)


class UploadManager:
    """Serves blocks to peers and decides who is worth serving.

    Args:
        torrent: The torrent being shared; its geometry bounds every request.
        storage: Where the bytes come from.
        peers: The peer manager; anything exposing ``connections`` will do, so
            tests can pass a stand-in.
        config: Slots, rate limit, queue limits and choke timings.
        event_bus: Optional bus that receives upload and choke events.
        have: Bitfield of the pieces we hold. Empty by default, because
            claiming pieces we do not have would be a lie to the swarm.
    """

    def __init__(
        self,
        torrent: Torrent,
        *,
        storage: StorageManager,
        peers: Any,
        config: UploadConfig | None = None,
        event_bus: EventBus | None = None,
        have: Bitfield | None = None,
    ) -> None:
        self._torrent = torrent
        self._storage = storage
        self._peers = peers
        self._config = config or UploadConfig()
        self._event_bus = event_bus
        self._have = have if have is not None else Bitfield(torrent.piece_count)

        self._policy = ChokePolicy(config=self._config)
        self._bucket = TokenBucket(rate=float(self._config.max_upload_speed))
        self._queues: dict[str, deque[QueuedRequest]] = {}
        self._tracked: dict[str, Any] = {}
        self._decision: ChokeDecision = ChokeDecision()
        self._stats = UploadStats(requests_rejected={})
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Fairness cursor: the serve pass starts with a different peer each
        # time, so a peer with a deep queue cannot starve the others.
        self._rotation = 0
        self._serving: set[asyncio.Task[None]] = set()
        self._waker: asyncio.Task[None] | None = None
        self._last_download: dict[str, float] = {}
        self._downloaded: dict[str, int] = {}

    # ------------------------------------------------------------------ state

    @property
    def config(self) -> UploadConfig:
        """Slot count, rate limit and choke timings in force."""
        return self._config

    @property
    def running(self) -> bool:
        """Whether the background loop is awake."""
        return self._task is not None and not self._task.done()

    @property
    def stats(self) -> UploadStats:
        """A snapshot of what has been counted, including the current queue."""
        return replace(
            self._stats,
            peers_unchoked=len(self._decision.allowed),
            queue_depth=sum(len(queue) for queue in self._queues.values()),
        )

    @property
    def have(self) -> Bitfield:
        """The pieces we will admit to holding."""
        return self._have

    @property
    def decision(self) -> ChokeDecision:
        """The current choking decision."""
        return self._decision

    def unchoked_peers(self) -> tuple[str, ...]:
        """Peers currently allowed to download from us."""
        return self._decision.allowed

    def queued(self, peer: Any) -> tuple[QueuedRequest, ...]:
        """Requests waiting for one peer."""
        return tuple(self._queues.get(self._key(peer), deque()))

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start the choke/serve loop.

        Safe to call twice: an already-running manager is left alone.
        """
        if self.running:
            return
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="upload")

    async def stop(self) -> None:
        """Stop the loop and wait for in-flight sends to finish."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._serving:
            await asyncio.gather(*self._serving, return_exceptions=True)

    async def flush(self) -> None:
        """Serve everything currently queued, without waiting for the loop.

        Used by tests (and by anything that wants a deterministic answer now):
        it keeps passing until the queues are empty, sleeping briefly whenever
        the rate limiter says "not yet" rather than spinning.
        """
        while any(self._queues.values()):
            if await self.pump() > 0:
                continue
            if not self._serving:
                # Nothing went out and nothing is in flight: what is left is
                # queued for peers we are choking, and waiting would be forever.
                return
            await asyncio.sleep(0.01)
        if self._serving:
            await asyncio.gather(*self._serving, return_exceptions=True)

    async def __aenter__(self) -> UploadManager:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------- callbacks

    def on_request(self, peer: Any, message: Request) -> None:
        """A peer asked for a block.

        Everything that can be wrong with a request is checked here, and a
        rejected request is counted by reason rather than ignored: "how many
        peers are asking us for pieces we do not have" is a real question with
        a real answer.
        """
        self._stats = replace(self._stats, requests_received=self._stats.requests_received + 1)
        key = self._key(peer)
        self._track(peer)

        reason = self._reject_reason(peer, message, key=key)
        if reason is not None:
            self._reject(reason)
            return

        queue = self._queues.setdefault(key, deque())
        if any(item.key == (message.index, message.begin) for item in queue):
            self._reject("duplicate")
            return
        queue.append(
            QueuedRequest(
                index=message.index,
                begin=message.begin,
                length=message.length,
                queued_at=time.monotonic(),
            )
        )
        self._wake.set()

    def on_cancel(self, peer: Any, message: Request) -> None:
        """A peer took back a request."""
        queue = self._queues.get(self._key(peer))
        if queue is None:
            return
        before = len(queue)
        remaining = deque(item for item in queue if item.key != (message.index, message.begin))
        if len(remaining) == before:
            return
        self._queues[self._key(peer)] = remaining
        self._stats = replace(
            self._stats, requests_dropped=self._stats.requests_dropped + (before - len(remaining))
        )

    def on_disconnect(self, peer: Any, reason: str = "") -> None:
        """A peer went away: its queue goes with it."""
        key = self._key(peer)
        queue = self._queues.pop(key, None)
        dropped = len(queue) if queue else 0
        self._tracked.pop(key, None)
        self._last_download.pop(key, None)
        self._downloaded.pop(key, None)
        self._policy.forget(key)
        if dropped:
            self._stats = replace(
                self._stats, requests_dropped=self._stats.requests_dropped + dropped
            )
        logger.debug("upload: %s disconnected (%s), %d request(s) dropped", key, reason, dropped)

    def note_piece_verified(self, index: int) -> None:
        """A finished piece: admit it, and tell the swarm.

        A piece nobody knows about is a piece nobody asks for, so this is also
        where the ``have`` messages go out.
        """
        if not 0 <= index < self._torrent.piece_count:
            raise ValueError(f"piece index out of range: {index}")
        if self._have.has(index):
            return
        self._have.set(index)
        self._broadcast_have(index)
        self._wake.set()

    def note_downloaded(self, peer: Any, length: int) -> None:
        """Record bytes a peer sent us, so merit can be measured.

        Whoever wires the download engine to the upload engine calls this; the
        choking policy ranks peers by exactly this number.
        """
        key = self._key(peer)
        self._downloaded[key] = self._downloaded.get(key, 0) + length
        self._last_download[key] = time.monotonic()

    # ------------------------------------------------------------------ loop

    async def pump(self) -> int:
        """Run one pass: re-decide choking, then serve what we can.

        Returns:
            How many blocks were sent.
        """
        self._sync_peers()
        await self._apply_choke()
        return await self._serve_queues()

    async def _loop(self) -> None:
        """Background loop: pump when there is work, and every so often anyway."""
        while True:
            try:
                await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must not die
                logger.warning("upload pass failed: %s", exc)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_TICK_SECONDS)
            except TimeoutError:
                self._wake.clear()
            else:
                self._wake.clear()

    # ---------------------------------------------------------------- choking

    def _sync_peers(self) -> None:
        """Track arrivals and departures among the peer manager's connections."""
        live = {self._key(peer): peer for peer in self._live_peers()}
        for key in list(self._tracked):
            if key not in live:
                self.on_disconnect(self._tracked[key], "no longer connected")
        for peer in live.values():
            self._track(peer)

    async def _apply_choke(self) -> None:
        """Tell the policy who deserves a slot, and make it so on the wire."""
        now = time.monotonic()
        peers = {key: self._accounting(key, peer, now=now) for key, peer in self._tracked.items()}
        decision = self._policy.evaluate(peers, now=now)
        self._decision = decision

        for key, peer in self._tracked.items():
            should_choke = not decision.is_unchoked(key)
            if should_choke == bool(getattr(peer, "am_choking", False)):
                continue
            try:
                await peer.set_choking(should_choke)
            except Exception as exc:  # noqa: BLE001 - a peer may be gone already
                logger.debug("could not set choking for %s: %s", key, exc)
                continue
            self._emit(
                EventType.PEER_CHOKED if should_choke else EventType.PEER_UNCHOKED,
                f"{key}: {'choked' if should_choke else 'unchoked'}",
                data={"peer": key, "optimistic": decision.optimistic == key},
            )

    def _accounting(self, key: str, peer: Any, *, now: float) -> PeerAccounting:
        """What the policy needs to know about one peer."""
        session = getattr(peer, "session", None)
        return PeerAccounting(
            key=key,
            interested=bool(getattr(session, "peer_interested", False)),
            choking_us=bool(getattr(session, "peer_choking", True)),
            bytes_up=int(getattr(session, "uploaded", 0) or 0),
            bytes_down=self._downloaded.get(key, int(getattr(session, "downloaded", 0) or 0)),
            last_download_at=self._last_download.get(key),
            optimistic_rounds=0,
        )

    # ---------------------------------------------------------------- serving

    async def _serve_queues(self) -> int:
        """Serve queued requests: fairly, and within the rate limit.

        One pass drains as much as the current budget allows, taking one block
        from each peer in turn. When the pacer says a block has to wait, the
        request goes back to the front of its queue and a wake-up is scheduled
        — blocking here would stall every other peer behind this one.
        """
        self._reap_stale(time.monotonic())
        served = 0
        while True:
            progressed = False
            for key in self._round_robin():
                queue = self._queues[key]  # _round_robin only returns peers with work
                if not self._decision.is_unchoked(key):
                    # Still choking this peer: their queue waits, it does not
                    # grow the bill. A peer we have choked gets nothing, ever.
                    continue
                # Every queued peer is tracked: on_request tracks, and
                # _sync_peers clears the queue of anyone who has gone.
                peer = self._tracked[key]
                item = queue.popleft()
                delay, self._bucket = self._bucket.take(item.length, time.monotonic())
                if delay > 0:
                    # The bytes are spent; the pacer only decides when they
                    # go out. Waiting here would stall every other peer.
                    self._schedule_send(peer, key, item, delay)
                    progressed = True
                    continue
                if await self._send_one(peer, key, item):
                    served += 1
                    progressed = True
            if not progressed:
                return served

    def _schedule_send(self, peer: Any, key: str, item: QueuedRequest, delay: float) -> None:
        """Send a block once the rate limiter says it has been paid for."""
        task = asyncio.create_task(self._send_later(peer, key, item, delay))
        self._serving.add(task)
        task.add_done_callback(self._serving.discard)

    async def _send_later(self, peer: Any, key: str, item: QueuedRequest, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._send_one(peer, key, item)

    async def _send_one(self, peer: Any, key: str, item: QueuedRequest) -> bool:
        """Read one block from disk and send it. Returns False if it failed."""
        try:
            data = await self._storage.read_block(item.index, item.begin, item.length)
        except Exception as exc:  # noqa: BLE001 - a read failure is not fatal
            logger.warning("upload: cannot read piece %d block %d: %s", item.index, item.begin, exc)
            self._reject("read_failed")
            return False
        if len(data) != item.length:
            # Short read: the file shrank under us. Sending a short block would
            # corrupt the peer's piece, so the request is dropped instead.
            logger.warning(
                "upload: short read for piece %d block %d (%d of %d bytes)",
                item.index,
                item.begin,
                len(data),
                item.length,
            )
            self._reject("read_failed")
            return False
        try:
            await peer.send_piece(item.index, item.begin, data)
        except Exception as exc:  # noqa: BLE001 - the peer may have hung up
            logger.debug("upload: cannot send to %s: %s", key, exc)
            self._stats = replace(self._stats, requests_dropped=self._stats.requests_dropped + 1)
            return False

        session = getattr(peer, "session", None)
        if session is not None and hasattr(session, "note_upload"):
            session.note_upload(len(data))
        self._stats = replace(
            self._stats,
            blocks_served=self._stats.blocks_served + 1,
            bytes_uploaded=self._stats.bytes_uploaded + len(data),
            requests_served=self._stats.requests_served + 1,
        )
        self._emit(
            EventType.PIECE_UPLOADED,
            f"served piece {item.index} block at {item.begin} ({len(data)} bytes) to {key}",
            level=logging.DEBUG,
            data={
                "index": item.index,
                "begin": item.begin,
                "length": len(data),
                "peer": key,
            },
        )
        return True

    def _round_robin(self) -> list[str]:
        """Peers with queued work, rotated so no peer starves the others."""
        keys = [key for key, queue in self._queues.items() if queue]
        if not keys:
            return []
        rotation = self._rotation % len(keys)
        self._rotation = (self._rotation + 1) % 997
        return keys[rotation:] + keys[:rotation]

    def _reap_stale(self, now: float) -> None:
        """Drop requests that have waited longer than the queue allows."""
        for key, queue in list(self._queues.items()):
            fresh = deque(
                item for item in queue if now - item.queued_at < self._config.queue_timeout
            )
            if len(fresh) != len(queue):
                self._queues[key] = fresh
                self._stats = replace(
                    self._stats,
                    requests_dropped=self._stats.requests_dropped + (len(queue) - len(fresh)),
                )

    # ------------------------------------------------------------- validation

    def _reject_reason(self, peer: Any, message: Request, *, key: str) -> str | None:
        """Why this request cannot be served, or ``None`` if it can.

        Every check is here because every field of a request is chosen by
        somebody else. The order matters: bounds before existence, existence
        before permission, and permission last, so we never tell a stranger
        which pieces we hold until we have decided they are worth telling.
        """
        if not 0 <= message.index < self._torrent.piece_count:
            return "bad_index"
        piece_size = self._torrent.piece_size(message.index)
        if message.begin < 0 or message.begin >= piece_size:
            return "bad_offset"
        if not 0 < message.length <= MAX_BLOCK_SIZE:
            return "bad_length"
        if message.begin + message.length > piece_size:
            return "bad_length"
        if not self._have.has(message.index):
            return "missing_piece"
        session = getattr(peer, "session", None)
        if not bool(getattr(session, "peer_interested", False)):
            return "not_interested"
        if not self._decision.is_unchoked(key):
            return "choked"
        if len(self._queues.get(key, deque())) >= self._config.max_requests_per_peer:
            return "queue_full"
        return None

    def _reject(self, reason: str) -> None:
        """Count a refusal by reason."""
        rejected = dict(self._stats.requests_rejected)
        rejected[reason] = rejected.get(reason, 0) + 1
        self._stats = replace(self._stats, requests_rejected=rejected)

    # ----------------------------------------------------------------- helpers

    def _live_peers(self) -> list[Any]:
        """Connected peers from the peer manager."""
        connections: Iterable[Any] = getattr(self._peers, "connections", ())
        return [peer for peer in connections if bool(getattr(peer, "connected", False))]

    def _track(self, peer: Any) -> None:
        """Remember a peer object so its queue can be served later."""
        self._tracked[self._key(peer)] = peer

    @staticmethod
    def _key(peer: Any) -> str:
        """Stable identity for a peer, however it is addressed."""
        address = getattr(peer, "address", None)
        host = getattr(address, "host", None)
        port = getattr(address, "port", None)
        if host is not None and port is not None:
            return f"{host}:{port}"
        return str(getattr(peer, "key", id(peer)))

    def _broadcast_have(self, index: int) -> None:
        """Tell every connected peer about a new piece."""
        for key, peer in self._tracked.items():
            send = getattr(peer, "send_have", None)
            if send is None:
                continue
            task = asyncio.create_task(self._send_have(peer, index, key))
            self._serving.add(task)
            task.add_done_callback(self._serving.discard)

    async def _send_have(self, peer: Any, index: int, key: str) -> None:
        try:
            await peer.send_have(index)
        except Exception as exc:  # noqa: BLE001 - a peer may be gone already
            logger.debug("could not announce piece %d to %s: %s", index, key, exc)

    def _emit(
        self,
        event: EventType,
        message: str,
        *,
        level: int = logging.INFO,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Publish an event, if a bus was provided."""
        if self._event_bus is None:
            return
        self._event_bus.emit(
            make_event(
                event,
                message=message,
                torrent_id=self._torrent.hex_info_hash,
                level=level,
                data=dict(data or {}),
            )
        )
