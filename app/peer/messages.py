"""Peer wire messages (BEP 3, TRD §24).

Every message after the handshake is a length-prefixed frame:

```
[ 4-byte length ][ 1-byte id ][ payload ]
```

A **length of zero is a keep-alive**: no id, no payload. Get this wrong and you
read the next message's id byte as a length prefix, which desynchronises the
stream — one of the most common bugs in hand-rolled clients.

Two deliberate design choices:

**Messages are immutable value objects that validate on construction.** Nobody
can build a ``Request(length=0)`` or a ``Have(index=-1)`` and put it on the
wire, and everything a peer sends goes through the same constructors, so
validation cannot be bypassed by taking a different code path.

**Range checks stop here; per-torrent checks do not.** A message cannot know
how many pieces the torrent has, so ``index`` is only checked for being
non-negative. The `piece_count` and `piece_length` checks happen in
:mod:`app.peer.state`, which does know.

Payload sizes are validated exactly, not "at least": a ``have`` message with
five bytes is not a ``have`` message with a typo, it is a peer we should not
trust.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from app.core.constants import (
    HAVE_PAYLOAD_LENGTH,
    MAX_BLOCK_SIZE,
    MAX_PORT,
    MESSAGE_HEADER_LENGTH,
    MIN_PORT,
    MSG_BITFIELD,
    MSG_CANCEL,
    MSG_CHOKE,
    MSG_EXTENDED,
    MSG_HAVE,
    MSG_INTERESTED,
    MSG_NOT_INTERESTED,
    MSG_PIECE,
    MSG_PORT,
    MSG_REQUEST,
    MSG_UNCHOKE,
    PIECE_HEADER_LENGTH,
    REQUEST_PAYLOAD_LENGTH,
)
from app.peer.errors import MessageError

_REQUEST_STRUCT: Final[struct.Struct] = struct.Struct(">III")
_PIECE_HEADER_STRUCT: Final[struct.Struct] = struct.Struct(">II")


@dataclass(frozen=True, slots=True)
class KeepAlive:
    """Keep-alive: a zero-length frame that carries no id."""

    def __str__(self) -> str:
        return "KeepAlive"


@dataclass(frozen=True, slots=True)
class Choke:
    """The peer will not send us pieces until it unchokes us."""

    def __str__(self) -> str:
        return "Choke"


@dataclass(frozen=True, slots=True)
class Unchoke:
    """The peer is willing to serve us pieces."""

    def __str__(self) -> str:
        return "Unchoke"


@dataclass(frozen=True, slots=True)
class Interested:
    """The peer wants pieces we have."""

    def __str__(self) -> str:
        return "Interested"


@dataclass(frozen=True, slots=True)
class NotInterested:
    """The peer has everything it wants from us."""

    def __str__(self) -> str:
        return "NotInterested"


@dataclass(frozen=True, slots=True)
class Have:
    """The peer completed a piece.

    Args:
        index: Piece index. Non-negative; upper bounds are checked by the
            session, which knows the torrent's piece count.
    """

    index: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise MessageError(f"piece index must not be negative, got {self.index}")

    def __str__(self) -> str:
        return f"Have(index={self.index})"


@dataclass(frozen=True, slots=True)
class Bitfield:
    """The peer's piece availability, one bit per piece.

    The payload is kept as raw bytes because its meaning depends on the
    torrent's piece count; :meth:`app.peer.bitfield.Bitfield.from_bytes`
    interprets it once that is known. Spare bits are tolerated on input and
    cleared on output.

    Args:
        data: Bitfield bytes, at least ``ceil(piece_count / 8)`` long.
    """

    data: bytes

    def __post_init__(self) -> None:
        if not self.data:
            raise MessageError("bitfield payload must not be empty")

    def __str__(self) -> str:
        return f"Bitfield({len(self.data)} bytes)"


@dataclass(frozen=True, slots=True)
class Request:
    """Ask for one block of a piece.

    Args:
        index: Piece index.
        begin: Byte offset within the piece.
        length: Block size; 1 to :data:`MAX_BLOCK_SIZE`.
    """

    index: int
    begin: int
    length: int

    def __post_init__(self) -> None:
        _validate_block(self, "request")

    def __str__(self) -> str:
        return f"Request(index={self.index}, begin={self.begin}, length={self.length})"


@dataclass(frozen=True, slots=True)
class Piece:
    """A block of piece data, sent in response to a :class:`Request`.

    Args:
        index: Piece index.
        begin: Byte offset within the piece.
        data: The block bytes.
    """

    index: int
    begin: int
    data: bytes

    def __post_init__(self) -> None:
        if self.index < 0:
            raise MessageError(f"piece index must not be negative, got {self.index}")
        if self.begin < 0:
            raise MessageError(f"block offset must not be negative, got {self.begin}")
        if not self.data:
            raise MessageError("piece payload must not be empty")
        if len(self.data) > MAX_BLOCK_SIZE:
            raise MessageError(
                f"piece payload of {len(self.data)} bytes exceeds the "
                f"{MAX_BLOCK_SIZE} byte block limit"
            )

    def __str__(self) -> str:
        return f"Piece(index={self.index}, begin={self.begin}, length={len(self.data)})"


@dataclass(frozen=True, slots=True)
class Cancel:
    """Withdraw a :class:`Request` we no longer want (endgame, or a hangup)."""

    index: int
    begin: int
    length: int

    def __post_init__(self) -> None:
        _validate_block(self, "cancel")

    def __str__(self) -> str:
        return f"Cancel(index={self.index}, begin={self.begin}, length={self.length})"


@dataclass(frozen=True, slots=True)
class Extended:
    """An extension-protocol message (BEP 10).

    The wire form is one byte naming which extension is speaking, then a
    bencoded payload whose shape each extension defines. Id 0 is the extension
    handshake itself; the ids for everything else are negotiated in it, which
    is why this message carries an id chosen by the *sender* rather than one we
    can hard-code.

    Attributes:
        extension_id: Which extension this is, as the sender numbered it. 0 is
            the handshake.
        payload: The bencoded body. For ``ut_metadata`` (BEP 9) the body is
            usually followed by raw metadata bytes, which do not belong to the
            bencoded value — see
            :mod:`app.peer.metadata_exchange`.
    """

    extension_id: int
    payload: bytes = b""

    def __post_init__(self) -> None:
        if not 0 <= self.extension_id <= 255:
            raise MessageError(f"extension id {self.extension_id} is outside 0-255")

    def __str__(self) -> str:
        return f"Extended(id={self.extension_id}, {len(self.payload)} bytes)"


@dataclass(frozen=True, slots=True)
class Port:
    """The peer's DHT port (BEP 5). Recorded, and used to seed the DHT."""

    port: int

    def __post_init__(self) -> None:
        if not MIN_PORT <= self.port <= MAX_PORT:
            raise MessageError(f"DHT port {self.port} is outside {MIN_PORT}-{MAX_PORT}")

    def __str__(self) -> str:
        return f"Port(port={self.port})"


Message = (
    KeepAlive
    | Choke
    | Unchoke
    | Interested
    | NotInterested
    | Have
    | Bitfield
    | Request
    | Piece
    | Cancel
    | Port
    | Extended
)
"""Every message the wire protocol defines."""

MESSAGE_NAMES: Final[dict[int, str]] = {
    MSG_CHOKE: "choke",
    MSG_UNCHOKE: "unchoke",
    MSG_INTERESTED: "interested",
    MSG_NOT_INTERESTED: "not_interested",
    MSG_HAVE: "have",
    MSG_BITFIELD: "bitfield",
    MSG_REQUEST: "request",
    MSG_PIECE: "piece",
    MSG_CANCEL: "cancel",
    MSG_PORT: "port",
    MSG_EXTENDED: "extended",
}


def _validate_block(message: Request | Cancel, name: str) -> None:
    """Shared validation for ``request`` and ``cancel`` payloads."""
    if message.index < 0:
        raise MessageError(f"piece index must not be negative, got {message.index}")
    if message.begin < 0:
        raise MessageError(f"block offset must not be negative, got {message.begin}")
    if not 1 <= message.length <= MAX_BLOCK_SIZE:
        raise MessageError(f"{name} length {message.length} is outside 1-{MAX_BLOCK_SIZE}")


def encode(message: Message) -> bytes:
    """Serialise a message to a complete wire frame, length prefix included.

    Args:
        message: Any peer message.

    Returns:
        The frame: 4-byte length, then id and payload.

    Raises:
        MessageError: If the message cannot be encoded.
    """
    body = _encode_body(message)
    return len(body).to_bytes(MESSAGE_HEADER_LENGTH, "big") + body


def decode(body: bytes) -> Message:
    """Decode a frame body: one id byte followed by its payload.

    Args:
        body: The bytes after the 4-byte length prefix. Empty means keep-alive.

    Returns:
        The decoded message.

    Raises:
        MessageError: If the id is unknown or the payload size is wrong.
    """
    if not body:
        return KeepAlive()

    message_id = body[0]
    payload = body[1:]

    match message_id:
        case _ if message_id == MSG_CHOKE:
            _require_exact(payload, 0, "choke")
            return Choke()
        case _ if message_id == MSG_UNCHOKE:
            _require_exact(payload, 0, "unchoke")
            return Unchoke()
        case _ if message_id == MSG_INTERESTED:
            _require_exact(payload, 0, "interested")
            return Interested()
        case _ if message_id == MSG_NOT_INTERESTED:
            _require_exact(payload, 0, "not_interested")
            return NotInterested()
        case _ if message_id == MSG_HAVE:
            _require_exact(payload, HAVE_PAYLOAD_LENGTH, "have")
            return Have(int.from_bytes(payload, "big"))
        case _ if message_id == MSG_BITFIELD:
            return Bitfield(payload)
        case _ if message_id == MSG_REQUEST:
            _require_exact(payload, REQUEST_PAYLOAD_LENGTH, "request")
            index, begin, length = _REQUEST_STRUCT.unpack(payload)
            return Request(index, begin, length)
        case _ if message_id == MSG_PIECE:
            if len(payload) <= PIECE_HEADER_LENGTH:
                raise MessageError("piece message carries no block data")
            index, begin = _PIECE_HEADER_STRUCT.unpack(payload[:PIECE_HEADER_LENGTH])
            return Piece(index, begin, bytes(payload[PIECE_HEADER_LENGTH:]))
        case _ if message_id == MSG_CANCEL:
            _require_exact(payload, REQUEST_PAYLOAD_LENGTH, "cancel")
            index, begin, length = _REQUEST_STRUCT.unpack(payload)
            return Cancel(index, begin, length)
        case _ if message_id == MSG_PORT:
            _require_exact(payload, 2, "port")
            return Port(int.from_bytes(payload, "big"))
        case _ if message_id == MSG_EXTENDED:
            if not payload:
                raise MessageError("extended message names no extension")
            return Extended(payload[0], bytes(payload[1:]))
        case _:
            raise MessageError(f"unknown message id {message_id}")


def message_name(message: Message | int) -> str:
    """A log-friendly name for a message or a raw message id."""
    if isinstance(message, int):
        return MESSAGE_NAMES.get(message, f"unknown({message})")
    return type(message).__name__.lower()


def _encode_body(message: Message) -> bytes:
    """Serialise the id and payload, without the length prefix."""
    match message:
        case KeepAlive():
            return b""
        case Choke():
            return bytes((MSG_CHOKE,))
        case Unchoke():
            return bytes((MSG_UNCHOKE,))
        case Interested():
            return bytes((MSG_INTERESTED,))
        case NotInterested():
            return bytes((MSG_NOT_INTERESTED,))
        case Have():
            return bytes((MSG_HAVE,)) + message.index.to_bytes(4, "big")
        case Bitfield():
            return bytes((MSG_BITFIELD,)) + message.data
        case Request():
            return bytes((MSG_REQUEST,)) + _REQUEST_STRUCT.pack(
                message.index, message.begin, message.length
            )
        case Piece():
            return (
                bytes((MSG_PIECE,))
                + _PIECE_HEADER_STRUCT.pack(message.index, message.begin)
                + message.data
            )
        case Cancel():
            return bytes((MSG_CANCEL,)) + _REQUEST_STRUCT.pack(
                message.index, message.begin, message.length
            )
        case Port():
            return bytes((MSG_PORT,)) + message.port.to_bytes(2, "big")
        case Extended():
            return bytes((MSG_EXTENDED, message.extension_id)) + message.payload
        case _:  # pragma: no cover - the type system already excludes this
            raise MessageError(f"cannot encode unsupported message {message!r}")


def _require_exact(payload: bytes, size: int, name: str) -> None:
    """Reject a payload whose size is not exactly what the message defines."""
    if len(payload) != size:
        raise MessageError(f"{name} message must carry {size} payload bytes, got {len(payload)}")
