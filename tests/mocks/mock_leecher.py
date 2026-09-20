"""A leecher: the other half of the conversation, on a real socket.

`MockPeer` is a seeder — it answers requests. Uploading needs the opposite: a
peer that connects to *us*, says it is interested, asks for blocks, and checks
what comes back. This is that peer, and it is as real as the rest of the
harness: a TCP connection, a handshake, real messages.

It also plays the part of a badly behaved peer when a test asks it to, because
"a stranger sends us a request for a piece we do not have" is not a
hypothetical — it is Tuesday.

Example::

    leecher = MockLeecher(info_hash=..., piece_length=..., payload=payload)
    await leecher.connect("127.0.0.1", port)
    await leecher.interested()
    await leecher.request(0, 0, 16 * 1024)
    blocks = await leecher.wait_for_blocks(1)
    assert leecher.valid    # the bytes match the payload
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.constants import DEFAULT_HANDSHAKE_TIMEOUT, DEFAULT_PIECE_LENGTH
from app.peer.errors import PeerError
from app.peer.handshake import outgoing_handshake
from app.peer.messages import (
    Cancel,
    Choke,
    Have,
    Interested,
    Message,
    NotInterested,
    Request,
    Unchoke,
)
from app.peer.messages import Piece as PieceMessage
from app.peer.protocol import PeerStream

MOCK_LEECHER_ID: bytes = b"-LT8000-mockleecher!"


@dataclass(slots=True)
class MockLeecher:
    """A peer that downloads from us.

    Args:
        info_hash: The torrent's info hash, sent in the handshake.
        piece_length: Piece length of the torrent, used to check blocks.
        payload: The real bytes of the torrent, if the leecher should verify
            what it is given.
        peer_id: Peer id to announce.
        wrong_info_hash: Announce a different hash, to test rejection at the
            listener.
        silent: Connect but never send the handshake, to test timeouts.
    """

    info_hash: bytes
    piece_length: int = DEFAULT_PIECE_LENGTH
    payload: bytes | None = None
    peer_id: bytes = MOCK_LEECHER_ID
    wrong_info_hash: bytes | None = None
    silent: bool = False

    stream: PeerStream | None = field(default=None, init=False)
    received: list[Message] = field(default_factory=list, init=False)
    sent: list[Message] = field(default_factory=list, init=False)
    connected_at: float | None = field(default=None, init=False)
    handshake_failed: bool = field(default=False, init=False)

    # ------------------------------------------------------------- lifecycle

    async def connect(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    ) -> bool:
        """Dial ``host:port`` and complete the handshake.

        Returns:
            True if the handshake succeeded. A failure is recorded rather than
            raised, because several tests connect to a listener that is
            supposed to refuse them.
        """
        self.stream = await PeerStream.connect(host, port, timeout=5.0)
        if self.silent:
            # Hold the socket open and say nothing: the listener must notice.
            self.connected_at = time.monotonic()
            return True
        try:
            await self.stream.perform_handshake(
                outgoing_handshake(self.wrong_info_hash or self.info_hash, self.peer_id),
                timeout=timeout,
            )
        except PeerError:
            self.handshake_failed = True
            await self.aclose()
            return False
        self.connected_at = time.monotonic()
        self._start_reading()
        return True

    def _start_reading(self) -> None:
        """Begin collecting messages in the background."""
        task = asyncio.create_task(self._read_loop())
        self._reader = task

    _reader: asyncio.Task[None] | None = None

    async def _read_loop(self) -> None:
        """Collect everything the peer sends us."""
        if self.stream is None:
            return
        try:
            async for message in self.stream.messages():
                self.received.append(message)
        except PeerError:
            return

    async def aclose(self) -> None:
        """Close the connection."""
        task = getattr(self, "_reader", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.stream is not None:
            await self.stream.aclose()
            self.stream = None

    async def __aenter__(self) -> MockLeecher:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- actions

    async def send(self, message: Message) -> None:
        """Send one message."""
        if self.stream is None:
            raise PeerError("not connected")
        await self.stream.send(message)
        self.sent.append(message)

    async def interested(self, interested: bool = True) -> None:
        """Say whether we want pieces."""
        await self.send(Interested() if interested else NotInterested())

    async def request(self, index: int, begin: int, length: int) -> None:
        """Ask for one block."""
        await self.send(Request(index=index, begin=begin, length=length))

    async def cancel(self, index: int, begin: int, length: int) -> None:
        """Take a request back."""
        await self.send(Cancel(index=index, begin=begin, length=length))

    async def request_piece(self, index: int, block_size: int = 16 * 1024) -> int:
        """Ask for every block of one piece. Returns how many were asked for."""
        size = self.piece_size(index)
        offset = 0
        count = 0
        while offset < size:
            length = min(block_size, size - offset)
            await self.request(index, offset, length)
            offset += length
            count += 1
        return count

    # ---------------------------------------------------------------- queries

    def piece_size(self, index: int) -> int:
        """Size of a piece, from the payload when we have it."""
        if self.payload is None:
            return self.piece_length
        start = index * self.piece_length
        return max(0, min(self.piece_length, len(self.payload) - start))

    @property
    def blocks(self) -> list[PieceMessage]:
        """Every block message received."""
        return [message for message in self.received if isinstance(message, PieceMessage)]

    @property
    def unchoked(self) -> bool:
        """Whether an ``unchoke`` arrived without a later ``choke``."""
        state = False
        for message in self.received:
            if isinstance(message, Unchoke):
                state = True
            elif isinstance(message, Choke):
                state = False
        return state

    @property
    def haves(self) -> list[int]:
        """Piece indexes announced with ``have``."""
        return [message.index for message in self.received if isinstance(message, Have)]

    @property
    def bytes_received(self) -> int:
        """Payload bytes received in blocks (protocol overhead excluded)."""
        return sum(len(message.data) for message in self.blocks)

    @property
    def valid(self) -> bool:
        """Whether every block matches the real payload.

        True when there are no blocks yet and nothing to contradict: the
        question "has this peer lied to us" has an answer only once data has
        actually arrived.
        """
        if self.payload is None:
            return not self.blocks
        for message in self.blocks:
            start = message.index * self.piece_length + message.begin
            if self.payload[start : start + len(message.data)] != message.data:
                return False
        return True

    # ---------------------------------------------------------------- waiting

    async def wait_until(
        self, predicate: Callable[[MockLeecher], bool], *, timeout: float = 5.0
    ) -> bool:
        """Wait for a condition on the received messages."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self):
                return True
            await asyncio.sleep(0.01)
        return bool(predicate(self))

    async def wait_for_unchoke(self, *, timeout: float = 5.0) -> bool:
        """Wait to be unchoked."""
        return await self.wait_until(lambda peer: bool(peer.unchoked), timeout=timeout)

    async def wait_for_blocks(self, count: int, *, timeout: float = 5.0) -> list[PieceMessage]:
        """Wait for at least ``count`` blocks, and return what arrived."""
        await self.wait_until(lambda peer: len(peer.blocks) >= count, timeout=timeout)
        return self.blocks

    async def wait_for_have(self, index: int, *, timeout: float = 5.0) -> bool:
        """Wait for a specific piece to be announced."""
        return await self.wait_until(lambda peer: index in peer.haves, timeout=timeout)
