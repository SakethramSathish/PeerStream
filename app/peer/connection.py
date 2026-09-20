"""One peer connection: socket, session and lifecycle (TRD §22, §23).

M4 gave us the *language* — messages, framing, state. This module gives us a
*conversation*: it owns the stream, drives the handshake, dispatches incoming
messages into the session, keeps the connection alive, and reports everything
through the event bus.

The shape is deliberately simple:

```
connection = PeerConnection(address, context, peer_id=..., event_bus=bus)
await connection.connect()      # TCP + handshake
connection.start()              # background read loop
...
await connection.aclose()       # cancel, close, report
```

Two details that decide whether this class is pleasant or not:

**The read deadline is the idle timeout.** A long-lived connection cannot use a
short per-read deadline (a perfectly healthy peer says nothing for minutes), so
the stream's read timeout is set to ``idle_timeout``. A peer that goes silent
for that long raises :class:`PeerTimeoutError`, which is exactly the signal we
want, and keep-alives sent every ``keepalive_interval`` keep a healthy peer
from ever looking idle.

**Sending updates local state.** Sending ``interested`` is what makes us
interested; the flag must flip only after the bytes are actually written, or
``can_request`` lies to the scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.core.config import NetworkConfig
from app.core.constants import INFO_HASH_SIZE
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.core.peer_id import client_version_string
from app.peer.bitfield import Bitfield
from app.peer.errors import (
    PeerDisconnected,
    PeerError,
    PeerTimeoutError,
    ProtocolError,
)
from app.peer.extension import HANDSHAKE_ID, ExtensionError, ExtensionState, decode_handshake
from app.peer.handshake import Handshake, outgoing_handshake
from app.peer.messages import (
    Cancel,
    Choke,
    Extended,
    Have,
    Interested,
    Message,
    NotInterested,
    Request,
    Unchoke,
)
from app.peer.messages import (
    Piece as PieceMessage,
)
from app.peer.protocol import PeerStream
from app.peer.state import ConnectionState, PeerSession
from app.tracker.base import PeerAddress

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SwarmContext:
    """What a connection needs to know about the torrent it is serving.

    Kept separate from :class:`~app.torrent.metadata.Torrent` so the peer layer
    depends on four values, not on the whole metainfo parser.

    Args:
        info_hash: The torrent's info hash — what the handshake must match.
        piece_count: Number of pieces, which sizes the bitfield.
        piece_length: Size of a full piece, used to bound incoming blocks.
        name: Torrent name, for log lines and event messages.
    private: BEP 27's flag. A private torrent is tracker-only, so neither the
        DHT nor peer exchange may be used for it: the swarm is closed on
        purpose, and handing its peers to strangers defeats that.
    """

    info_hash: bytes
    piece_count: int
    piece_length: int
    name: str = ""
    private: bool = False

    def __post_init__(self) -> None:
        if len(self.info_hash) != INFO_HASH_SIZE:
            raise ValueError(f"info_hash must be {INFO_HASH_SIZE} bytes")
        if self.piece_count <= 0:
            raise ValueError(f"piece_count must be positive, got {self.piece_count}")
        if self.piece_length <= 0:
            raise ValueError(f"piece_length must be positive, got {self.piece_length}")

    @classmethod
    def from_torrent(cls, torrent: object) -> SwarmContext:
        """Build a context from a parsed torrent."""
        return cls(
            info_hash=torrent.info_hash,  # type: ignore[attr-defined]
            piece_count=torrent.piece_count,  # type: ignore[attr-defined]
            piece_length=torrent.piece_length,  # type: ignore[attr-defined]
            name=torrent.name,  # type: ignore[attr-defined]
            private=bool(torrent.private),  # type: ignore[attr-defined]
        )

    @property
    def hex_info_hash(self) -> str:
        """The info hash as hex, used as the event bus's torrent id."""
        return self.info_hash.hex()


class PeerConnection:
    """A live conversation with one peer.

    Args:
        address: Where to connect (or where an incoming peer came from).
        context: The torrent this connection is about.
        peer_id: Our peer id, sent in the handshake.
        config: Connection timeouts and limits.
        event_bus: Optional bus that receives every lifecycle event.
        on_block: Called with ``(connection, message)`` for each ``piece``
            message received. The download engine (M7) needs to know *which*
            peer sent a block to credit it, cancel endgame duplicates, and
            penalise peers that send corrupt data; keeping it a callback means
            this class never decides what to do with the data itself.
        on_have: Called with ``(connection, index)`` when a peer announces a
            piece it did not have before, so the engine can update rarity.
        on_request: Called with ``(connection, message)`` when a peer asks us
            for a block. The upload engine (M8) decides whether to serve it,
            and this class deliberately does not: choking and rate limits are
            policy, not transport.
        on_cancel: Called with ``(connection, message)`` when a peer takes back
            a request, so the upload queue can drop it.
        extensions: The BEP 10 extensions we answer, mapped to the ids we number
            them with. Empty (the default) means we do not advertise the
            extension protocol at all, which is what a private torrent wants.
        on_extension: Called with ``(connection, name, payload)`` for every
            extension message the peer sends us, already translated from its id
            to the name we both agreed on. Peer exchange is delivered here; this
            class does not know what a ``ut_pex`` body means.
    """

    def __init__(
        self,
        address: PeerAddress,
        context: SwarmContext,
        *,
        peer_id: bytes,
        config: NetworkConfig | None = None,
        event_bus: EventBus | None = None,
        on_block: Callable[[PeerConnection, PieceMessage], None] | None = None,
        on_have: Callable[[PeerConnection, int], None] | None = None,
        on_request: Callable[[PeerConnection, Request], None] | None = None,
        on_cancel: Callable[[PeerConnection, Cancel], None] | None = None,
        extensions: Mapping[str, int] | None = None,
        on_extension: Callable[[PeerConnection, str, bytes], None] | None = None,
    ) -> None:
        self.address = address
        self.context = context
        self.our_peer_id = peer_id
        self.config = config or NetworkConfig()
        self.event_bus = event_bus
        self.on_block = on_block
        self.on_have = on_have
        self.on_request = on_request
        self.on_cancel = on_cancel
        self.on_extension = on_extension
        self.session = PeerSession(piece_count=context.piece_count)
        self.stream: PeerStream | None = None
        self.disconnect_reason: str | None = None
        self.connected_at: float | None = None
        #: Which extensions this connection can use, in both directions.
        self.extensions = ExtensionState(ours=dict(extensions or {}))
        #: True when the peer dialled us. BEP 11's "reachable" flag is only
        #: honest for a connection we initiated and that completed.
        self.incoming = False
        self._run_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        """Open the socket and complete the handshake.

        Raises:
            PeerError: If the connection or handshake failed. The connection
                is left closed, and a ``peer_failed`` event is published.
        """
        self._emit(
            EventType.PEER_CONNECTING,
            f"connecting to {self.address}",
            level=logging.DEBUG,
        )
        try:
            self.stream = await PeerStream.connect(
                self.address.host,
                self.address.port,
                timeout=self.config.connection_timeout,
                read_timeout=self.config.idle_timeout,
                max_message_length=self.config.max_message_length,
            )
        except PeerError as exc:
            self.stream = None
            self._fail(str(exc))
            raise

        try:
            handshake = await self.stream.perform_handshake(
                self._our_handshake(),
                timeout=self.config.handshake_timeout,
            )
        except PeerError as exc:
            await self.stream.aclose()
            self.stream = None
            self._fail(str(exc))
            raise

        self.session.note_handshake(handshake.peer_id, handshake.client)
        self.connected_at = time.monotonic()
        await self._negotiate_extensions(handshake.supports_extensions)
        self._emit(EventType.PEER_CONNECTED, f"connected to {handshake.client}")
        self._emit(
            EventType.PEER_HANDSHAKE,
            str(handshake),
            level=logging.DEBUG,
            data={
                "client": handshake.client,
                "peer_id": handshake.peer_id.hex(),
                "supports_dht": handshake.supports_dht,
                "latency_ms": self.stream.handshake_latency_ms,
            },
        )

    async def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Adopt a socket a peer opened to us, and complete the handshake.

        The incoming counterpart of :meth:`connect`. Everything after the
        handshake — read loop, keep-alives, teardown — is identical, because
        once both sides have exchanged handshakes the direction of the dial
        does not matter.

        Raises:
            PeerError: The handshake was wrong, late, or for another torrent.
        """
        self._emit(
            EventType.PEER_CONNECTING,
            f"incoming connection from {self.address}",
            level=logging.DEBUG,
        )
        self.stream = PeerStream(
            reader,
            writer,
            max_message_length=self.config.max_message_length,
            timeout=self.config.idle_timeout,
            label=f"{self.address.host}:{self.address.port}",
        )
        try:
            handshake = await self.stream.accept_handshake(
                self._our_handshake(),
                timeout=self.config.handshake_timeout,
            )
        except PeerError as exc:
            await self.stream.aclose()
            self.stream = None
            self._fail(str(exc))
            raise

        self.session.note_handshake(handshake.peer_id, handshake.client)
        self.connected_at = time.monotonic()
        self.incoming = True
        await self._negotiate_extensions(handshake.supports_extensions)
        self._emit(EventType.PEER_CONNECTED, f"connected to {handshake.client} (incoming)")
        self._emit(
            EventType.PEER_HANDSHAKE,
            str(handshake),
            level=logging.DEBUG,
            data={
                "client": handshake.client,
                "peer_id": handshake.peer_id.hex(),
                "supports_dht": handshake.supports_dht,
                "incoming": True,
            },
        )

    async def run(self) -> None:
        """Read and dispatch messages until the connection ends.

        Awaited by :meth:`start`'s background task. Returns normally when the
        peer disconnects, goes idle, or breaks the protocol; raises
        :class:`asyncio.CancelledError` only when we are closed locally.
        """
        if self.stream is None:
            raise RuntimeError("connect() must be called before run()")

        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name=f"keepalive:{self.address.host}:{self.address.port}"
        )
        try:
            async for message in self.stream.messages():
                self._handle(message)
            self.disconnect_reason = self.disconnect_reason or "peer closed the connection"
        except PeerTimeoutError as exc:
            self.disconnect_reason = f"idle: {exc}"
        except ProtocolError as exc:
            self.disconnect_reason = str(exc)
            self._fail(str(exc))
        except asyncio.CancelledError:
            self.disconnect_reason = self.disconnect_reason or "closed locally"
            raise
        finally:
            await self._finish()

    def start(self) -> asyncio.Task[None]:
        """Run the read loop as a background task.

        Returns:
            The task; cancel it (or call :meth:`aclose`) to stop.
        """
        if self._run_task is not None:
            raise RuntimeError("this connection is already running")
        self._run_task = asyncio.create_task(
            self.run(), name=f"peer:{self.address.host}:{self.address.port}"
        )
        return self._run_task

    async def aclose(self, *, reason: str = "closed locally") -> None:
        """Close the connection and stop its tasks. Safe to call repeatedly."""
        if self.disconnect_reason is None:
            self.disconnect_reason = reason
        await self._stop_tasks()
        if self.stream is not None:
            await self.stream.aclose()
        self.session.mark_closed()
        self._closed.set()

    # ------------------------------------------------------------------- I/O

    async def send(self, message: Message) -> None:
        """Send one message and update our side of the session state.

        Raises:
            PeerDisconnected: Not connected, or the write failed.
        """
        if self.stream is None:
            raise PeerDisconnected(f"{self.address}: not connected")
        await self.stream.send(message)
        self._note_sent(message)

    async def send_interested(self, interested: bool = True) -> None:
        """Tell the peer whether we want its pieces."""
        await self.send(Interested() if interested else NotInterested())

    async def send_have(self, index: int) -> None:
        """Announce one piece we have just verified.

        Peers only ask us for pieces they know we hold, so a completed piece
        that is never announced is a piece nobody downloads from us.
        """
        if not 0 <= index < self.context.piece_count:
            raise ValueError(f"piece index out of range: {index}")
        await self.send(Have(index=index))

    async def send_piece(self, index: int, begin: int, data: bytes) -> None:
        """Serve one block to this peer."""
        await self.send(PieceMessage(index=index, begin=begin, data=data))

    async def set_choking(self, choking: bool) -> None:
        """Tell the peer whether we will serve it."""
        await self.send(Choke() if choking else Unchoke())

    # ------------------------------------------------------------ inspection

    @property
    def connected(self) -> bool:
        """Whether the connection is open and running."""
        return self.stream is not None and self.session.state is ConnectionState.CONNECTED

    @property
    def closed(self) -> bool:
        """Whether the connection has finished."""
        return self._closed.is_set() or self.session.closed

    @property
    def client(self) -> str:
        """The peer's client software, or ``Unknown`` before the handshake."""
        return self.session.client

    @property
    def peer_id(self) -> bytes | None:
        """The peer's id, once the handshake has been read."""
        return self.session.peer_id

    @property
    def bitfield(self) -> Bitfield:
        """What the peer has told us it holds."""
        return self.session.bitfield

    @property
    def choked(self) -> bool:
        """Whether the peer is choking us."""
        return self.session.peer_choking

    @property
    def am_choking(self) -> bool:
        """Whether we are refusing to upload to this peer.

        The mirror of :attr:`choked`, and what the upload engine compares
        against when it decides a peer's state has to change.
        """
        return self.session.am_choking

    @property
    def peer_interested(self) -> bool:
        """Whether the peer has told us it wants our pieces."""
        return self.session.peer_interested

    @property
    def progress(self) -> float:
        """Fraction of the torrent the peer holds."""
        return self.session.progress

    @property
    def latency_ms(self) -> float | None:
        """Handshake round-trip time in milliseconds."""
        return self.stream.handshake_latency_ms if self.stream is not None else None

    @property
    def bytes_sent(self) -> int:
        """Bytes written to this peer, including the handshake."""
        return self.stream.bytes_sent if self.stream is not None else 0

    @property
    def bytes_received(self) -> int:
        """Bytes read from this peer, including the handshake."""
        return self.stream.bytes_received if self.stream is not None else 0

    @property
    def idle_for(self) -> float:
        """Seconds since the last byte in either direction."""
        return self.stream.idle_for if self.stream is not None else 0.0

    def __str__(self) -> str:
        state = "closed" if self.closed else ("connected" if self.connected else "idle")
        return f"<PeerConnection {self.address} {self.client} {state}>"

    # -------------------------------------------------------------- internals

    def _handle(self, message: Message) -> None:
        """Fold one message into the session and publish what it means."""
        announced = isinstance(message, Have) and not self.session.bitfield.has(message.index)
        try:
            event = self.session.apply(message, piece_length=self.context.piece_length)
        except ProtocolError as exc:
            self._fail(str(exc))
            raise

        if isinstance(message, PieceMessage) and self.on_block is not None:
            self.on_block(self, message)
        elif announced and isinstance(message, Have) and self.on_have is not None:
            # Only a *new* piece changes rarity; a repeated have is a no-op.
            self.on_have(self, message.index)
        elif isinstance(message, Request) and self.on_request is not None:
            self.on_request(self, message)
        elif isinstance(message, Cancel) and self.on_cancel is not None:
            self.on_cancel(self, message)
        elif isinstance(message, Extended):
            self._handle_extended(message)

        if event is None:
            return
        data = self._event_data()
        if event is EventType.PEER_BITFIELD:
            data["pieces"] = self.session.bitfield.count
            data["complete"] = self.session.is_seed
        self._emit(event, str(message), level=logging.DEBUG, data=data)

    # -------------------------------------------------------------- extensions

    def _our_handshake(self) -> Handshake:
        """The handshake we send: the extension bit only if we have extensions."""
        return outgoing_handshake(
            self.context.info_hash,
            self.our_peer_id,
            extensions=bool(self.extensions.ours),
        )

    async def _negotiate_extensions(self, they_support: bool) -> None:
        """Send our BEP 10 handshake, if both sides asked for one.

        Their reply arrives on the read loop like any other message; nothing here
        waits for it, because a peer that never answers simply never gets an
        extension message from us.
        """
        if not they_support or not self.extensions.ours or self.stream is None:
            return
        body = self.extensions.encode_handshake(version=client_version_string())
        with contextlib.suppress(PeerError):
            # A peer that hangs up mid-negotiation is a peer that hung up; the
            # read loop reports it, and losing the extension is not a failure.
            await self.stream.send(Extended(HANDSHAKE_ID, body))

    def _handle_extended(self, message: Extended) -> None:
        """Translate one extension message from the peer's id to a name."""
        if message.extension_id == HANDSHAKE_ID:
            try:
                self.extensions.note_theirs(decode_handshake(message.payload))
            except ExtensionError as exc:
                # A malformed handshake costs us the extensions, not the
                # connection: the peer may still be perfectly good at pieces.
                logger.debug("extension handshake from %s was unusable: %s", self.address, exc)
                return
            self._emit(
                EventType.PEER_HANDSHAKE,
                f"extensions from {self.address}: {', '.join(self.extensions.shared()) or 'none'}",
                level=logging.DEBUG,
                data={
                    "extensions": ",".join(self.extensions.shared()),
                    "client_version": self.extensions.version,
                },
            )
            return

        name = self._name_for(message.extension_id)
        if name is None:
            # An id we never advertised. Either the peer is confused or it is
            # probing; dropping the message is the correct answer to both.
            logger.debug(
                "peer %s sent extension id %d we did not offer", self.address, message.extension_id
            )
            return
        if self.on_extension is not None:
            self.on_extension(self, name, message.payload)

    def _name_for(self, extension_id: int) -> str | None:
        """Which of our extensions an incoming id refers to."""
        for name, identifier in self.extensions.ours.items():
            if identifier == extension_id:
                return name
        return None

    async def send_extension(self, name: str, payload: bytes) -> bool:
        """Send one extension message. Returns whether it went.

        False means the peer never advertised ``name``, or its handshake has not
        arrived yet, or the write failed. Not an exception: an extension is
        optional by definition, and a caller that treats its absence as fatal
        would drop perfectly good piece traffic over it.
        """
        if not self.extensions.can_send(name) or self.stream is None:
            return False
        identifier = self.extensions.their_id(name)
        if identifier is None:
            return False
        try:
            await self.send(Extended(identifier, payload))
        except PeerError as exc:
            logger.debug("extension %s to %s failed: %s", name, self.address, exc)
            return False
        return True

    def _note_sent(self, message: Message) -> None:
        """Flip our side of the state only after a message is on the wire."""
        match message:
            case Interested():
                self.session.am_interested = True
            case NotInterested():
                self.session.am_interested = False
            case Choke():
                self.session.am_choking = True
            case Unchoke():
                self.session.am_choking = False

    async def _keepalive_loop(self) -> None:
        """Send a keep-alive whenever the connection has gone quiet."""
        interval = self.config.keepalive_interval
        try:
            while True:
                await asyncio.sleep(interval)
                if self.closed or self.stream is None:
                    return
                if self.stream.idle_for >= interval:
                    await self.stream.send_keep_alive()
        except PeerError:
            return  # the read loop will report the failure
        except asyncio.CancelledError:
            return

    async def _finish(self) -> None:
        """Tear down after the read loop ends, and report the disconnect."""
        await self._stop_tasks()
        if self.stream is not None:
            await self.stream.aclose()
        was_connected = self.connected_at is not None
        self.session.mark_closed()
        if was_connected:
            self._emit(
                EventType.PEER_DISCONNECTED,
                f"disconnected: {self.disconnect_reason}",
                level=logging.DEBUG,
                data=self._event_data(),
            )
        self._closed.set()

    async def _stop_tasks(self) -> None:
        """Cancel the keep-alive and read-loop tasks, if running.

        The current task is skipped: :meth:`run` tears down through here too,
        and a task awaiting itself raises ``RuntimeError``.
        """
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._keepalive_task, self._run_task)
            if task is not None and task is not current
        ]
        self._keepalive_task = None
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _event_data(self) -> dict[str, object]:
        """Fields every peer event carries, so the UI can group by peer."""
        return {
            "host": self.address.host,
            "port": self.address.port,
            "peer": f"{self.address.host}:{self.address.port}",
            "client": self.client,
            "source": self.address.source,
        }

    def _fail(self, reason: str) -> None:
        """Publish a failure event."""
        self._emit(
            EventType.PEER_FAILED,
            f"{self.address}: {reason}",
            level=logging.WARNING,
            data=self._event_data() | {"reason": reason},
        )

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
