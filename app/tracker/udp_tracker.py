"""UDP tracker client (BEP 15).

A UDP announce is a *conversation*, not a request: the client first exchanges a
16-byte ``connect`` for a ``connection_id``, then spends that id on an
``announce`` or a ``scrape``. The id expires — a minute is the usual lifetime —
and a tracker that no longer recognises it answers with an error or, more often,
with nothing at all.

Three things make this more than HTTP over a different socket:

**Packets are unnumbered and unauthenticated.** Every request carries a random
``transaction_id`` and every reply is checked against it, because a datagram
that arrives late, twice, or from the wrong host is indistinguishable from a
real one otherwise. Replies that do not match are dropped, not parsed — BEP 15
says so, and it is the only defence against acting on another client's response.

**There is no connection to detect failure on.** UDP tells you nothing when the
tracker is gone; an ICMP "port unreachable" is a hint, not an answer. So every
request is retried with exponential backoff and then declared timed out, and
the retry is where the connection id is refreshed, because a silent tracker is
very often a stale id.

**The wire format is fixed-width big-endian.** Everything here is built and read
with :mod:`struct` by pure functions at the top of this module, so the protocol
can be tested against recorded byte strings without a socket at all — which is
how the tests below work, alongside a local UDP tracker double that speaks the
real protocol on a loopback port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import socket
import struct
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from typing import Final
from urllib.parse import urlsplit

from app.core.constants import INFO_HASH_SIZE, PEER_ID_SIZE
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.core.peer_id import user_agent
from app.tracker.base import (
    DEFAULT_ANNOUNCE_INTERVAL,
    AnnounceRequest,
    AnnounceResponse,
    PeerAddress,
    ScrapeResponse,
    Tracker,
    TrackerEvent,
)
from app.tracker.errors import (
    TrackerConnectionError,
    TrackerProtocolError,
    TrackerTimeoutError,
    UnsupportedTrackerError,
)
from app.tracker.http_tracker import parse_compact_peers_ipv4

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- the wire

#: Magic constant that opens every connect request.
PROTOCOL_ID: Final[int] = 0x41727101980

ACTION_CONNECT: Final[int] = 0
ACTION_ANNOUNCE: Final[int] = 1
ACTION_SCRAPE: Final[int] = 2
ACTION_ERROR: Final[int] = 3

CONNECT_REQUEST_SIZE: Final[int] = 16
CONNECT_RESPONSE_SIZE: Final[int] = 16
ANNOUNCE_REQUEST_SIZE: Final[int] = 98
ANNOUNCE_RESPONSE_HEADER_SIZE: Final[int] = 20
SCRAPE_REQUEST_HEADER_SIZE: Final[int] = 16
SCRAPE_RESPONSE_HEADER_SIZE: Final[int] = 8
SCRAPE_ENTRY_SIZE: Final[int] = 12
ERROR_RESPONSE_HEADER_SIZE: Final[int] = 8
HEADER_SIZE: Final[int] = 8  # action + transaction id, on every packet

# The UDP ``event`` field is an integer, and its numbering is not the HTTP
# tracker's ordering: completed is 1 here, started is 2.
EVENT_NONE: Final[int] = 0
EVENT_COMPLETED: Final[int] = 1
EVENT_STARTED: Final[int] = 2
EVENT_STOPPED: Final[int] = 3

EVENT_CODES: Final[Mapping[TrackerEvent | None, int]] = {
    None: EVENT_NONE,
    TrackerEvent.COMPLETED: EVENT_COMPLETED,
    TrackerEvent.STARTED: EVENT_STARTED,
    TrackerEvent.STOPPED: EVENT_STOPPED,
}

# ------------------------------------------------------------------- behaviour

DEFAULT_TIMEOUT: Final[float] = 8.0  # per attempt, matching TrackerConfig.udp_timeout
DEFAULT_MAX_RETRIES: Final[int] = 2  # three attempts in all
INITIAL_RETRY_DELAY: Final[float] = 0.5
MAX_RETRY_DELAY: Final[float] = 8.0
#: A connection id is good for a minute by convention; we retire ours early.
CONNECTION_ID_LIFETIME: Final[float] = 60.0
DEFAULT_MAX_PEERS: Final[int] = 300
#: A UDP scrape packet has room for 74 info hashes (8 + 74*20 = 1488, just
#: under one Ethernet frame). Asking for more is asking to be ignored.
MAX_SCRAPE_HASHES: Final[int] = 74
DEFAULT_UDP_PORT: Final[int] = 6969


def endpoint_of(url: str) -> tuple[str, int]:
    """The ``(host, port)`` a UDP tracker URL names.

    A UDP URL carries no path worth parsing — ``udp://host:6969/announce`` and
    ``udp://host:6969`` are the same tracker — but the port is mandatory in
    practice, so a missing one falls back to 6969 rather than failing to open
    the torrent.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise UnsupportedTrackerError(f"UDP tracker URL {url!r} names no host")
    return host, parts.port or DEFAULT_UDP_PORT


class UdpTracker(Tracker):
    """UDP tracker client.

    Args:
        url: Announce URL, ``udp://host:port`` (a path, if present, is ignored).
        timeout: How long to wait for one reply, per attempt.
        max_peers: Cap on peers accepted from a response.
        max_retries: Extra attempts after the first, with exponential backoff.
        connection_lifetime: How long a connection id is reused.
        event_bus: Optional bus for tracker events.
        agent: Recorded on the client; unused on the wire, since BEP 15 has no
            user-agent field, and kept so the two clients can be constructed
            the same way.

    Raises:
        UnsupportedTrackerError: The URL is not a ``udp://`` URL.
    """

    SUPPORTED_SCHEMES = ("udp",)

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_peers: int = DEFAULT_MAX_PEERS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        connection_lifetime: float = CONNECTION_ID_LIFETIME,
        event_bus: EventBus | None = None,
        agent: str | None = None,
    ) -> None:
        super().__init__(url)
        self._host, self._port = endpoint_of(url)
        self._timeout = timeout
        self._max_peers = max_peers
        self._max_retries = max_retries
        self._connection_lifetime = connection_lifetime
        self._event_bus = event_bus
        self._agent = agent or user_agent()

        self._endpoint: _UdpEndpoint | None = None
        self._lock: asyncio.Lock | None = None
        self._connection_id: int | None = None
        self._connection_id_at: float = 0.0
        self._transaction_seed: int = random.getrandbits(32)
        self._key: int = random.getrandbits(32)

    # ------------------------------------------------------------------ reading

    @property
    def port(self) -> int:
        """The UDP port we send to."""
        return self._port

    @property
    def connection_id(self) -> int | None:
        """The cached connection id, or ``None`` when we have no live one."""
        return self._connection_id

    @property
    def transaction_seed(self) -> int:
        """Where transaction ids start. Exposed so tests can predict them."""
        return self._transaction_seed

    # ---------------------------------------------------------------------- API

    async def announce(self, request: AnnounceRequest) -> AnnounceResponse:
        """Send an announce and return the parsed response.

        Raises:
            TrackerTimeoutError: The tracker never answered, across all retries.
            TrackerConnectionError: The socket failed underneath us.
            TrackerProtocolError: The answer was malformed, or was an error.
        """
        self._publish_request(request)
        started = time.perf_counter()
        key = zlib.crc32(request.key.encode("utf-8")) if request.key else self._key
        try:
            transaction_id, raw = await self._transact(
                ACTION_ANNOUNCE,
                lambda connection_id, tid: build_announce_request(
                    connection_id, tid, request, key=key
                ),
            )
        except (TrackerTimeoutError, TrackerConnectionError) as exc:
            self._publish_failure(str(exc))
            raise
        try:
            response = parse_announce_response(
                raw,
                transaction_id,
                max_peers=self._max_peers,
            )
        except TrackerProtocolError as exc:
            self._publish_failure(str(exc))
            raise
        self._publish_response(response, latency_ms=(time.perf_counter() - started) * 1000)
        return response

    async def scrape(self, info_hashes: Sequence[bytes]) -> Mapping[bytes, ScrapeResponse]:
        """Scrape swarm counters, in batches small enough for one packet.

        Unlike the HTTP client this one answers with *only* the hashes the
        tracker knows about, so a missing key means "not reported" rather than
        "empty swarm".
        """
        hashes = [bytes(info_hash) for info_hash in info_hashes]
        for info_hash in hashes:
            if len(info_hash) != INFO_HASH_SIZE:
                raise TrackerProtocolError(
                    f"info_hash must be {INFO_HASH_SIZE} bytes, got {len(info_hash)}"
                )
        if not hashes:
            return {}

        results: dict[bytes, ScrapeResponse] = {}
        for start in range(0, len(hashes), MAX_SCRAPE_HASHES):
            batch = hashes[start : start + MAX_SCRAPE_HASHES]

            def payload_for(connection_id: int, tid: int, batch: Sequence[bytes] = batch) -> bytes:
                return build_scrape_request(connection_id, tid, batch)

            transaction_id, raw = await self._transact(ACTION_SCRAPE, payload_for)
            results.update(parse_scrape_response(raw, transaction_id, batch))
        return results

    async def aclose(self) -> None:
        """Close the socket and forget the connection id. Safe to call twice."""
        endpoint, self._endpoint = self._endpoint, None
        self._connection_id = None
        self._connection_id_at = 0.0
        if endpoint is not None:
            endpoint.close()

    # --------------------------------------------------------------- transport

    def _ensure_lock(self) -> asyncio.Lock:
        """Lazily create the lock: it binds to the running loop, so the
        constructor — which is often called before one exists — cannot."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_endpoint(self) -> _UdpEndpoint:
        """Open the socket on first use, and reopen it if it died."""
        endpoint = self._endpoint
        if endpoint is not None and endpoint.open:
            return endpoint
        loop = asyncio.get_running_loop()
        try:
            _transport, endpoint = await loop.create_datagram_endpoint(
                _UdpEndpoint, remote_addr=(self._host, self._port)
            )
        except OSError as exc:
            raise TrackerConnectionError(
                f"cannot reach UDP tracker {self.host}:{self._port}: {exc}"
            ) from exc
        self._endpoint = endpoint
        return endpoint

    def _next_transaction_id(self) -> int:
        """A fresh transaction id, masked to 32 bits.

        Sequential rather than random: a tracker that misbehaves is easier to
        diagnose from a log of ids that count upwards.
        """
        self._transaction_seed = (self._transaction_seed + 1) & 0xFFFFFFFF
        return self._transaction_seed

    async def _transact(self, action: int, build: Callable[[int, int], bytes]) -> tuple[int, bytes]:
        """Run one request/response exchange, retrying with backoff.

        Returns the transaction id alongside the packet, because the parser has
        to check the reply against the id it was sent with.

        Args:
            action: The action we expect back, used to filter replies.
            build: Called with ``(connection_id, transaction_id)`` on every
                attempt, since the id is refreshed between retries.

        Raises:
            TrackerTimeoutError: Every attempt went unanswered.
            TrackerConnectionError: The socket closed underneath us.
            TrackerProtocolError: The tracker refused, and refreshing the
                connection id did not help.
        """
        async with self._ensure_lock():
            last: Exception | None = None
            for attempt in range(self._max_retries + 1):
                if attempt and last is not None:
                    delay = min(INITIAL_RETRY_DELAY * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
                    logger.debug(
                        "retrying UDP tracker %s:%d in %.1fs (%s)",
                        self._host,
                        self._port,
                        delay,
                        last,
                    )
                    await asyncio.sleep(delay)
                try:
                    endpoint = await self._ensure_endpoint()
                    reused = self._connection_id is not None
                    connection_id = await self._ensure_connection_id(endpoint)
                    transaction_id = self._next_transaction_id()
                    raw = await self._round_trip(
                        endpoint,
                        build(connection_id, transaction_id),
                        action=action,
                        transaction_id=transaction_id,
                    )
                except TrackerTimeoutError as exc:
                    # A silent tracker is usually a stale connection id, so the
                    # retry goes out with a new one.
                    self._forget_connection()
                    last = exc
                except TrackerConnectionError as exc:
                    self._endpoint = None
                    self._forget_connection()
                    last = exc
                else:
                    if _is_error(raw) and reused and attempt == 0:
                        # It refused an id we had already used once. Take that
                        # as a stale connection and ask for a new one, rather
                        # than as a refusal to serve the torrent at all.
                        self._forget_connection()
                        last = TrackerProtocolError(error_message(raw))
                        continue
                    return transaction_id, raw
            assert last is not None
            raise last

    async def _round_trip(
        self,
        endpoint: _UdpEndpoint,
        payload: bytes,
        *,
        action: int,
        transaction_id: int,
    ) -> bytes:
        """Send one packet and wait for the matching reply."""
        assert endpoint.transport is not None
        waiting = endpoint.wait_for(action, transaction_id)
        endpoint.transport.sendto(payload)
        try:
            return await asyncio.wait_for(waiting, timeout=self._timeout)
        except TimeoutError:
            raise TrackerTimeoutError(
                f"{self.host}:{self._port} did not answer within {self._timeout}s"
            ) from None

    async def _ensure_connection_id(self, endpoint: _UdpEndpoint) -> int:
        """The cached connection id, or a fresh one from a ``connect``."""
        if self._connection_id is not None:
            age = time.monotonic() - self._connection_id_at
            if age < self._connection_lifetime:
                return self._connection_id
            logger.debug(
                "UDP connection id for %s:%d is %.0fs old; reconnecting",
                self._host,
                self._port,
                age,
            )
        return await self._connect(endpoint)

    async def _connect(self, endpoint: _UdpEndpoint) -> int:
        """Exchange a ``connect`` for a connection id."""
        transaction_id = self._next_transaction_id()
        raw = await self._round_trip(
            endpoint,
            build_connect_request(transaction_id),
            action=ACTION_CONNECT,
            transaction_id=transaction_id,
        )
        connection_id = parse_connect_response(raw, transaction_id)
        self._connection_id = connection_id
        self._connection_id_at = time.monotonic()
        return connection_id

    def _forget_connection(self) -> None:
        self._connection_id = None
        self._connection_id_at = 0.0

    # ------------------------------------------------------------------ events

    def _publish_request(self, request: AnnounceRequest) -> None:
        if self._event_bus is None:
            return
        self._event_bus.emit(
            make_event(
                EventType.TRACKER_REQUEST,
                message=f"announce to {self.host}:{self._port} ({request.event or 'periodic'})",
                torrent_id=request.info_hash.hex(),
                data={
                    "url": self.url,
                    "transport": "udp",
                    "event": request.event.value if request.event else None,
                },
            )
        )

    def _publish_response(self, response: AnnounceResponse, *, latency_ms: float) -> None:
        if self._event_bus is None:
            return
        self._event_bus.emit(
            make_event(
                EventType.TRACKER_RESPONSE,
                message=(
                    f"{len(response.peers)} peers, {response.seeders} seeders ({latency_ms:.0f} ms)"
                ),
                data={
                    "url": self.url,
                    "transport": "udp",
                    "peers": len(response.peers),
                    "seeders": response.seeders,
                    "leechers": response.leechers,
                    "interval": response.interval,
                    "latency_ms": round(latency_ms, 2),
                },
            )
        )

    def _publish_failure(self, reason: str) -> None:
        if self._event_bus is None:
            return
        self._event_bus.emit(
            make_event(
                EventType.TRACKER_FAILED,
                message=f"announce failed: {reason}",
                level=logging.WARNING,
                data={"url": self.url, "host": self.host, "reason": reason},
            )
        )


class _UdpEndpoint(asyncio.DatagramProtocol):
    """One datagram socket, waiting for one reply at a time.

    Replies are matched on the transaction id *here*, at the edge, rather than
    by the caller: a datagram that is not the answer to the question we just
    asked is not evidence of anything, and dropping it at the door is the only
    way to be sure a late or foreign packet is never parsed.
    """

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self._waiting: asyncio.Future[bytes] | None = None
        self._expected: tuple[int, int] | None = None

    @property
    def open(self) -> bool:
        """Whether the socket can still carry a datagram."""
        return self.transport is not None and not self.transport.is_closing()

    def wait_for(self, action: int, transaction_id: int) -> asyncio.Future[bytes]:
        """Arm the endpoint to accept one reply."""
        self._expected = (action, transaction_id)
        self._waiting = asyncio.get_running_loop().create_future()
        return self._waiting

    def close(self) -> None:
        """Close the socket, whoever is asking.

        A tracker can outlive the loop that opened its socket — a CLI that runs
        ``asyncio.run`` twice, for instance — and a cleanup path that raised
        would turn a successful announce into a crash on the way out. The file
        descriptor is closed directly, which is the part that actually matters.
        """
        transport, self.transport = self.transport, None
        self._fail(TrackerConnectionError("UDP socket closed"))
        if transport is None:
            return
        with contextlib.suppress(RuntimeError):  # the owning loop is already gone
            transport.close()
        raw = transport.get_extra_info("socket")
        if isinstance(raw, socket.socket):  # 3.13 may hand back a wrapper object
            with contextlib.suppress(OSError):
                raw.close()

    # ------------------------------------------------------- DatagramProtocol

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None
        self._fail(TrackerConnectionError(f"UDP socket lost: {exc or 'closed'}"))

    def error_received(self, exc: Exception) -> None:
        # An ICMP "port unreachable" is a hint, not an answer: some hosts send
        # it and then reply anyway. The timeout is the authority, so this is
        # only logged.
        logger.debug("UDP error from %s: %s", self.transport, exc)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        waiting = self._waiting
        expected = self._expected
        if waiting is None or waiting.done() or expected is None:
            return  # nobody is listening; the packet is not ours to judge
        if len(data) < HEADER_SIZE:
            return
        action, transaction_id = (int(value) for value in struct.unpack_from(">II", data, 0))
        wanted_action, wanted_id = expected
        if transaction_id != wanted_id:
            return
        if action not in (wanted_action, ACTION_ERROR):
            return
        self._expected = None
        self._waiting = None
        waiting.set_result(bytes(data))

    # --------------------------------------------------------------- internals

    def _fail(self, error: Exception) -> None:
        waiting, self._waiting = self._waiting, None
        self._expected = None
        if waiting is not None and not waiting.done():
            waiting.set_exception(error)


# --------------------------------------------------------------- request builders


def build_connect_request(transaction_id: int) -> bytes:
    """The 16-byte ``connect``: magic, action 0, transaction id."""
    return struct.pack(">QII", PROTOCOL_ID, ACTION_CONNECT, transaction_id & 0xFFFFFFFF)


def build_announce_request(
    connection_id: int,
    transaction_id: int,
    request: AnnounceRequest,
    *,
    key: int | None = None,
    ip: int = 0,
) -> bytes:
    """The 98-byte ``announce``.

    Args:
        connection_id: From a prior ``connect``.
        transaction_id: Fresh per request.
        request: What to announce.
        key: The ``key`` field, which lets a client change address mid-swarm.
            Randomised per tracker when not supplied.
        ip: The ``ip`` field. Left at 0, meaning "use the address the packet
            came from", which is what the specification wants.

    Raises:
        TrackerProtocolError: The request's byte fields are the wrong length.
    """
    if len(request.info_hash) != INFO_HASH_SIZE:
        raise TrackerProtocolError(
            f"info_hash must be {INFO_HASH_SIZE} bytes, got {len(request.info_hash)}"
        )
    if len(request.peer_id) != PEER_ID_SIZE:
        raise TrackerProtocolError(
            f"peer_id must be {PEER_ID_SIZE} bytes, got {len(request.peer_id)}"
        )
    # UDP's num_want is signed and -1 is legal ("send as many as you like");
    # anything below that is nonsense, so it is clamped rather than sent.
    num_want = max(-1, request.num_want)
    event_code = EVENT_CODES.get(request.event, EVENT_NONE)
    payload = struct.pack(
        ">QII",
        connection_id & 0xFFFFFFFFFFFFFFFF,
        ACTION_ANNOUNCE,
        transaction_id & 0xFFFFFFFF,
    )
    payload += request.info_hash + request.peer_id
    payload += struct.pack(">QQQ", request.downloaded, request.left, request.uploaded)
    payload += struct.pack(
        ">IIIiH", event_code, ip, (key or 0) & 0xFFFFFFFF, num_want, request.port
    )
    assert len(payload) == ANNOUNCE_REQUEST_SIZE
    return payload


def build_scrape_request(
    connection_id: int,
    transaction_id: int,
    info_hashes: Sequence[bytes],
) -> bytes:
    """The ``scrape``: a header plus one 20-byte info hash per torrent.

    Raises:
        TrackerProtocolError: No hashes, or more than will fit in one packet.
    """
    if not info_hashes:
        raise TrackerProtocolError("a UDP scrape needs at least one info hash")
    if len(info_hashes) > MAX_SCRAPE_HASHES:
        raise TrackerProtocolError(
            f"a UDP scrape carries at most {MAX_SCRAPE_HASHES} info hashes, "
            f"asked for {len(info_hashes)}"
        )
    for info_hash in info_hashes:
        if len(info_hash) != INFO_HASH_SIZE:
            raise TrackerProtocolError(
                f"info_hash must be {INFO_HASH_SIZE} bytes, got {len(info_hash)}"
            )
    payload = struct.pack(
        ">QII",
        connection_id & 0xFFFFFFFFFFFFFFFF,
        ACTION_SCRAPE,
        transaction_id & 0xFFFFFFFF,
    )
    return payload + b"".join(bytes(info_hash) for info_hash in info_hashes)


# ----------------------------------------------------------------- response readers


def parse_connect_response(raw: bytes, transaction_id: int) -> int:
    """Read the connection id out of a ``connect`` reply.

    Raises:
        TrackerProtocolError: Wrong action, wrong transaction id, or truncated.
    """
    action = _header(raw, transaction_id, minimum=CONNECT_RESPONSE_SIZE)
    if action != ACTION_CONNECT:
        raise TrackerProtocolError(f"expected a connect reply (action 0), got action {action}")
    (connection_id,) = struct.unpack_from(">Q", raw, HEADER_SIZE)
    return int(connection_id)


def parse_announce_response(
    raw: bytes,
    transaction_id: int,
    *,
    max_peers: int = DEFAULT_MAX_PEERS,
    default_interval: int = DEFAULT_ANNOUNCE_INTERVAL,
) -> AnnounceResponse:
    """Read an ``announce`` reply.

    A UDP tracker always reports both swarm counters, unlike an HTTP one, so
    :attr:`~app.tracker.base.AnnounceResponse.swarm_reported` is always true
    for these responses.

    Raises:
        TrackerProtocolError: Wrong action, wrong transaction id, truncated, or
            carrying an error.
    """
    action = _header(raw, transaction_id, minimum=ANNOUNCE_RESPONSE_HEADER_SIZE)
    if action != ACTION_ANNOUNCE:
        raise TrackerProtocolError(f"expected an announce reply (action 1), got action {action}")
    interval, leechers, seeders = (
        int(value) for value in struct.unpack_from(">III", raw, HEADER_SIZE)
    )
    if interval <= 0:
        logger.warning(
            "UDP tracker sent an invalid interval (%d); falling back to %d seconds",
            interval,
            default_interval,
        )
        interval = default_interval
    return AnnounceResponse(
        interval=interval,
        peers=parse_udp_peers(raw[ANNOUNCE_RESPONSE_HEADER_SIZE:], max_peers=max_peers),
        complete=seeders,
        incomplete=leechers,
    )


def parse_scrape_response(
    raw: bytes,
    transaction_id: int,
    info_hashes: Sequence[bytes],
) -> Mapping[bytes, ScrapeResponse]:
    """Read a ``scrape`` reply, one 12-byte entry per requested info hash.

    Entries come back in the order they were asked for. A tracker that knows
    nothing about a hash returns zeroes for it rather than dropping it, so a
    missing key means the tracker stopped answering before the end.

    Raises:
        TrackerProtocolError: Wrong action, wrong transaction id, a body that is
            not a whole number of entries, or more entries than were requested.
    """
    action = _header(raw, transaction_id, minimum=SCRAPE_RESPONSE_HEADER_SIZE)
    if action != ACTION_SCRAPE:
        raise TrackerProtocolError(f"expected a scrape reply (action 2), got action {action}")
    body = raw[SCRAPE_RESPONSE_HEADER_SIZE:]
    if len(body) % SCRAPE_ENTRY_SIZE:
        raise TrackerProtocolError(
            f"scrape body has {len(body)} bytes, which is not a multiple of {SCRAPE_ENTRY_SIZE}"
        )
    entries = len(body) // SCRAPE_ENTRY_SIZE
    if entries > len(info_hashes):
        raise TrackerProtocolError(
            f"tracker returned {entries} scrape entries for {len(info_hashes)} "
            "requested info hashes"
        )
    results: dict[bytes, ScrapeResponse] = {}
    for index in range(entries):
        seeders, completed, leechers = struct.unpack_from(">III", body, index * SCRAPE_ENTRY_SIZE)
        results[bytes(info_hashes[index])] = ScrapeResponse(
            complete=seeders,
            incomplete=leechers,
            downloaded=completed,
        )
    return results


def parse_udp_peers(blob: bytes, *, max_peers: int = DEFAULT_MAX_PEERS) -> tuple[PeerAddress, ...]:
    """The peer list at the end of an announce: 6-byte IPv4 records.

    The format is byte-for-byte the compact IPv4 form an HTTP tracker sends, so
    the parser is shared rather than written twice.
    """
    return tuple(parse_compact_peers_ipv4(blob, max_peers=max_peers))


def error_message(raw: bytes) -> str:
    """The text of an error reply, decoded leniently."""
    return raw[HEADER_SIZE:].decode("utf-8", errors="replace").strip() or "unknown error"


# ----------------------------------------------------------------------- internals


def _header(raw: bytes, transaction_id: int, *, minimum: int) -> int:
    """Validate the 8-byte packet header and return the action.

    An error packet is turned into a :class:`TrackerProtocolError` here, so no
    caller can mistake a refusal for a reply.
    """
    if len(raw) < HEADER_SIZE:
        raise TrackerProtocolError(f"UDP reply of {len(raw)} bytes is too short to carry a header")
    action, received = (int(value) for value in struct.unpack_from(">II", raw, 0))
    if received != transaction_id:
        raise TrackerProtocolError(
            f"UDP reply carried transaction id {received}, expected {transaction_id}"
        )
    if action == ACTION_ERROR:
        raise TrackerProtocolError(f"tracker refused the request: {error_message(raw)}")
    if len(raw) < minimum:
        raise TrackerProtocolError(
            f"UDP reply of {len(raw)} bytes is too short for action {action} (needs {minimum})"
        )
    return action


def _is_error(raw: bytes) -> bool:
    """Whether this packet is a refusal, whatever its transaction id."""
    if len(raw) < HEADER_SIZE:
        return False
    (action, _transaction_id) = struct.unpack_from(">II", raw, 0)
    return int(action) == ACTION_ERROR


__all__ = [
    "ACTION_ANNOUNCE",
    "ACTION_CONNECT",
    "ACTION_ERROR",
    "ACTION_SCRAPE",
    "ANNOUNCE_REQUEST_SIZE",
    "CONNECTION_ID_LIFETIME",
    "DEFAULT_MAX_PEERS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "DEFAULT_UDP_PORT",
    "EVENT_CODES",
    "MAX_SCRAPE_HASHES",
    "PROTOCOL_ID",
    "UdpTracker",
    "build_announce_request",
    "build_connect_request",
    "build_scrape_request",
    "endpoint_of",
    "error_message",
    "parse_announce_response",
    "parse_connect_response",
    "parse_scrape_response",
    "parse_udp_peers",
]
