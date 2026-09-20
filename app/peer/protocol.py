"""Framed message I/O over a TCP stream (TRD §24).

This is the layer that turns a byte stream into messages. TCP guarantees bytes
in order, nothing else: a read may return half a message, or three messages, or
one byte. Three failure modes have to be handled explicitly:

* **Partial reads.** ``readexactly`` handles this by waiting for the rest, so a
  frame split across ten TCP segments still arrives whole.
* **Multiple messages in one read.** Because we read *exactly* the number of
  bytes the frame declares, whatever remains stays in the stream buffer for the
  next call. No manual reassembly buffer is needed — and hand-rolled buffers
  are where most clients lose sync.
* **Malformed lengths.** The length prefix allows 2^32-1 bytes. Trusting it
  would let a peer make us allocate 4 GiB, so anything above
  ``max_message_length`` is refused *before* the payload is read, and the
  connection is then the caller's to close.

``asyncio.StreamReader`` already buffers, so this class holds no read buffer of
its own; it only counts bytes and enforces deadlines.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

from app.core.constants import (
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_REQUEST_TIMEOUT,
    HANDSHAKE_LENGTH,
    MAX_MESSAGE_LENGTH,
    MESSAGE_HEADER_LENGTH,
)
from app.peer.errors import PeerConnectionError, PeerDisconnected, PeerTimeoutError, ProtocolError
from app.peer.handshake import Handshake
from app.peer.messages import KeepAlive, Message, decode, encode

logger = logging.getLogger(__name__)


class PeerStream:
    """A framed message channel to one peer.

    Args:
        reader: Stream reader from :func:`asyncio.open_connection` or a server.
        writer: Matching writer.
        max_message_length: Largest frame we will accept, counting the id byte.
        timeout: Default deadline for reads, in seconds.
        label: Name used in error messages and logs, usually ``host:port``.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_message_length: int = MAX_MESSAGE_LENGTH,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        label: str = "peer",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_message_length = max_message_length
        self._timeout = timeout
        self._label = label
        self._bytes_sent = 0
        self._bytes_received = 0
        self._last_activity = time.monotonic()
        self.handshake_latency_ms: float | None = None

    # ------------------------------------------------------------ connection

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        timeout: float = DEFAULT_CONNECTION_TIMEOUT,
        read_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        max_message_length: int = MAX_MESSAGE_LENGTH,
    ) -> PeerStream:
        """Open a TCP connection to a peer.

        Args:
            host: Peer host.
            port: Peer port.
            timeout: Deadline for the TCP connect itself.
            read_timeout: Default deadline for later reads. A long-lived
                connection sets this to its idle timeout, so a peer that goes
                quiet is noticed instead of hanging forever.
            max_message_length: Largest frame accepted from this peer.

        Raises:
            PeerTimeoutError: The TCP connect did not complete in time.
            PeerConnectionError: Refused, unreachable, or a DNS failure.
        """
        label = f"{host}:{port}"
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_connection(host, port)
        except TimeoutError as exc:
            raise PeerTimeoutError(f"{label}: connect timed out after {timeout}s") from exc
        except OSError as exc:
            raise PeerConnectionError(f"{label}: cannot connect: {exc}") from exc
        return cls(
            reader,
            writer,
            max_message_length=max_message_length,
            timeout=read_timeout,
            label=label,
        )

    # -------------------------------------------------------------- handshake

    async def perform_handshake(
        self, handshake: Handshake, *, timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    ) -> Handshake:
        """Send our handshake and read the peer's, validating the info hash.

        Args:
            handshake: Our handshake; its info hash is what the peer must echo.
            timeout: Deadline for the peer's reply.

        Returns:
            The peer's handshake, already checked against our info hash.

        Raises:
            PeerTimeoutError: The peer never sent a complete handshake.
            PeerDisconnected: The peer hung up mid-handshake.
            HandshakeError: The handshake was malformed or for another torrent.
        """
        started = time.monotonic()
        await self._write(handshake.encode())
        data = await self._read_exactly(HANDSHAKE_LENGTH, timeout)
        self._bytes_received += HANDSHAKE_LENGTH
        self.handshake_latency_ms = (time.monotonic() - started) * 1000
        peer_handshake = Handshake.decode(data, expected_info_hash=handshake.info_hash)
        logger.debug("%s: handshake from %s", self._label, peer_handshake)
        return peer_handshake

    async def accept_handshake(
        self, handshake: Handshake, *, timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    ) -> Handshake:
        """Read the peer's handshake, then answer with ours.

        The incoming side of :meth:`perform_handshake`: a peer that dialled us
        has already sent its handshake, so reading first and replying second is
        what keeps both sides from waiting on each other.

        Args:
            handshake: Our handshake; its info hash is what the peer must have
                asked for, so a peer from another swarm is rejected here.
            timeout: Deadline for the peer's handshake.

        Returns:
            The peer's handshake, already checked against our info hash.

        Raises:
            PeerTimeoutError: The peer never sent a complete handshake.
            PeerDisconnected: The peer hung up mid-handshake.
            HandshakeError: The handshake was malformed or for another torrent.
        """
        data = await self._read_exactly(HANDSHAKE_LENGTH, timeout)
        self._bytes_received += HANDSHAKE_LENGTH
        peer_handshake = Handshake.decode(data, expected_info_hash=handshake.info_hash)
        await self._write(handshake.encode())
        logger.debug("%s: handshake from %s", self._label, peer_handshake)
        return peer_handshake

    # --------------------------------------------------------------- messages

    async def read_message(self, *, timeout: float | None = None) -> Message:
        """Read exactly one message.

        Args:
            timeout: Deadline in seconds; the stream default when omitted.

        Returns:
            The decoded message. A zero-length frame yields :class:`KeepAlive`.

        Raises:
            PeerTimeoutError: Nothing arrived within the deadline.
            PeerDisconnected: The stream ended mid-frame.
            ProtocolError: The declared length exceeds our limit.
            MessageError: The frame body was not a valid message.
        """
        deadline = self._timeout if timeout is None else timeout

        raw_length = await self._read_exactly(MESSAGE_HEADER_LENGTH, deadline)
        self._bytes_received += MESSAGE_HEADER_LENGTH
        length = int.from_bytes(raw_length, "big")

        if length == 0:
            self._last_activity = time.monotonic()
            return KeepAlive()
        if length > self._max_message_length:
            raise ProtocolError(
                f"{self._label}: declared message length {length} exceeds the "
                f"{self._max_message_length} byte limit"
            )

        body = await self._read_exactly(length, deadline)
        self._bytes_received += length
        self._last_activity = time.monotonic()
        return decode(body)

    async def send(self, message: Message) -> None:
        """Encode and write one message, then wait for the buffer to drain.

        Raises:
            PeerDisconnected: The connection is closing, or the write failed.
        """
        await self._write(encode(message))

    async def send_keep_alive(self) -> None:
        """Send a keep-alive, holding the connection open across idle periods."""
        await self.send(KeepAlive())

    async def messages(self) -> AsyncIterator[Message]:
        """Yield messages until the peer disconnects or the stream is closed.

        A clean disconnect ends the iteration rather than raising: from the
        read loop's point of view, a peer hanging up is a loop condition, not
        an exception.
        """
        while not self.closed:
            try:
                yield await self.read_message()
            except PeerDisconnected:
                return

    # ------------------------------------------------------------- inspection

    @property
    def bytes_sent(self) -> int:
        """Bytes written, including the handshake."""
        return self._bytes_sent

    @property
    def bytes_received(self) -> int:
        """Bytes read, including the handshake."""
        return self._bytes_received

    @property
    def label(self) -> str:
        """The peer's ``host:port`` label used in messages."""
        return self._label

    @property
    def closed(self) -> bool:
        """Whether the underlying writer is closing or closed."""
        return self._writer.is_closing()

    @property
    def idle_for(self) -> float:
        """Seconds since the last successful read or write."""
        return time.monotonic() - self._last_activity

    # -------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        """Close the connection. Safe to call more than once."""
        if self._writer.is_closing():
            return
        self._writer.close()
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await self._writer.wait_closed()

    async def __aenter__(self) -> PeerStream:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __str__(self) -> str:
        return f"<PeerStream {self._label}>"

    # -------------------------------------------------------------- internals

    async def _write(self, data: bytes) -> None:
        """Write raw bytes and drain, translating socket failures."""
        if self._writer.is_closing():
            raise PeerDisconnected(f"{self._label}: connection is closing")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except TimeoutError as exc:
            raise PeerTimeoutError(f"{self._label}: write timed out") from exc
        except OSError as exc:
            raise PeerDisconnected(f"{self._label}: write failed: {exc}") from exc
        self._bytes_sent += len(data)
        self._last_activity = time.monotonic()

    async def _read_exactly(self, count: int, timeout: float | None) -> bytes:
        """Read exactly ``count`` bytes, or fail with a typed error."""
        try:
            if timeout is None or timeout <= 0:
                return await self._reader.readexactly(count)
            async with asyncio.timeout(timeout):
                return await self._reader.readexactly(count)
        except TimeoutError as exc:
            raise PeerTimeoutError(f"{self._label}: no data for {timeout}s") from exc
        except asyncio.IncompleteReadError as exc:
            raise PeerDisconnected(
                f"{self._label}: closed after {len(exc.partial)} of {count} bytes"
            ) from exc
        except OSError as exc:
            raise PeerDisconnected(f"{self._label}: connection failed: {exc}") from exc
