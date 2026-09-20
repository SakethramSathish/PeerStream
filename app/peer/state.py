"""Per-peer protocol state (TRD §22).

This is everything we know about one peer *except its socket*, which keeps it
testable without networking: feed it messages, assert on the result. M5's
:class:`~app.peer.connection.PeerConnection` will own a stream and one of these.

It answers the questions the engine actually asks:

* **Can I request from this peer?** Only if we are interested and it is not
  choking us. Both flags start pessimistic (we choke, they choke) because the
  protocol does: nothing flows until each side says so.
* **What can I request?** The pieces it holds that we do not
  (:meth:`PeerSession.wanted_from`).
* **Is this peer worth a slot?** Derived from its bitfield and its rates.

:meth:`PeerSession.apply` is the only place incoming messages change state. It
returns the :class:`~app.core.events.EventType` the message caused, or ``None``,
so M5 can publish exactly one event per meaningful transition instead of
sprinkling event emission through the read loop.

Range checking lives here rather than in :mod:`app.peer.messages` because only
this layer knows the torrent's piece count and piece length — a ``have`` for
piece 9,999 is not a malformed message in isolation, it is malformed for a
torrent with 32 pieces.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.events import EventType
from app.peer.bitfield import Bitfield
from app.peer.errors import MessageError, ProtocolError
from app.peer.messages import (
    Bitfield as BitfieldMessage,
)
from app.peer.messages import (
    Cancel,
    Choke,
    Extended,
    Have,
    Interested,
    KeepAlive,
    Message,
    NotInterested,
    Piece,
    Port,
    Request,
    Unchoke,
)

logger = logging.getLogger(__name__)


class ConnectionState(StrEnum):
    """Where a peer connection is in its lifecycle."""

    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    CONNECTED = "connected"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(slots=True)
class PeerSession:
    """Protocol state for one peer.

    Args:
        piece_count: Pieces in the torrent; the bitfield is sized from it.
        peer_id: The peer's id, known after the handshake.
        client: Client name inferred from the peer id, for the peers tab.
        state: Lifecycle position.
        am_choking: Whether we are refusing to upload to this peer.
        am_interested: Whether we have told the peer we want its pieces.
        peer_choking: Whether the peer is refusing to upload to us.
        peer_interested: Whether the peer wants our pieces.
    """

    piece_count: int
    peer_id: bytes | None = None
    client: str = "Unknown"
    state: ConnectionState = ConnectionState.CONNECTING
    am_choking: bool = True
    am_interested: bool = False
    peer_choking: bool = True
    peer_interested: bool = False
    uploaded: int = 0
    downloaded: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    latency_ms: float | None = None
    bitfield: Bitfield = field(init=False)
    _bitfield_received: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if self.piece_count <= 0:
            raise ValueError(f"piece_count must be positive, got {self.piece_count}")
        self.bitfield = Bitfield(self.piece_count)

    # ------------------------------------------------------------- lifecycle

    def note_handshake(self, peer_id: bytes, client: str) -> None:
        """Record a completed handshake and move to CONNECTED."""
        self.peer_id = peer_id
        self.client = client
        self.state = ConnectionState.CONNECTED
        self.touch()

    def mark_closed(self) -> None:
        """Mark the connection closed."""
        self.state = ConnectionState.CLOSED

    def touch(self) -> None:
        """Record that we heard from the peer just now."""
        self.last_activity = time.monotonic()

    @property
    def idle_for(self) -> float:
        """Seconds since the last message from this peer."""
        return time.monotonic() - self.last_activity

    @property
    def closed(self) -> bool:
        """Whether the connection has finished."""
        return self.state in (ConnectionState.CLOSING, ConnectionState.CLOSED)

    # ---------------------------------------------------------------- inputs

    def apply(self, message: Message, *, piece_length: int | None = None) -> EventType | None:
        """Fold one incoming message into the session state.

        Args:
            message: The decoded message.
            piece_length: Piece size, when known. Enables the check that a
                block stays inside its piece.

        Returns:
            The event type this message caused, or ``None`` when the message
            changed nothing a listener needs to know about.

        Raises:
            ProtocolError: If the message violates ordering (a second
                bitfield) or exceeds the torrent's bounds.
            MessageError: If a block runs past the end of its piece.
        """
        self.touch()

        match message:
            case KeepAlive():
                return None
            case Choke():
                self.peer_choking = True
                return EventType.PEER_CHOKED
            case Unchoke():
                self.peer_choking = False
                return EventType.PEER_UNCHOKED
            case Interested():
                self.peer_interested = True
                return EventType.PEER_INTERESTED
            case NotInterested():
                self.peer_interested = False
                return None
            case Have():
                self._validate_index(message.index)
                if not self.bitfield.has(message.index):
                    self.bitfield.set(message.index)
                return None
            case BitfieldMessage():
                self._apply_bitfield(message)
                return EventType.PEER_BITFIELD
            case Piece():
                self._validate_block(message.index, message.begin, len(message.data), piece_length)
                self.downloaded += len(message.data)
                return EventType.PIECE_BLOCK_RECEIVED
            case Request() | Cancel() | Port() | Extended():
                # Upload-side, DHT and extension messages: none of them change
                # what we hold. The extension ones are answered by the metadata
                # exchange (BEP 9), which drives its own connection.
                return None
            case _:  # pragma: no cover - the type system already excludes this
                raise ProtocolError(f"unhandled message {message!r}")

    def note_upload(self, length: int) -> None:
        """Record bytes we sent, for the choking policy to rank this peer by.

        The mirror of what :meth:`apply` does for incoming blocks: tit-for-tat
        is arithmetic on what each side actually transferred, so both counters
        live together and both are counted, never estimated.
        """
        if length < 0:
            raise ValueError(f"length must not be negative, got {length}")
        self.uploaded += length
        self.touch()

    # -------------------------------------------------------------- queries

    @property
    def can_request(self) -> bool:
        """Whether we may request blocks: interested and not choked."""
        return self.am_interested and not self.peer_choking

    def wanted_from(self, ours: Bitfield) -> list[int]:
        """Pieces this peer holds that we still need."""
        return ours.missing_from(self.bitfield)

    def is_interesting(self, ours: Bitfield) -> bool:
        """Whether this peer holds something we lack."""
        return ours.is_interesting(self.bitfield)

    @property
    def progress(self) -> float:
        """Fraction of the torrent this peer holds, from 0.0 to 1.0."""
        return self.bitfield.count / self.piece_count

    @property
    def is_seed(self) -> bool:
        """Whether the peer has every piece."""
        return self.bitfield.complete

    # --------------------------------------------------------------- counters

    def record_download(self, count: int) -> None:
        """Add to the downloaded byte counter."""
        self.downloaded += count
        self.touch()

    def record_upload(self, count: int) -> None:
        """Add to the uploaded byte counter."""
        self.uploaded += count
        self.touch()

    def __str__(self) -> str:
        identity = (
            self.client if self.peer_id is None else f"{self.client}/{self.peer_id.hex()[:8]}"
        )
        return (
            f"PeerSession({identity}, state={self.state}, "
            f"pieces={self.bitfield.count}/{self.piece_count}, "
            f"choked={self.peer_choking}, interested={self.am_interested})"
        )

    # -------------------------------------------------------------- internals

    def _apply_bitfield(self, message: BitfieldMessage) -> None:
        """Install the peer's bitfield, once."""
        if self._bitfield_received:
            raise ProtocolError("peer sent a second bitfield; the first is authoritative")
        self.bitfield = Bitfield.from_bytes(message.data, self.piece_count)
        self._bitfield_received = True

    def _validate_index(self, index: int) -> None:
        if not 0 <= index < self.piece_count:
            raise ProtocolError(
                f"piece index {index} is outside 0-{self.piece_count - 1} for this torrent"
            )

    def _validate_block(
        self, index: int, begin: int, length: int, piece_length: int | None
    ) -> None:
        """Check that a block lies inside its piece.

        ``begin`` is already known non-negative: :class:`Piece` rejects a
        negative offset when it is constructed, so there is no second check.
        """
        self._validate_index(index)
        if piece_length is None:
            return
        if begin >= piece_length:
            raise MessageError(
                f"block offset {begin} starts past the end of a {piece_length} byte piece"
            )
        if begin + length > piece_length:
            raise MessageError(
                f"block {begin}+{length} runs past the end of a {piece_length} byte piece"
            )
