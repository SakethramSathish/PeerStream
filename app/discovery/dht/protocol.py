"""Asyncio UDP transport for KRPC queries, retries and timeouts.

A DHT node is a UDP socket that asks strangers questions. This module owns that
socket and the bookkeeping that makes asking safe:

**Every query gets a transaction id, and every reply is checked against one.**
The outstanding-query table is keyed by id; a datagram whose id is not in it is
dropped without being parsed further. This is not tidiness: a DHT is a network
of hosts we have never met, and an unmatched reply is either late, duplicated,
or forged — all three look identical, and all three must be ignored.

**Silence is the normal failure.** UDP tells us nothing when a node is gone; an
ICMP "port unreachable" is a hint, not an answer. So each query is retried with
backoff and then declared timed out, and the caller decides what a silent node
means (usually: try the next closest one).

**Queries we receive are answered from a callback.** The routing decisions live
in :class:`~app.discovery.dht.node.DhtNode`; this class only knows how to get a
dictionary onto the wire and back. A handler that wants to refuse sends an
error by raising :class:`~app.discovery.dht.errors.DhtRemoteError`, which is
encoded as a KRPC error with the code and message it carries.

The socket is created on :meth:`KrpcTransport.start` rather than in the
constructor, because it binds to the running loop and a node is often built
before one exists.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from app.bencode.decoder import BencodeValue
from app.discovery.dht.errors import (
    DhtError,
    DhtRemoteError,
    DhtTimeoutError,
)
from app.discovery.dht.krpc import (
    MAX_PACKET_SIZE,
    KrpcFailure,
    KrpcMessage,
    KrpcQuery,
    KrpcResponse,
    decode,
    encode_error,
    encode_query,
    encode_response,
    new_transaction_id,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: Final[float] = 2.0  # per attempt; a DHT reply should be quick
DEFAULT_RETRIES: Final[int] = 1  # two attempts in all
RETRY_DELAY: Final[float] = 0.4
#: Bounded, so a node that answers slowly cannot accumulate futures forever.
MAX_OUTSTANDING: Final[int] = 256

QueryHandler = Callable[[KrpcQuery, tuple[str, int]], Awaitable[Mapping[bytes, BencodeValue]]]
"""What a node does with a query: returns the response body, or raises
:class:`~app.discovery.dht.errors.DhtRemoteError` to refuse."""


class _KrpcEndpoint(asyncio.DatagramProtocol):
    """The socket: match replies to queries, and hand queries to a handler."""

    def __init__(self, handler: QueryHandler | None = None) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self._handler = handler
        self._outstanding: dict[bytes, asyncio.Future[KrpcMessage]] = {}

    # ------------------------------------------------------------------ sending

    def ask(self, transaction_id: bytes) -> asyncio.Future[KrpcMessage]:
        """Register interest in the reply to this transaction id."""
        future: asyncio.Future[KrpcMessage] = asyncio.get_running_loop().create_future()
        self._outstanding[transaction_id] = future
        future.add_done_callback(lambda _done: self._outstanding.pop(transaction_id, None))
        return future

    @property
    def outstanding(self) -> int:
        return len(self._outstanding)

    # ---------------------------------------------------------------- receiving

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None
        for future in list(self._outstanding.values()):
            if not future.done():
                future.set_exception(DhtError(f"DHT socket closed: {exc or 'closed'}"))

    def error_received(self, exc: Exception) -> None:
        # An ICMP error is a hint, not an answer: some hosts send one and reply
        # anyway. The timeout is the authority, so this is only logged.
        logger.debug("DHT socket error: %s", exc)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            message = decode(data)
        except DhtError as exc:
            logger.debug("ignoring an unreadable packet from %s: %s", addr, exc)
            return

        if isinstance(message, KrpcQuery):
            self._received_query(message, addr)
            return

        future = self._outstanding.get(message.transaction_id)
        if future is None or future.done():
            logger.debug(
                "ignoring a %s for transaction %s: nobody asked",
                type(message).__name__,
                message.transaction_id.hex(),
            )
            return
        future.set_result(message)

    def _received_query(self, query: KrpcQuery, addr: tuple[str, int]) -> None:
        if self._handler is None or self.transport is None:
            return
        task = asyncio.get_running_loop().create_task(self._answer(query, addr))
        task.add_done_callback(_consume)

    async def _answer(self, query: KrpcQuery, addr: tuple[str, int]) -> None:
        assert self.transport is not None
        assert self._handler is not None
        try:
            body = await self._handler(query, addr)
        except DhtRemoteError as refusal:
            self.transport.sendto(
                encode_error(query.transaction_id, refusal.code, str(refusal)), addr
            )
        except Exception as exc:  # noqa: BLE001 - a query must never kill the node
            logger.warning("answering a DHT %s from %s failed: %s", query.method, addr, exc)
            self.transport.sendto(
                encode_error(query.transaction_id, 201, "internal error"), addr
            )
        else:
            self.transport.sendto(encode_response(query.transaction_id, body), addr)


class KrpcTransport:
    """One UDP socket that speaks KRPC.

    Args:
        handler: Called for every query we receive, with the query and the
            address it came from. Return the response body; raise
            :class:`~app.discovery.dht.errors.DhtRemoteError` to refuse.
        host: Bind address.
        port: Bind port; ``0`` picks a free one (what tests use).
        timeout: How long to wait for one reply.
        retries: Extra attempts after the first.

    Raises:
        DhtError: The socket could not be bound.
    """

    def __init__(
        self,
        *,
        handler: QueryHandler | None = None,
        host: str = "0.0.0.0",  # a DHT node listens for anyone
        port: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._retries = max(0, retries)
        self._endpoint = _KrpcEndpoint(handler)
        self._error: Exception | None = None

    # ------------------------------------------------------------------ reading

    @property
    def port(self) -> int:
        """The bound UDP port. Only meaningful after :meth:`start`."""
        return self._port

    @property
    def outstanding(self) -> int:
        """How many queries are waiting for an answer."""
        return self._endpoint.outstanding

    @property
    def bound(self) -> bool:
        return self._endpoint.transport is not None

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> int:
        """Bind the socket. Returns the port, since ``0`` means "any".

        Idempotent: a node that is already bound keeps its socket, because
        binding twice would fail with "address already in use" — and "start"
        is a thing a caller may reasonably do twice.
        """
        if self.bound:
            return self._port
        loop = asyncio.get_running_loop()
        _transport, _protocol = await loop.create_datagram_endpoint(
            lambda: self._endpoint, local_addr=(self._host, self._port)
        )
        sockname = self._endpoint.transport.get_extra_info("sockname") if self._endpoint.transport else None
        if isinstance(sockname, tuple):
            self._port = int(sockname[1])
        logger.debug("DHT socket bound to %s:%d", self._host, self._port)
        return self._port

    async def aclose(self) -> None:
        """Close the socket. Safe to call twice."""
        if self._endpoint.transport is not None:
            self._endpoint.transport.close()
            self._endpoint.transport = None

    async def __aenter__(self) -> KrpcTransport:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ querying

    async def query(
        self,
        address: tuple[str, int],
        method: str,
        arguments: Mapping[bytes, BencodeValue],
        *,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> KrpcResponse:
        """Ask one node one question and wait for the answer.

        Args:
            address: The node's ``(host, port)``.
            method: ``ping``, ``find_node``, ``get_peers`` or ``announce_peer``.
            arguments: The ``a`` dictionary, without our node id — that is added
                here, so no caller can forget it.
            timeout: Override for this query.
            retries: Override for this query.

        Returns:
            The decoded response.

        Raises:
            DhtTimeoutError: No reply within the timeout, across all attempts.
            DhtRemoteError: The node answered with a KRPC error.
            DhtError: The socket is not bound, or the packet is too large.
        """
        transport = self._endpoint.transport
        if transport is None:
            raise DhtError("the DHT socket is not bound; call start() first")
        deadline = self._timeout if timeout is None else timeout
        attempts = (self._retries if retries is None else max(0, retries)) + 1
        last: Exception | None = None

        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(RETRY_DELAY * attempt)
            if self._endpoint.outstanding >= MAX_OUTSTANDING:
                raise DhtError(
                    f"{self._endpoint.outstanding} DHT queries are already outstanding"
                )
            transaction_id = new_transaction_id()
            payload = encode_query(transaction_id, method, dict(arguments))
            if len(payload) > MAX_PACKET_SIZE:
                raise DhtError(
                    f"a {method} query of {len(payload)} bytes will not fit in one packet"
                )
            future = self._endpoint.ask(transaction_id)
            transport.sendto(payload, address)
            try:
                message = await asyncio.wait_for(future, timeout=deadline)
            except TimeoutError:
                last = DhtTimeoutError(
                    f"{address[0]}:{address[1]} did not answer a {method} in {deadline}s"
                )
                continue
            if isinstance(message, KrpcFailure):
                raise DhtRemoteError(message.message, code=message.code, address=address)
            if isinstance(message, KrpcResponse):
                return message
            # A query in reply to our query is not a reply: ignore it and let
            # the timeout have the last word.
            last = DhtTimeoutError(f"{address[0]}:{address[1]} answered a query with a query")
            continue
        assert last is not None
        raise last

    def send(self, address: tuple[str, int], payload: bytes) -> None:
        """Send a packet we encoded ourselves. Used by tests and replies."""
        transport = self._endpoint.transport
        if transport is None:
            raise DhtError("the DHT socket is not bound; call start() first")
        transport.sendto(payload, address)


def _consume(task: asyncio.Task[None]) -> None:
    """Swallow the exception of a fire-and-forget task, having logged it."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("answering a DHT query failed: %s", error)
