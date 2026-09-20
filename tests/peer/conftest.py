"""Fixtures for peer protocol tests.

:class:`FakePeer` is a scripted TCP server: it performs a real handshake over a
real socket and then plays whatever bytes the test queues. Framing bugs —
partial reads, several messages in one segment, oversized length prefixes —
only show up when the two ends are genuinely separated by TCP, so the tests
that look for them use this instead of a stubbed reader.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence

import pytest
from app.core.constants import HANDSHAKE_LENGTH
from app.peer.handshake import Handshake
from app.peer.messages import Message, encode

TEST_INFO_HASH: bytes = bytes(range(20))
TEST_PEER_ID: bytes = bytes(range(100, 120))
FAKE_PEER_ID: bytes = b"-qB4410-" + b"0" * 12


class FakePeer:
    """A scripted peer that listens on loopback and speaks the wire protocol.

    Args:
        info_hash: Info hash to advertise in the handshake reply.
        peer_id: Peer id to advertise.
        reply_to_handshake: When False the peer stays silent, which is how a
            handshake timeout is provoked.
        handshake_delay: Seconds to wait before replying.
        reserved: Reserved bytes to advertise.
    """

    def __init__(
        self,
        *,
        info_hash: bytes = TEST_INFO_HASH,
        peer_id: bytes = FAKE_PEER_ID,
        reply_to_handshake: bool = True,
        handshake_delay: float = 0.0,
        reserved: bytes = bytes(8),
    ) -> None:
        self.host = "127.0.0.1"
        self.port = 0
        self.received_handshake: Handshake | None = None
        self.received: bytes = b""
        self.closed = False

        self._handshake = Handshake(info_hash=info_hash, peer_id=peer_id, reserved=reserved)
        self._reply_to_handshake = reply_to_handshake
        self._handshake_delay = handshake_delay
        self._actions: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        self._server: asyncio.Server | None = None
        self._connections: list[asyncio.StreamWriter] = []

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> tuple[str, int]:
        """Start listening and return ``(host, port)``."""
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or ()
        self.port = sockets[0].getsockname()[1]
        return self.host, self.port

    async def stop(self) -> None:
        """Stop listening and close every accepted connection."""
        self.closed = True
        for writer in self._connections:
            writer.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await self._server.wait_closed()
            self._server = None

    @property
    def address(self) -> tuple[str, int]:
        """The listening address."""
        return self.host, self.port

    # -------------------------------------------------------------- scripting

    async def write(self, data: bytes) -> None:
        """Queue raw bytes to send to the client."""
        await self._actions.put(("write", data))

    async def send(self, message: Message) -> None:
        """Queue one encoded message."""
        await self.write(encode(message))

    async def send_all(self, messages: Sequence[Message]) -> None:
        """Queue several messages as a single TCP write (one segment)."""
        await self.write(b"".join(encode(message) for message in messages))

    async def sleep(self, seconds: float) -> None:
        """Queue a pause, so the client sees the previous bytes arrive first."""
        await self._actions.put(("sleep", seconds))

    async def close_connection(self) -> None:
        """Queue a disconnect."""
        await self._actions.put(("close", None))

    # -------------------------------------------------------------- internals

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connections.append(writer)
        try:
            data = await reader.readexactly(HANDSHAKE_LENGTH)
            self.received = bytes(data)
            self.received_handshake = Handshake.decode(data)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            writer.close()
            return

        # Everything the client sends after the handshake is recorded too.
        collector = asyncio.create_task(self._collect(reader))

        if self._handshake_delay:
            await asyncio.sleep(self._handshake_delay)
        if self._reply_to_handshake:
            writer.write(self._handshake.encode())
            await writer.drain()

        try:
            while True:
                action, payload = await self._actions.get()
                if action == "write":
                    writer.write(payload)  # type: ignore[arg-type]
                    await writer.drain()
                elif action == "sleep":
                    await asyncio.sleep(payload)  # type: ignore[arg-type]
                elif action == "close":
                    break
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            collector.cancel()

    async def _collect(self, reader: asyncio.StreamReader) -> None:
        """Record post-handshake bytes from the client until it disconnects."""
        try:
            while chunk := await reader.read(4096):
                self.received += chunk
        except (ConnectionError, asyncio.CancelledError):
            pass


async def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    """Wait for a condition set by the other end of a socket.

    Queuing an action on a :class:`FakePeer` returns immediately, so tests need
    a real synchronisation point before asserting on what the peer observed.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met before the deadline")


class FailingReader(asyncio.StreamReader):
    """A reader that raises instead of reading, to exercise error mapping."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def readexactly(self, n: int) -> bytes:  # type: ignore[override]
        raise self._error


class FailingWriter:
    """A writer whose drain always fails, to exercise error mapping."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.written: list[bytes] = []

    def is_closing(self) -> bool:
        return False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        raise self._error

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


@pytest.fixture
def info_hash() -> bytes:
    """A fixed 20-byte info hash shared by the peer tests."""
    return TEST_INFO_HASH


@pytest.fixture
def peer_id() -> bytes:
    """Our peer id in tests."""
    return TEST_PEER_ID


@pytest.fixture
async def fake_peer() -> FakePeer:
    """A running scripted peer, stopped at the end of the test."""
    peer = FakePeer()
    await peer.start()
    try:
        yield peer
    finally:
        await peer.stop()
