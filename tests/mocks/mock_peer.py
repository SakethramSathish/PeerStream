"""In-process peer that performs a real handshake and serves real pieces.

This is the seeder half of the protocol tests: it listens on loopback, speaks
the exact wire protocol over TCP, and serves blocks out of a real payload. It
is deliberately built on :class:`app.peer.protocol.PeerStream` and
:mod:`app.peer.messages` — the same code the client uses — so a bug in framing
cannot cancel itself out across the two ends of a test.

It is **not** a stub: the bits it returns came from the payload you give it, so
a test can hash them and know the whole path worked.

Runs on the caller's event loop, so start it from an async test or an
``asyncio.run`` block.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from app.core.constants import DEFAULT_PIECE_LENGTH
from app.peer.bitfield import Bitfield
from app.peer.errors import PeerError
from app.peer.extension import HANDSHAKE_ID, decode_handshake, encode_handshake
from app.peer.handshake import outgoing_handshake
from app.peer.messages import Bitfield as BitfieldMessage
from app.peer.messages import (
    Choke,
    Extended,
    Have,
    Interested,
    Message,
    NotInterested,
    Piece,
    Request,
    Unchoke,
)
from app.peer.protocol import PeerStream
from app.tracker.base import PeerAddress

MOCK_PEER_ID: bytes = b"-MP0100-" + b"0" * 12


class MockPeer:
    """A scriptable seeder over real TCP.

    Args:
        payload: The bytes to serve. Piece ``i`` is
            ``payload[i * piece_length : (i + 1) * piece_length]``.
        info_hash: Info hash to advertise in the handshake.
        piece_length: Piece size used to slice the payload.
        host: Bind address.
        port: Bind port; ``0`` picks a free port.
        peer_id: Peer id to advertise.
        bitfield: Pieces to claim; defaults to every piece in the payload.
        wrong_info_hash: Reply with a *different* info hash, so the client
            rejects us — the polite way to test the mismatch path.
        refuse_handshake: Accept the TCP connection and hang up without
            replying, to provoke a client-side timeout.
        choke: Answer ``interested`` with ``choke`` instead of ``unchoke``.
        serve_requests: When False, requests are ignored, which is how a peer
            that never sends data is simulated.
    """

    def __init__(
        self,
        payload: bytes,
        *,
        info_hash: bytes,
        piece_length: int = DEFAULT_PIECE_LENGTH,
        host: str = "127.0.0.1",
        port: int = 0,
        peer_id: bytes | None = None,
        bitfield: Bitfield | None = None,
        wrong_info_hash: bytes | None = None,
        refuse_handshake: bool = False,
        choke: bool = False,
        serve_requests: bool = True,
        request_delay: float = 0.0,
        extensions: Mapping[str, int] | None = None,
        pex_peers: Sequence[PeerAddress] = (),
        malformed_extension: bool = False,
        extension_port: int | None = None,
    ) -> None:
        """Build a seeder.

        Args:
            payload: The real data to serve.
            info_hash: The torrent's info hash, matched during handshake.
            piece_length: Piece length of the torrent.
            host: Interface to bind.
            port: Port to bind (0 picks a free one).
            peer_id: Peer id to announce.
            bitfield: Piece availability to announce (all pieces by default).
            wrong_info_hash: Announce a different hash, to test rejection.
            refuse_handshake: Refuse the handshake entirely.
            choke: Never unchoke, however polite the client is.
            serve_requests: Answer requests at all.
            request_delay: Seconds to wait before each block, for a seeder
                that is slow rather than broken — the case endgame mode exists
                to work around.
            extensions: BEP 10 map this peer advertises, name to the id it wants
                to *receive*. Omit it and the extension bit stays off, which is
                what a client that predates BEP 10 looks like.
            pex_peers: Contacts to volunteer in a ``ut_pex`` message once the
                extension handshake has completed.
            malformed_extension: Answer the extension handshake with bencode
                garbage, to see what a client does with a peer it cannot
                understand.
            extension_port: The ``p`` field of our handshake — the port we would
                like to be reached on, which for an incoming connection is the
                only dialable address the other side ever learns.
        """
        self.payload = payload
        self.info_hash = info_hash
        self.piece_length = piece_length
        self.host = host
        self.port = port
        self.peer_id = peer_id or MOCK_PEER_ID
        self.wrong_info_hash = wrong_info_hash
        self.refuse_handshake = refuse_handshake
        self.choke = choke
        self.serve_requests = serve_requests
        self.request_delay = request_delay
        self.extensions = dict(extensions or {})
        self.pex_peers = tuple(pex_peers)
        self.malformed_extension = malformed_extension
        self.extension_port = extension_port

        self.requests_served: int = 0
        self.connection_count: int = 0
        self.received: list[Message] = []
        self.extension_messages: list[tuple[int, bytes]] = []
        """Every ``Extended`` message a client sent us, id and body."""
        self.their_extensions: dict[str, int] = {}
        """The ``m`` map from the client's handshake, once it arrives."""
        self.client_supports_extensions: bool = False
        """Whether the client set BEP 10's bit in its reserved bytes."""
        self.client_version: str = ""
        """The ``v`` field the client sent, if it sent one."""

        self._bitfield = bitfield
        self._server: asyncio.Server | None = None
        self._streams: list[PeerStream] = []

    # ------------------------------------------------------------ accessors

    @property
    def piece_count(self) -> int:
        """How many pieces the payload fills."""
        if not self.payload:
            return 1
        return (len(self.payload) + self.piece_length - 1) // self.piece_length

    @property
    def bitfield(self) -> Bitfield:
        """What this peer claims to have."""
        if self._bitfield is None:
            self._bitfield = Bitfield.full(self.piece_count)
        return self._bitfield

    @property
    def address(self) -> PeerAddress:
        """The address a client should connect to."""
        return PeerAddress(host=self.host, port=self.port)

    @property
    def bytes_sent(self) -> int:
        """Total bytes written to clients across every connection."""
        return sum(stream.bytes_sent for stream in self._streams)

    @property
    def live_connections(self) -> int:
        """How many connections are still open."""
        return sum(1 for stream in self._streams if not stream.closed)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> PeerAddress:
        """Start listening and return the address to connect to."""
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or ()
        self.port = sockets[0].getsockname()[1]
        return self.address

    async def stop(self) -> None:
        """Close every connection and stop listening."""
        for stream in self._streams:
            await stream.aclose()
        self._streams.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> MockPeer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # -------------------------------------------------------------- actions

    async def send_have(self, index: int) -> int:
        """Announce a newly completed piece to every connected client.

        Returns:
            How many clients were told.
        """
        sent = 0
        for stream in list(self._streams):
            if stream.closed:
                continue
            try:
                await stream.send(Have(index=index))
            except PeerError:
                continue
            sent += 1
        return sent

    def block(self, index: int, begin: int, length: int) -> bytes | None:
        """The payload bytes for a requested block, or None if out of range."""
        start = index * self.piece_length + begin
        if begin < 0 or start >= len(self.payload):
            return None
        piece_end = min((index + 1) * self.piece_length, len(self.payload))
        return self.payload[start : min(start + length, piece_end)]

    # -------------------------------------------------------------- internals

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = PeerStream(reader, writer, timeout=30.0, label="mock-peer")
        self._streams.append(stream)
        self.connection_count += 1

        if self.refuse_handshake:
            await stream.aclose()
            return

        try:
            theirs = await stream.perform_handshake(
                outgoing_handshake(
                    self.wrong_info_hash or self.info_hash,
                    self.peer_id,
                    extensions=bool(self.extensions),
                ),
                timeout=5.0,
            )
        except PeerError:
            await stream.aclose()
            return

        self.client_supports_extensions = theirs.supports_extensions
        if self.extensions and theirs.supports_extensions:
            await self._send_extension_handshake(stream)

        try:
            await stream.send(BitfieldMessage(data=self.bitfield.to_bytes()))
            async for message in stream.messages():
                self.received.append(message)
                await self._respond(stream, message)
        except PeerError:
            pass
        finally:
            await stream.aclose()

    async def _send_extension_handshake(self, stream: PeerStream) -> None:
        """Answer BEP 10, and volunteer our peers if we were given any."""
        if self.malformed_extension:
            await stream.send(Extended(HANDSHAKE_ID, b"d3:m4:"))
            return
        await stream.send(
            Extended(
                HANDSHAKE_ID,
                encode_handshake(
                    self.extensions, version="mock-peer 1.0", port=self.extension_port
                ),
            )
        )
        # Our peers are volunteered from _respond, once the client's own
        # handshake has told us which id to use.

    async def send_pex(self, stream: PeerStream, peers: Sequence[PeerAddress]) -> None:
        """Volunteer contacts to one client, using the id *it* advertised.

        Which is the point: BEP 10 ids belong to the receiver, so a peer that
        sends with its own id is talking to itself.
        """
        from app.discovery.pex import UT_PEX, PexContact, encode_pex

        identifier = self.their_extensions.get(UT_PEX)
        if identifier is None:
            return
        await stream.send(
            Extended(
                identifier,
                encode_pex(added=[PexContact(address=peer, reachable=True) for peer in peers]),
            )
        )

    async def _respond(self, stream: PeerStream, message: Message) -> None:
        """Answer one client message the way a seeder would."""
        match message:
            case Extended(extension_id=int(identifier), payload=bytes(payload)):
                self.extension_messages.append((identifier, payload))
                if identifier == HANDSHAKE_ID:
                    try:
                        decoded = decode_handshake(payload)
                    except PeerError:
                        self.their_extensions = {}
                    else:
                        self.their_extensions = dict(decoded.extensions)
                        self.client_version = decoded.version
                    if self.pex_peers:
                        await self.send_pex(stream, self.pex_peers)
                return
            case Interested():
                await stream.send(Choke() if self.choke else Unchoke())
            case NotInterested():
                await stream.send(Choke())
            case Request():
                await self._serve(stream, message)
            case _:
                return

    async def _serve(self, stream: PeerStream, request: Request) -> None:
        """Serve one block, or hang up on a request we cannot satisfy."""
        if not self.serve_requests:
            return
        if self.request_delay:
            await asyncio.sleep(self.request_delay)
        data = self.block(request.index, request.begin, request.length)
        if data is None:
            # A peer asking for bytes outside the torrent is broken or hostile.
            await stream.aclose()
            return
        await stream.send(Piece(index=request.index, begin=request.begin, data=data))
        self.requests_served += 1
