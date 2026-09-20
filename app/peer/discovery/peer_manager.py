"""Peer discovery bookkeeping: candidates, connections and slots (TRD §22).

A tracker hands us addresses; most of them are useless. Peers sit behind NAT
mappings that have expired, on home connections that are switched off, or are
simply full. The manager's job is to turn an untrusted list of addresses into a
small set of working connections, and to remember what it learned so it stops
asking.

Specifically it owns:

* **Candidates** — addresses we know about but are not connected to, each with
  a failure count and a retry time. A candidate that fails repeatedly backs off
  exponentially and is eventually dropped: a dead address we redial every few
  seconds is indistinguishable from an attack on someone else's network.
* **Connections** — the live :class:`~app.peer.connection.PeerConnection`
  objects, capped at ``max_connections``. Slots are precious: a client that
  opens 200 connections and uses each badly is worse than one that opens 40 and
  uses them well.
* **Reaping** — when a connection's read loop ends, the slot is freed and the
  peer returns to the candidate pool (it may come back), with a backoff so a
  peer that hangs up is not redialled instantly.

Nothing here decides *what* to download; that is M7. This module only answers
"who can we talk to right now, and who has piece N".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from app.core.config import NetworkConfig
from app.core.constants import DEFAULT_REFILL_INTERVAL
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.discovery.pex import (
    UT_PEX,
    PexContact,
    PexError,
    PexLedger,
    decode_pex,
    sanitize_incoming,
)
from app.peer.connection import PeerConnection, SwarmContext
from app.peer.errors import PeerError
from app.peer.messages import Cancel, Message, Request
from app.peer.messages import Piece as PieceMessage
from app.tracker.base import PeerAddress

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(slots=True)
class PeerCandidate:
    """An address we could connect to, and what we learned last time.

    Attributes:
        address: The peer's address.
        failures: Consecutive connection or handshake failures.
        last_attempt: When we last tried, as a monotonic timestamp.
        last_success: When we last held a working connection.
        last_error: Why the last attempt failed.
        retry_at: Earliest time to try again, after backoff.
    """

    address: PeerAddress
    failures: int = 0
    last_attempt: float | None = None
    last_success: float | None = None
    last_error: str | None = None
    retry_at: float = 0.0

    @property
    def available(self) -> bool:
        """Whether backoff has expired and we may try again."""
        return time.monotonic() >= self.retry_at

    def record_failure(self, reason: str, *, max_failures: int, reconnect_delay: float) -> bool:
        """Record a failure and back off exponentially.

        Returns:
            True when the candidate has failed too often and should be dropped.
        """
        self.failures += 1
        self.last_error = reason
        self.last_attempt = time.monotonic()
        self.retry_at = time.monotonic() + reconnect_delay * (2 ** (self.failures - 1))
        return self.failures >= max_failures

    def record_success(self) -> None:
        """Reset the failure counter after a working connection."""
        self.failures = 0
        self.last_error = None
        self.last_success = time.monotonic()
        self.retry_at = 0.0

    def note_disconnect(
        self, reason: str, *, max_failures: int, reconnect_delay: float, handshaked: bool
    ) -> bool:
        """Record that a connection ended.

        A peer that completed a handshake is worth trying again later, so its
        failure count is cleared — but it still waits out a backoff, because a
        peer that just hung up is unlikely to want us back this second.

        Returns:
            True when the candidate is spent and should be dropped.
        """
        self.last_attempt = time.monotonic()
        if handshaked:
            self.record_success()
            self.retry_at = time.monotonic() + reconnect_delay
            return False
        return self.record_failure(
            reason, max_failures=max_failures, reconnect_delay=reconnect_delay
        )


@dataclass(frozen=True, slots=True)
class PeerManagerStats:
    """Counters for the UI's peers tab (PRD §10.8)."""

    discovered: int = 0
    candidates: int = 0
    connected: int = 0
    uninteresting: int = 0
    max_connections: int = 0


class PeerManager:
    """Decides which peers to connect to, and keeps the set healthy.

    Args:
        context: The torrent these peers belong to.
        peer_id: Our peer id, used for every handshake.
        config: Connection limits and timeouts.
        event_bus: Optional bus that receives discovery and failure events.
        on_block: Passed to each connection; called with ``(connection, message)``
            for every received block.
        on_have: Passed to each connection; called with ``(connection, index)``
            when a peer announces a new piece.
        on_disconnect: Called with ``(connection, reason)`` when a connection
            ends, so the engine can release the piece work it had assigned.
        on_request: Passed to each connection; called with
            ``(connection, message)`` when a peer asks us for a block (M8).
        on_cancel: Passed to each connection; called with
            ``(connection, message)`` when a peer takes a request back (M8).
        max_connections: Slot cap; defaults to ``config.max_peers_per_torrent``.
        our_port: Our listening port, used to avoid connecting to ourselves in
            loopback swarms.
    """

    def __init__(
        self,
        context: SwarmContext,
        *,
        peer_id: bytes,
        config: NetworkConfig | None = None,
        event_bus: EventBus | None = None,
        on_block: Callable[[PeerConnection, PieceMessage], None] | None = None,
        on_have: Callable[[PeerConnection, int], None] | None = None,
        on_disconnect: Callable[[PeerConnection, str], None] | None = None,
        on_request: Callable[[PeerConnection, Request], None] | None = None,
        on_cancel: Callable[[PeerConnection, Cancel], None] | None = None,
        max_connections: int | None = None,
        our_port: int | None = None,
    ) -> None:
        self.context = context
        self.peer_id = peer_id
        self.config = config or NetworkConfig()
        self.event_bus = event_bus
        self.on_block = on_block
        self.on_have = on_have
        self.on_disconnect = on_disconnect
        self.on_request = on_request
        self.on_cancel = on_cancel
        self.max_connections = max_connections or self.config.max_peers_per_torrent
        self.our_port = our_port

        # BEP 27: a private torrent's swarm is closed on purpose, so we neither
        # offer ut_pex nor accept it. The extension map stays empty, which means
        # the handshake does not even set the extension bit.
        self.pex_enabled = not context.private
        self._pex: dict[tuple[str, int], PexLedger] = {}
        self.pex_received = 0
        """How many addresses peer exchange has handed us, accepted or not."""
        self._candidates: dict[tuple[str, int], PeerCandidate] = {}
        self._connections: dict[tuple[str, int], PeerConnection] = {}
        self._discovered = 0
        self._maintain_task: asyncio.Task[None] | None = None
        self._reap_tasks: set[asyncio.Task[None]] = set()
        self._stop_event = asyncio.Event()
        # Set whenever the pool gains a peer, so the maintain loop does not sit
        # out a whole interval while a freshly announced swarm goes undialled.
        self._wakeup = asyncio.Event()

    # ------------------------------------------------------------ discovery

    def add_peers(self, peers: Sequence[PeerAddress], *, source: str | None = None) -> int:
        """Add addresses to the candidate pool.

        Duplicates, addresses we are already connected to, and (on loopback)
        our own listening socket are ignored, because trackers hand those out
        freely.

        Args:
            peers: Addresses to add.
            source: Overrides the provenance recorded on each address.

        Returns:
            How many addresses were genuinely new.
        """
        added = 0
        for address in peers:
            effective = (
                address
                if source is None
                else PeerAddress(
                    host=address.host,
                    port=address.port,
                    peer_id=address.peer_id,
                    source=source,
                )
            )
            key = effective.address
            if key in self._candidates or key in self._connections:
                continue
            if self._is_self(effective):
                continue

            self._candidates[key] = PeerCandidate(address=effective)
            self._discovered += 1
            added += 1
            self._emit(
                EventType.PEER_DISCOVERED,
                f"discovered peer {effective} via {effective.source}",
                level=logging.DEBUG,
                data={"host": effective.host, "port": effective.port, "source": effective.source},
            )
        if added:
            self._wakeup.set()
        return added

    # ----------------------------------------------------------- connecting

    async def fill(self, *, limit: int | None = None) -> tuple[PeerConnection, ...]:
        """Open connections until the slot cap is reached.

        Candidates are tried least-failed first, and all attempts in one round
        run concurrently: connecting serially at a ten-second timeout each
        would need minutes to fill forty slots.

        Returns:
            The connections that were opened successfully.
        """
        slots = self.max_connections - len(self._connections)
        if slots <= 0:
            return ()
        if limit is not None:
            slots = min(slots, limit)

        chosen = self._select(slots)
        if not chosen:
            return ()

        results = await asyncio.gather(*(self._connect_one(candidate) for candidate in chosen))
        return tuple(connection for connection in results if connection is not None)

    async def connect_to(self, address: PeerAddress) -> PeerConnection:
        """Connect to one specific address.

        Raises:
            PeerError: If the connection or handshake failed.
        """
        if existing := self._connections.get(address.address):
            return existing
        connection = self._build_connection(address)
        await connection.connect()
        self._register(connection)
        return connection

    async def adopt(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> PeerConnection | None:
        """Take in a socket a peer opened to us.

        The incoming counterpart of :meth:`connect_to`. The handshake is
        completed here rather than in the listener, because "is this socket
        worth a slot" is a question only the peer manager can answer, and
        answering it after the handshake wastes nothing but a stray TCP
        connection.

        Args:
            reader: Stream reader for the accepted socket.
            writer: Stream writer for the accepted socket.

        Returns:
            The adopted, running connection, or ``None`` if it was refused:
            no free slot, a failed handshake, or a peer we are already
            talking to. A refused socket is closed by the caller.
        """
        if len(self._connections) >= self.max_connections:
            logger.debug("refusing incoming connection: no free slot")
            return None

        peername = writer.get_extra_info("peername")
        if not peername:
            return None
        host, port = peername[0], int(peername[1])
        try:
            address = PeerAddress(host=host, port=port, source="incoming")
        except ValueError:
            return None
        if address.address in self._connections:
            return None
        if self._is_self(address):
            return None

        connection = self._build_connection(address)
        try:
            await connection.attach(reader, writer)
        except PeerError as exc:
            logger.debug("incoming connection from %s failed: %s", address, exc)
            return None
        self._register(connection)
        self._emit(
            EventType.PEER_DISCOVERED,
            f"incoming peer {address}",
            level=logging.DEBUG,
            data={"source": "incoming", "address": str(address)},
        )
        return connection

    # --------------------------------------------------------------- access

    @property
    def connections(self) -> tuple[PeerConnection, ...]:
        """Every live connection."""
        return tuple(self._connections.values())

    @property
    def candidates(self) -> tuple[PeerCandidate, ...]:
        """Every candidate we know about, most promising first.

        Includes peers that are waiting out a backoff: a UI listing "known
        peers" should show them. :meth:`_select` is what applies availability.
        """
        return tuple(
            sorted(
                self._candidates.values(),
                key=lambda candidate: (
                    candidate.failures,
                    candidate.last_attempt or 0.0,
                    candidate.address.address,
                ),
            )
        )

    @property
    def stats(self) -> PeerManagerStats:
        """Counts for the UI."""
        return PeerManagerStats(
            discovered=self._discovered,
            candidates=len(self._candidates),
            connected=len(self._connections),
            uninteresting=sum(1 for peer in self._connections.values() if peer.progress == 0),
            max_connections=self.max_connections,
        )

    def connection_for(self, address: PeerAddress) -> PeerConnection | None:
        """The live connection to an address, if any."""
        return self._connections.get(address.address)

    def peers_holding(self, index: int) -> tuple[PeerConnection, ...]:
        """Connected peers that report having piece ``index``."""
        return tuple(
            connection
            for connection in self._connections.values()
            if connection.connected and connection.bitfield.has(index)
        )

    def unchoked_peers(self) -> tuple[PeerConnection, ...]:
        """Connected peers that are willing to serve us right now."""
        return tuple(
            connection
            for connection in self._connections.values()
            if connection.connected and not connection.choked
        )

    # ------------------------------------------------------------------ I/O

    async def broadcast(self, message: Message) -> int:
        """Send a message to every connected peer.

        Returns:
            How many peers received it. One dead socket must not stop a
            ``have`` from reaching the others, so failures are skipped.
        """
        sent = 0
        for connection in self.connections:
            try:
                await connection.send(message)
            except PeerError as exc:
                logger.debug("broadcast to %s failed: %s", connection.address, exc)
            else:
                sent += 1
        return sent

    # ------------------------------------------------------------ lifecycle

    async def maintain(self, *, interval: float = DEFAULT_REFILL_INTERVAL) -> None:
        """Keep topping the connection count up until cancelled.

        Wakes early when :meth:`add_peers` learns about someone new: a client
        that waits ten seconds to dial the peers a tracker just handed it is a
        client that looks broken.
        """
        while not self._stop_event.is_set():
            await self.fill()
            await self.exchange_peers()
            await self._wait_until_next_fill(interval)

    # ------------------------------------------------------- peer exchange

    async def exchange_peers(self) -> int:
        """Tell every peer that speaks ``ut_pex`` who else we are talking to.

        Returns how many messages went out. The ledger inside each connection
        decides whether one is due at all — BEP 11 caps us at one per peer per
        minute — so calling this from a five-second loop is correct, not rude.

        Only peers we are *connected to* are advertised, and only to peers whose
        extension handshake has arrived. A candidate we have not dialled is not
        a peer we can vouch for, which is the difference between peer exchange
        and a relay for somebody else's denial-of-service target list.
        """
        if not self.pex_enabled or not self._connections:
            return 0

        contacts = self._pex_contacts()
        sent = 0
        for key, connection in list(self._connections.items()):
            if not connection.extensions.can_send(UT_PEX):
                continue
            ledger = self._pex.setdefault(key, PexLedger())
            # Nobody needs to be told about itself; it would only spend a slot
            # in a capped message.
            ledger.sync(contact for contact in contacts if contact.address.address != key)
            payload = ledger.build()
            if payload is None:
                continue
            if await connection.send_extension(UT_PEX, payload):
                sent += 1
        return sent

    def _pex_contacts(self) -> list[PexContact]:
        """The connected peers we can describe honestly.

        Two filters, both about not advertising something useless:

        * An incoming connection's ``address`` is the peer's *ephemeral source
          port*, which nobody can dial. Unless the peer told us its listening
          port in the BEP 10 handshake's ``p`` field, we do not know a reachable
          address for it and we say nothing about it at all.
        * "Reachable" (BEP 11's ``0x10``) means we dialled it and it answered. A
          peer that dialled us proves nothing about whether we could dial it
          back, so incoming connections never earn the flag.
        """
        contacts: list[PexContact] = []
        for connection in self._connections.values():
            address = connection.address
            if connection.incoming:
                stated = connection.extensions.port
                if stated is None:
                    continue
                address = PeerAddress(host=address.host, port=stated, source="pex")
            contacts.append(
                PexContact(
                    address=address,
                    seed=connection.session.is_seed,
                    reachable=not connection.incoming,
                )
            )
        return contacts

    def _on_extension(self, connection: PeerConnection, name: str, payload: bytes) -> None:
        """Handle one extension message from a peer."""
        if name != UT_PEX:
            logger.debug("ignoring extension %s from %s", name, connection.address)
            return
        try:
            message = decode_pex(payload)
        except PexError as exc:
            # BEP 11 lets us drop a peer that egregiously breaks the format. We
            # do not: a garbled pex message says nothing about whether this peer
            # can serve pieces, and the connection is worth more than the lesson.
            logger.debug("unreadable pex from %s: %s", connection.address, exc)
            return

        self.pex_received += len(message.peers)
        accepted = sanitize_incoming(message)
        if message.truncated:
            logger.debug(
                "pex from %s carried %d contacts past our cap",
                connection.address,
                message.truncated,
            )
        if not accepted:
            return
        added = self.add_peers(accepted, source="pex")
        logger.debug(
            "pex from %s: %d offered, %d new (dropped %d)",
            connection.address,
            len(accepted),
            added,
            len(message.dropped) + len(message.dropped6),
        )

    async def _wait_until_next_fill(self, interval: float) -> None:
        """Wait for new peers, a stop, or the interval — whichever is first."""
        self._wakeup.clear()
        waiting = {
            asyncio.ensure_future(event.wait()) for event in (self._stop_event, self._wakeup)
        }
        try:
            done, pending = await asyncio.wait(
                waiting, timeout=interval, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            for task in waiting:
                task.cancel()
            raise
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(Exception):
                task.result()

    def start(self, *, interval: float = DEFAULT_REFILL_INTERVAL) -> asyncio.Task[None]:
        """Run :meth:`maintain` in the background."""
        if self._maintain_task is not None:
            raise RuntimeError("this peer manager is already running")
        self._stop_event = asyncio.Event()
        self._maintain_task = asyncio.create_task(
            self.maintain(interval=interval), name=f"peers:{self.context.hex_info_hash[:8]}"
        )
        return self._maintain_task

    async def stop(self) -> None:
        """Stop maintaining and close every connection."""
        self._stop_event.set()
        if self._maintain_task is not None:
            self._maintain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintain_task
            self._maintain_task = None
        await self.close_all()

    async def close_all(self) -> None:
        """Close every connection and forget it."""
        for connection in self.connections:
            await connection.aclose()
        self._connections.clear()

    # -------------------------------------------------------------- internals

    def _is_self(self, address: PeerAddress) -> bool:
        """Whether an address is our own listener on loopback.

        Incoming connections report our own port back to us in local swarms;
        connecting to ourselves wastes a slot and confuses the statistics.
        """
        return (
            self.our_port is not None
            and address.port == self.our_port
            and (address.host in LOOPBACK_HOSTS)
        )

    def _build_connection(self, address: PeerAddress) -> PeerConnection:
        """Create a connection object for an address."""
        return PeerConnection(
            address,
            self.context,
            peer_id=self.peer_id,
            config=self.config,
            event_bus=self.event_bus,
            on_block=self.on_block,
            on_have=self.on_have,
            on_request=self.on_request,
            on_cancel=self.on_cancel,
            extensions={UT_PEX: 1} if self.pex_enabled else None,
            on_extension=self._on_extension,
        )

    def _select(self, count: int) -> list[PeerCandidate]:
        """Pick the most promising available candidates, best first."""
        available = [candidate for candidate in self._candidates.values() if candidate.available]
        available.sort(
            key=lambda candidate: (
                candidate.failures,
                candidate.last_attempt or 0.0,
                candidate.address.address,
            )
        )
        return available[:count]

    async def _connect_one(self, candidate: PeerCandidate) -> PeerConnection | None:
        """Try one candidate; record the outcome either way."""
        connection = self._build_connection(candidate.address)
        try:
            await connection.connect()
        except PeerError as exc:
            spent = candidate.record_failure(
                str(exc),
                max_failures=self.config.max_peer_failures,
                reconnect_delay=self.config.reconnect_delay,
            )
            if spent:
                self._candidates.pop(candidate.address.address, None)
                logger.debug("dropping %s after failures", candidate.address)
            return None

        candidate.record_success()
        self._candidates.pop(candidate.address.address, None)
        self._register(connection)
        return connection

    def _register(self, connection: PeerConnection) -> None:
        """Track a connected peer and watch for it going away."""
        self._connections[connection.address.address] = connection
        task = connection.start()
        task.add_done_callback(self._make_reaper(connection))

    def _make_reaper(self, connection: PeerConnection) -> Callable[[asyncio.Task[None]], None]:
        """Build the done-callback that frees a slot when a read loop ends."""

        def reap(_task: asyncio.Task[None]) -> None:
            # The read loop is done; the bookkeeping happens in its own task so
            # a callback never runs it halfway through something else. The task
            # is kept in a set because a pending task with no other reference
            # can be garbage collected before it finishes.
            task = asyncio.create_task(self._reap(connection))
            self._reap_tasks.add(task)
            task.add_done_callback(self._reap_tasks.discard)

        return reap

    async def _reap(self, connection: PeerConnection) -> None:
        """Free a finished connection's slot and keep the address for later."""
        key = connection.address.address
        self._connections.pop(key, None)
        # The ledger goes with the connection. The peers we advertised it are
        # retracted by every *other* ledger at its next pass, because their
        # snapshot no longer contains the peer that left.
        self._pex.pop(key, None)
        reason = connection.disconnect_reason or "unknown"
        await connection.aclose(reason=reason)

        candidate = PeerCandidate(address=connection.address)
        spent = candidate.note_disconnect(
            reason,
            max_failures=self.config.max_peer_failures,
            reconnect_delay=self.config.reconnect_delay,
            handshaked=connection.session.peer_id is not None,
        )
        if not spent:
            self._candidates[key] = candidate

        if self.on_disconnect is not None:
            self.on_disconnect(connection, reason)

    def _emit(
        self,
        event_type: EventType,
        message: str,
        *,
        level: int = logging.INFO,
        data: dict[str, object] | None = None,
    ) -> None:
        if self.event_bus is None:
            return
        self.event_bus.emit(
            make_event(
                event_type,
                message=message,
                torrent_id=self.context.hex_info_hash,
                level=level,
                data=data,
            )
        )
