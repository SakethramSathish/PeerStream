"""Fetching a torrent's info dictionary from a peer — BEP 9 over BEP 10.

A magnet link names a torrent without describing it: 20 bytes of hash and
nowhere to start. The info dictionary is the description, and BEP 9 is how you
ask a peer for it. It runs inside the extension protocol (BEP 10), which begins
with its own handshake:

1. We set the extension bit in the reserved bytes; the peer answers with its
   own handshake, an ``Extended`` message with id 0 whose bencoded body
   advertises ``m`` — a map of extension name to the id that peer numbers it
   with — and ``metadata_size``, the length of the info dictionary it holds.
   The id for ``ut_metadata`` is chosen by the sender, so nothing can be
   assumed: we read it out of their handshake or we do not speak at all.
2. We send ``{"msg_type": 0, "piece": n}`` for each 16 KiB piece. The peer
   answers ``{"msg_type": 1, "piece": n, "total_size": N}`` **followed by** the
   raw chunk — the bytes after the bencoded dictionary are not part of it,
   which is the one thing everyone gets wrong about this extension. Or it
   answers ``{"msg_type": 2, "piece": n}``, meaning "no".
3. We concatenate the chunks and check the SHA-1 against the info hash. This
   step is not a formality: the metadata arrives from a stranger, and the hash
   is the only thing that makes it *the torrent we asked for* rather than
   merely *a torrent*. Anything that does not hash to the magnet's info hash is
   discarded and the peer is reported as a failure.

The module also implements the answering side, because a client that asks for
metadata and refuses to serve it is the reason swarms are thin.

Typical use::

    result = await fetch_metadata(address, info_hash, peer_id=our_peer_id)
    torrent = torrent_from_info(result.info, announce=magnet.trackers)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha1
from typing import Final

from app.bencode import BencodeValue, decode, decode_prefix, encode
from app.bencode.errors import BencodeError
from app.core.constants import (
    INFO_HASH_SIZE,
    MAX_PORT,
    MIN_PORT,
    PEER_ID_SIZE,
)
from app.core.peer_id import generate_peer_id
from app.peer.errors import MetadataError, PeerError, PeerTimeoutError
from app.peer.extension import (
    UT_METADATA as EXTENSION_UT_METADATA,
)
from app.peer.extension import (
    ExtensionError,
    decode_handshake,
    encode_handshake,
)
from app.peer.handshake import outgoing_handshake
from app.peer.messages import Extended, Message
from app.peer.protocol import PeerStream

logger = logging.getLogger(__name__)

UT_METADATA: Final[str] = EXTENSION_UT_METADATA
"""The extension name BEP 9 defines for metadata exchange.

Re-exported from :mod:`app.peer.extension`, which owns the BEP 10 vocabulary:
one name, one spelling, whether the caller is fetching metadata or exchanging
peers.
"""

METADATA_PIECE_SIZE: Final[int] = 16 * 1024
"""Chunk size the protocol fixes for metadata transfer."""

MAX_METADATA_SIZE: Final[int] = 32 * 1024 * 1024
"""Largest info dictionary we will assemble.

Real info dictionaries are tens of kilobytes; a 100 000-piece torrent is about
two megabytes. A peer that claims more than this is either broken or trying to
make us allocate something enormous.
"""

MAX_METADATA_PIECES: Final[int] = 2048
"""Largest number of chunks we will request, which bounds the work per peer."""

REQUEST_PIPELINE: Final[int] = 4
"""How many chunks to have in flight.

Peers advertise ``reqq`` and drop bursts that exceed it; four is well inside
every client's limit and still keeps a slow link busy.
"""

METADATA_RETRY_SECONDS: Final[float] = 5.0
"""How long to wait before asking again for chunks that have not arrived.

UDP is not involved here, but chunks still go missing: a peer may drop a
request under load, or refuse one because its own queue was full. Re-asking is
cheap and a duplicate chunk is harmless, so a missing chunk costs five seconds
rather than the whole fetch.
"""

OUR_UT_METADATA_ID: Final[int] = 1
"""The id we number ``ut_metadata`` with in our own extension handshake."""

REQUEST: Final[int] = 0  # msg_type: "send me this piece"
DATA: Final[int] = 1  # msg_type: "here is the piece"
REJECT: Final[int] = 2  # msg_type: "I don't have it"

DEFAULT_METADATA_TIMEOUT: Final[float] = 20.0
"""Deadline for the whole exchange, generous because peers are slow to warm up."""


@dataclass(frozen=True, slots=True)
class MetadataResult:
    """A fetched info dictionary, already checked against its info hash.

    Attributes:
        info: The decoded info dictionary, verified to hash to the requested
            info hash.
        raw: The bytes the peer sent, canonical bencode of ``info``. Kept
            because serving metadata back to others must be byte-exact.
        address: The peer that supplied it.
        pieces: How many 16 KiB chunks it arrived in.
        elapsed: Wall-clock seconds the exchange took.
    """

    info: Mapping[bytes, BencodeValue]
    raw: bytes
    address: str
    pieces: int
    elapsed: float

    @property
    def size(self) -> int:
        """Size of the info dictionary, in bytes."""
        return len(self.raw)


@dataclass(frozen=True, slots=True)
class ExtensionHandshake:
    """What a peer told us it can do (BEP 10 handshake, id 0).

    Attributes:
        metadata_size: Length of the info dictionary the peer holds, or None
            when it did not say — which, for our purposes, means it has
            nothing we want.
        ut_metadata_id: The id *this peer* numbers ``ut_metadata`` with.
        client: The ``v`` string, usually the client name, when offered.
        reqq: The peer's stated request queue limit, when offered.
    """

    metadata_size: int | None = None
    ut_metadata_id: int | None = None
    client: str = ""
    reqq: int | None = None

    @property
    def can_serve_metadata(self) -> bool:
        """Whether this peer can be asked for metadata at all."""
        return self.ut_metadata_id is not None and self.metadata_size is not None

    @classmethod
    def from_payload(cls, payload: bytes) -> ExtensionHandshake:
        """Parse the bencoded body of an extension handshake.

        The general BEP 10 work — bencode, the ``m`` map, the optional fields —
        lives in :mod:`app.peer.extension`, because a connection exchanging
        peers needs exactly the same parsing. What is left here is the BEP 9
        reading of it: which id the peer numbers ``ut_metadata`` with, and
        whether it holds any metadata to serve.

        Args:
            payload: The bytes of an ``Extended`` message with id 0.

        Raises:
            MetadataError: If the body is not bencoded, is not a dictionary, or
                is too long to be a handshake.
        """
        try:
            decoded = decode_handshake(payload)
        except ExtensionError as exc:
            # Same sentence the caller has always seen; only the exception type
            # changes, and MetadataError is the one this module's callers catch.
            raise MetadataError(str(exc)) from exc
        return cls(
            metadata_size=decoded.metadata_size,
            ut_metadata_id=decoded.id_for(UT_METADATA),
            client=decoded.version,
            reqq=decoded.reqq,
        )

    def encode(self) -> bytes:
        """Serialise the handshake we send when fetching metadata.

        We advertise exactly one extension — ``ut_metadata`` — because that is
        the only one a metadata fetch answers. Advertising more would be a
        promise to answer messages we have no answer for.
        """
        return encode_handshake({UT_METADATA: OUR_UT_METADATA_ID})


# ---------------------------------------------------------------- the messages


def encode_request(piece: int) -> bytes:
    """Bencode a metadata request for one chunk."""
    return encode({b"msg_type": REQUEST, b"piece": piece})


def encode_data(piece: int, total_size: int, chunk: bytes) -> bytes:
    """Bencode a metadata reply and append the chunk it describes."""
    return encode({b"msg_type": DATA, b"piece": piece, b"total_size": total_size}) + chunk


def encode_reject(piece: int) -> bytes:
    """Bencode a refusal to serve one chunk."""
    return encode({b"msg_type": REJECT, b"piece": piece})


@dataclass(frozen=True, slots=True)
class MetadataMessage:
    """One parsed ``ut_metadata`` message.

    Attributes:
        kind: One of :data:`REQUEST`, :data:`DATA` or :data:`REJECT`.
        piece: Which chunk it concerns.
        total_size: The peer's notion of the whole metadata size, present on
            data messages.
        chunk: The bytes after the bencoded header, for a data message.
    """

    kind: int
    piece: int
    total_size: int | None = None
    chunk: bytes = b""

    @classmethod
    def decode(cls, payload: bytes) -> MetadataMessage:
        """Parse an ``ut_metadata`` message, header plus trailing bytes.

        Raises:
            MetadataError: If the header is not bencoded, is not a dictionary,
                or is missing a usable ``msg_type`` or ``piece``.
        """
        try:
            header, consumed = decode_prefix(payload)
        except BencodeError as exc:
            raise MetadataError(f"unreadable ut_metadata message: {exc}") from exc
        if not isinstance(header, dict):
            raise MetadataError("ut_metadata message header was not a dictionary")

        kind = header.get(b"msg_type")
        piece = header.get(b"piece")
        if not isinstance(kind, int):
            raise MetadataError("ut_metadata message has no msg_type")
        if not isinstance(piece, int) or piece < 0:
            raise MetadataError("ut_metadata message has no piece number")

        total_size = header.get(b"total_size")
        return cls(
            kind=kind,
            piece=piece,
            total_size=total_size if isinstance(total_size, int) else None,
            chunk=bytes(payload[consumed:]),
        )


def split_metadata(raw: bytes) -> list[bytes]:
    """Cut a bencoded info dictionary into protocol-sized chunks."""
    return [
        raw[offset : offset + METADATA_PIECE_SIZE]
        for offset in range(0, len(raw), METADATA_PIECE_SIZE)
    ]


async def fetch_metadata(
    host: str,
    port: int,
    info_hash: bytes,
    *,
    peer_id: bytes | None = None,
    timeout: float = DEFAULT_METADATA_TIMEOUT,
    dht_port: int | None = None,
) -> MetadataResult:
    """Fetch and verify a torrent's info dictionary from one peer.

    Args:
        host: The peer's address.
        port: The peer's port.
        info_hash: The 20-byte info hash we want the metadata for.
        peer_id: Our peer id; a random one is generated when omitted.
        timeout: Deadline for the whole exchange, in seconds.
        dht_port: Our DHT port, when we have one, so the peer can record us.

    Returns:
        The verified metadata, with the raw bytes as received.

    Raises:
        MetadataError: The peer cannot serve metadata, refuses, sends bytes
            that do not hash to ``info_hash``, or simply stops answering.
        PeerError: The connection or handshake failed.
    """
    if len(info_hash) != INFO_HASH_SIZE:
        raise MetadataError(f"info_hash must be {INFO_HASH_SIZE} bytes, got {len(info_hash)}")
    if not MIN_PORT <= port <= MAX_PORT:
        raise MetadataError(f"port {port} is outside {MIN_PORT}-{MAX_PORT}")

    our_peer_id = (
        peer_id if peer_id is not None and len(peer_id) == PEER_ID_SIZE else generate_peer_id()
    )
    label = f"{host}:{port}"
    started = time.monotonic()

    stream = await PeerStream.connect(host, port, timeout=timeout, read_timeout=timeout)
    try:
        handshake = await stream.perform_handshake(
            outgoing_handshake(info_hash, our_peer_id, dht=dht_port is not None, extensions=True),
            timeout=timeout,
        )
        if not handshake.supports_extensions:
            raise MetadataError(f"{label} does not speak the extension protocol (BEP 10)")

        # Our extension handshake: this is what makes the peer tell us the id
        # it numbers ut_metadata with, and the size of what it holds.
        our_extensions = ExtensionHandshake()
        await stream.send(Extended(0, our_extensions.encode()))

        theirs = await _await_extension_handshake(stream, timeout=timeout)
        if not theirs.can_serve_metadata:
            raise MetadataError(f"{label} has no metadata to offer")
        if theirs.metadata_size is None or theirs.metadata_size > MAX_METADATA_SIZE:
            raise MetadataError(f"{label} claims {theirs.metadata_size} bytes of metadata")
        assert theirs.ut_metadata_id is not None  # can_serve_metadata guarantees this

        raw = await _download(
            stream,
            ut_metadata_id=theirs.ut_metadata_id,
            size=theirs.metadata_size,
            label=label,
            timeout=timeout,
            our_id=OUR_UT_METADATA_ID,
        )

        if sha1(raw).digest() != info_hash:
            raise MetadataError(f"{label} sent metadata that does not hash to the info hash")

        try:
            info = decode(raw)
        except BencodeError as exc:
            raise MetadataError(f"{label} sent metadata that is not valid bencode: {exc}") from exc
        if not isinstance(info, dict):
            raise MetadataError(f"{label} sent metadata that is not a dictionary")

        result = MetadataResult(
            info=info,
            raw=raw,
            address=label,
            pieces=len(split_metadata(raw)),
            elapsed=time.monotonic() - started,
        )
        logger.info(
            "fetched %d bytes of metadata in %d pieces from %s in %.2fs",
            result.size,
            result.pieces,
            label,
            result.elapsed,
        )
        return result
    except PeerError:
        raise
    finally:
        await stream.aclose()


async def _await_extension_handshake(stream: PeerStream, *, timeout: float) -> ExtensionHandshake:
    """Read until the peer sends its extension handshake.

    Peers open with a flurry — bitfield, unchoke, have — and the extension
    handshake may be anywhere in it. Everything that is not the handshake is
    ignored rather than treated as an error, because a peer that sends a
    bitfield first is behaving perfectly well.

    Raises:
        MetadataError: If the deadline passes first.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MetadataError(f"{stream.label} never sent an extension handshake")
        try:
            message: Message = await stream.read_message(timeout=max(remaining, 0.1))
        except PeerError as exc:
            raise MetadataError(
                f"{stream.label} stopped before its extension handshake: {exc}"
            ) from exc
        if isinstance(message, Extended) and message.extension_id == 0:
            return ExtensionHandshake.from_payload(message.payload)


async def _download(
    stream: PeerStream,
    *,
    ut_metadata_id: int,
    size: int,
    label: str,
    timeout: float,
    our_id: int = OUR_UT_METADATA_ID,
) -> bytes:
    """Request every chunk and reassemble the metadata.

    A window of chunks is kept in flight, and re-asked for every
    :data:`METADATA_RETRY_SECONDS` until they arrive: peers drop requests under
    load, and a duplicate chunk is free where a stalled fetch is not. Each
    chunk is checked against the length it should have, because a peer that
    sent a short one would otherwise give us metadata that hashes to nothing.

    Raises:
        MetadataError: The peer rejected a piece, sent a wrong-sized chunk, or
            left the transfer unfinished by the deadline.
    """
    pieces = max(1, -(-size // METADATA_PIECE_SIZE))  # ceiling division
    if pieces > MAX_METADATA_PIECES:
        raise MetadataError(
            f"{label} claims metadata in {pieces} pieces, we allow {MAX_METADATA_PIECES}"
        )

    received: dict[int, bytes] = {}
    requested: set[int] = set()
    window_sent_at = 0.0
    deadline = time.monotonic() + timeout

    while len(received) < pieces:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MetadataError(
                f"{label} sent {len(received)} of {pieces} metadata pieces, then stopped"
            )

        missing = [index for index in range(pieces) if index not in received]
        in_flight = len(requested - set(received))
        to_send = [index for index in missing if index not in requested][
            : max(0, REQUEST_PIPELINE - in_flight)
        ]
        if not to_send and time.monotonic() - window_sent_at >= METADATA_RETRY_SECONDS:
            # Everything outstanding has been outstanding too long: ask again
            # for the earliest of it. A duplicate chunk is free.
            to_send = missing[:REQUEST_PIPELINE]
        if to_send:
            for index in to_send:
                await stream.send(Extended(ut_metadata_id, encode_request(index)))
                requested.add(index)
            window_sent_at = time.monotonic()

        message = await _read_metadata_message(
            stream,
            ut_metadata_id=ut_metadata_id,
            their_id=our_id,
            label=label,
            timeout=min(METADATA_RETRY_SECONDS, max(remaining, 0.1)),
        )
        if message is None:
            continue  # nothing arrived in this window; ask again

        if message.kind == REJECT:
            raise MetadataError(f"{label} rejected metadata piece {message.piece}")

        expected = min(METADATA_PIECE_SIZE, size - message.piece * METADATA_PIECE_SIZE)
        if message.piece < 0 or message.piece >= pieces:
            raise MetadataError(f"{label} sent metadata piece {message.piece} of {pieces}")
        if message.total_size is not None and message.total_size != size:
            raise MetadataError(
                f"{label} says the metadata is {message.total_size} bytes, it said {size} before"
            )
        if len(message.chunk) != expected:
            raise MetadataError(
                f"metadata piece {message.piece} from {label} was {len(message.chunk)} bytes, "
                f"expected {expected}"
            )
        received[message.piece] = message.chunk

    return b"".join(received[index] for index in range(pieces))


async def _read_metadata_message(
    stream: PeerStream,
    *,
    ut_metadata_id: int,
    label: str,
    timeout: float,
    their_id: int = OUR_UT_METADATA_ID,
) -> MetadataMessage | None:
    """Read the next ``ut_metadata`` reply, ignoring everything else.

    The reply may arrive under either of two ids, and which one is used is a
    detail this protocol hides: BEP 10 extension ids are per-peer, and a peer
    answering a request addresses it with the id **we** advertised, not the one
    it advertised in its own handshake. qBittorrent does exactly that, so
    matching only the id from the peer's handshake is how a client ends up
    timing out on a peer that answered it.

    A peer that asks *us* for metadata — allowed, and it happens when it wants
    the same torrent — is answered with a refusal and the read continues,
    because we are here to fetch, not to serve.

    Returns:
        The next data or rejection message, or ``None`` if nothing arrived
        before the deadline. A timeout is not a failure: the caller asks again.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            message: Message = await stream.read_message(timeout=max(remaining, 0.1))
        except PeerTimeoutError:
            return None
        except PeerError as exc:
            raise MetadataError(f"{label} stopped while sending metadata: {exc}") from exc
        if not isinstance(message, Extended):
            continue
        if message.extension_id not in {ut_metadata_id, their_id}:
            continue
        parsed = MetadataMessage.decode(message.payload)
        if parsed.kind == REQUEST:
            await stream.send(Extended(message.extension_id, encode_reject(parsed.piece)))
            continue
        return parsed


# ------------------------------------------------------------ the other side


class MetadataServer:
    """Answers ``ut_metadata`` requests for one torrent we hold.

    A peer that fetches metadata and never serves it adds a leech to the swarm.
    This is the small, self-contained half of that duty: given the raw info
    dictionary, answer requests for its chunks, and refuse politely when asked
    for something we do not have.

    The raw bytes are required, not the dictionary, because a re-encoding that
    differs by one byte would fail the requester's hash check.
    """

    def __init__(self, raw_info: bytes) -> None:
        if not raw_info:
            raise MetadataError("cannot serve empty metadata")
        self._raw = raw_info
        self._chunks: Sequence[bytes] = split_metadata(raw_info)

    @property
    def size(self) -> int:
        """Size of the metadata we serve."""
        return len(self._raw)

    @property
    def pieces(self) -> int:
        """How many chunks the metadata is served in."""
        return len(self._chunks)

    def handshake(self) -> bytes:
        """The extension handshake advertising what we can serve."""
        return encode(
            {
                b"m": {UT_METADATA.encode(): OUR_UT_METADATA_ID},
                b"metadata_size": len(self._raw),
            }
        )

    def answer(self, payload: bytes) -> bytes | None:
        """Answer one request.

        Args:
            payload: The body of an ``ut_metadata`` message, including any
                trailing bytes.

        Returns:
            The body of the reply — a data message with the chunk appended, or
            a rejection — or None when the message was not a request, in which
            case there is nothing to answer.
        """
        try:
            request = MetadataMessage.decode(payload)
        except MetadataError:
            logger.debug("ignoring an unreadable metadata request")
            return None
        if request.kind != REQUEST:
            return None
        if request.piece >= len(self._chunks):
            return encode_reject(request.piece)
        return encode_data(request.piece, len(self._raw), self._chunks[request.piece])


async def serve_metadata_request(
    stream: PeerStream,
    server: MetadataServer,
    message: Extended,
    *,
    their_id: int = OUR_UT_METADATA_ID,
) -> bool:
    """Answer one metadata request on the wire, if it is one.

    Args:
        stream: The connection the request arrived on.
        server: The metadata we hold.
        message: The message to answer.
        their_id: The id *the requesting peer* advertised for ``ut_metadata``.
            Replies are addressed with the requester's id, the same rule that
            makes :func:`fetch_metadata` accept two ids on the way back.

    Returns:
        True when a reply was sent.
    """
    reply = server.answer(message.payload)
    if reply is None:
        return False
    await stream.send(Extended(their_id, reply))
    return True


async def fetch_metadata_from_any(
    addresses: Sequence[tuple[str, int]],
    info_hash: bytes,
    *,
    peer_id: bytes | None = None,
    timeout: float = DEFAULT_METADATA_TIMEOUT,
    dht_port: int | None = None,
    attempts: int = 5,
) -> MetadataResult:
    """Try peers in order until one supplies verified metadata.

    Peers that do not speak the extension protocol, refuse, or go quiet are
    tried and discarded; the loop only ends in failure when every candidate has
    been exhausted. Errors from individual peers are logged, not raised,
    because on the open internet most peers in a swarm will not answer.

    Raises:
        MetadataError: No peer supplied metadata, or the list was empty.
    """
    if not addresses:
        raise MetadataError("no peers to ask for metadata")
    failures: list[str] = []
    for host, port in list(addresses)[:attempts]:
        try:
            return await fetch_metadata(
                host, port, info_hash, peer_id=peer_id, timeout=timeout, dht_port=dht_port
            )
        except (MetadataError, PeerError, OSError, TimeoutError) as exc:
            logger.debug("metadata fetch from %s:%d failed: %s", host, port, exc)
            failures.append(f"{host}:{port}: {exc}")
    raise MetadataError(
        f"no peer supplied metadata for {info_hash.hex()[:12]}; " + "; ".join(failures[:3])
    )
