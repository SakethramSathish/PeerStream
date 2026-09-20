"""Standalone UDP BitTorrent tracker for local testing (BEP 15).

This is the UDP counterpart of :mod:`tools.mock_tracker`: it speaks the real
wire protocol over real datagrams on a loopback port, so connect/announce/scrape
framing, transaction-id matching, connection-id handling, retries and timeout
handling are all exercised against something that behaves like a tracker rather
than against a mock of the client's own socket layer.

Run it directly::

    python tools/mock_udp_tracker.py --port 6969 --seeders 5 --leechers 2

or embed it in tests::

    tracker = MockUdpTracker(port=0)
    url = await tracker.start()          # udp://127.0.0.1:<port>
    ...
    await tracker.stop()

The tracker keeps a real swarm per info hash: peers that announce are registered
and handed back to later announces, and several misbehaviours can be dialled in
(:class:`UdpTrackerMode`) because a client is only as good as its behaviour when
the tracker is wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import struct
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from app.core.constants import INFO_HASH_SIZE, PEER_ID_SIZE
from app.tracker.base import DEFAULT_ANNOUNCE_INTERVAL
from app.tracker.udp_tracker import (
    ACTION_ANNOUNCE,
    ACTION_CONNECT,
    ACTION_ERROR,
    ACTION_SCRAPE,
    ANNOUNCE_REQUEST_SIZE,
    PROTOCOL_ID,
    SCRAPE_REQUEST_HEADER_SIZE,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 6969
DEFAULT_PEER_TTL: Final[float] = 300.0
MAX_PEERS_RETURNED: Final[int] = 200
HEADER_SIZE: Final[int] = 8


class UdpTrackerMode(StrEnum):
    """How this tracker behaves, so the client can be tested against each."""

    OK = "ok"
    #: Answer nothing at all: the client must decide for itself that it timed out.
    SILENT = "silent"
    #: Answer every request with bytes that are not a tracker packet.
    GARBAGE = "garbage"
    #: Refuse every announce with an error packet, as a stale connection id would.
    STALE = "stale"
    #: Answer an announce with a header and no body.
    TRUNCATED = "truncated"
    #: Reply to connect but never to the announce that follows it.
    SILENT_AFTER_CONNECT = "silent-after-connect"


@dataclass(slots=True)
class UdpPeer:
    """A peer currently announcing to this tracker."""

    peer_id: bytes
    host: str
    port: int
    uploaded: int = 0
    downloaded: int = 0
    left: int = 0
    last_seen: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def compact_ipv4(self) -> bytes:
        """The 6-byte record a tracker puts in an announce response."""
        import socket

        return socket.inet_aton(self.host) + struct.pack(">H", self.port)


class MockUdpTracker(asyncio.DatagramProtocol):
    """An in-process UDP tracker.

    Args:
        host: Bind address.
        port: Bind port; ``0`` picks a free port (what tests use).
        interval: ``interval`` reported to clients.
        extra_seeders: Added to the seeder count reported to clients.
        extra_leechers: Added to the leecher count.
        delay: Seconds to sleep before replying, to exercise client timeouts.
        peer_ttl: Seconds before a peer that stops announcing is forgotten.
        mode: One of :class:`UdpTrackerMode`.
        require_connection_id: Whether an unknown connection id is refused with
            an error packet, the way a real tracker treats one it did not issue.

    Attributes:
        swarms: Registered peers, per info hash.
        connect_count, announce_count, scrape_count: How many of each arrived.
        last_announce: The fields of the last announce, as they arrived on the
            wire — how tests check the client built the request correctly.
        issued_connection_ids: Every connection id handed out, in order.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        interval: int = DEFAULT_ANNOUNCE_INTERVAL,
        extra_seeders: int = 0,
        extra_leechers: int = 0,
        delay: float = 0.0,
        peer_ttl: float = DEFAULT_PEER_TTL,
        mode: UdpTrackerMode | str = UdpTrackerMode.OK,
        require_connection_id: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.interval = interval
        self.extra_seeders = extra_seeders
        self.extra_leechers = extra_leechers
        self.delay = delay
        self.peer_ttl = peer_ttl
        self.mode = UdpTrackerMode(mode)
        self.require_connection_id = require_connection_id

        self.swarms: dict[bytes, dict[tuple[str, int], UdpPeer]] = {}
        self.packets_received = 0
        self.connect_count = 0
        self.announce_count = 0
        self.scrape_count = 0
        self.last_announce: dict[str, object] = {}
        self.issued_connection_ids: list[int] = []

        self.transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._next_connection_id = 0x1234_5678_9ABC_DEF0

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> str:
        """Bind the socket and return the announce URL.

        The tracker serves on the caller's event loop. That is safe — the
        protocol callbacks are cooperative and nothing here blocks — and it is
        what makes it usable from a test that also awaits the client.
        """
        loop = asyncio.get_running_loop()
        _transport, protocol = await loop.create_datagram_endpoint(
            lambda: self, local_addr=(self.host, self.port)
        )
        del protocol
        sockname = self.transport.get_extra_info("sockname") if self.transport else None
        if isinstance(sockname, tuple):
            self.port = int(sockname[1])
        return self.announce_url

    async def stop(self) -> None:
        """Close the socket and drop every pending reply."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    async def __aenter__(self) -> MockUdpTracker:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # ------------------------------------------------------------ accessors

    @property
    def url(self) -> str:
        """The tracker URL, e.g. ``udp://127.0.0.1:6969``."""
        return f"udp://{self.host}:{self.port}"

    @property
    def announce_url(self) -> str:
        """The announce URL clients should be given."""
        return self.url

    def add_peer(
        self,
        info_hash: bytes,
        host: str,
        port: int,
        *,
        peer_id: bytes | None = None,
        left: int = 0,
        uploaded: int = 0,
        downloaded: int = 0,
    ) -> UdpPeer:
        """Register a peer as if it had announced."""
        peer = UdpPeer(
            peer_id=peer_id or bytes([0x2D]) * PEER_ID_SIZE,
            host=host,
            port=port,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left,
        )
        self.swarms.setdefault(info_hash, {})[peer.key] = peer
        return peer

    def peers_for(self, info_hash: bytes) -> tuple[UdpPeer, ...]:
        """Every registered peer for a torrent, oldest announcement first."""
        return tuple(self.swarms.get(info_hash, {}).values())

    def counts(self, info_hash: bytes) -> tuple[int, int]:
        """``(seeders, leechers)`` for a torrent, including the extra counters."""
        peers = self.peers_for(info_hash)
        seeders = sum(1 for peer in peers if peer.left == 0) + self.extra_seeders
        leechers = sum(1 for peer in peers if peer.left > 0) + self.extra_leechers
        return seeders, leechers

    # ------------------------------------------------------- DatagramProtocol

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets_received += 1  # counted before we decide how to misbehave
        task = asyncio.get_running_loop().create_task(self._handle(bytes(data), addr))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def error_received(self, exc: Exception) -> None:
        logger.debug("mock UDP tracker socket error: %s", exc)

    # ------------------------------------------------------------- internals

    async def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < HEADER_SIZE:
            return
        if self.mode is UdpTrackerMode.GARBAGE:
            await self._reply(b"this is not a tracker packet", addr)
            return
        if self.mode is UdpTrackerMode.SILENT:
            return

        first, action, transaction_id = struct.unpack_from(">QII", data, 0)
        if action == ACTION_CONNECT:
            await self._handle_connect(first, transaction_id, addr)
        elif action == ACTION_ANNOUNCE:
            await self._handle_announce(first, transaction_id, data, addr)
        elif action == ACTION_SCRAPE:
            await self._handle_scrape(first, transaction_id, data, addr)
        else:
            await self._error(transaction_id, f"unknown action {action}", addr)

    async def _handle_connect(
        self, protocol_id: int, transaction_id: int, addr: tuple[str, int]
    ) -> None:
        # A real tracker ignores a packet whose magic constant is wrong; it does
        # not explain itself.
        if protocol_id != PROTOCOL_ID:
            logger.debug("ignoring connect with protocol id %#x", protocol_id)
            return
        self.connect_count += 1
        self._next_connection_id = (self._next_connection_id + 1) & 0xFFFFFFFFFFFFFFFF
        connection_id = self._next_connection_id
        self.issued_connection_ids.append(connection_id)
        await self._reply(struct.pack(">IIQ", ACTION_CONNECT, transaction_id, connection_id), addr)

    async def _handle_announce(
        self, connection_id: int, transaction_id: int, data: bytes, addr: tuple[str, int]
    ) -> None:
        self.announce_count += 1
        if self.mode is UdpTrackerMode.SILENT_AFTER_CONNECT:
            return
        if len(data) != ANNOUNCE_REQUEST_SIZE:
            await self._error(transaction_id, "announce request has the wrong length", addr)
            return
        if self.require_connection_id and connection_id not in self.issued_connection_ids:
            await self._error(transaction_id, "Connection ID missmatch.", addr)
            return
        if self.mode is UdpTrackerMode.STALE:
            await self._error(transaction_id, "Connection ID missmatch.", addr)
            return

        info_hash = data[16:36]
        peer_id = data[36:56]
        downloaded, left, uploaded = struct.unpack_from(">QQQ", data, 56)
        event, ip, key, num_want, port = struct.unpack_from(">IIIiH", data, 80)
        self.last_announce = {
            "info_hash": info_hash,
            "peer_id": peer_id,
            "downloaded": downloaded,
            "left": left,
            "uploaded": uploaded,
            "event": event,
            "ip": ip,
            "key": key,
            "num_want": num_want,
            "port": port,
        }

        swarm = self.swarms.setdefault(info_hash, {})
        peer = UdpPeer(
            peer_id=peer_id,
            host=addr[0],
            port=port,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left,
        )
        swarm[peer.key] = peer
        _prune(swarm, self.peer_ttl)

        if self.mode is UdpTrackerMode.TRUNCATED:
            await self._reply(struct.pack(">II", ACTION_ANNOUNCE, transaction_id), addr)
            return

        seeders, leechers = self.counts(info_hash)
        peers = [
            other
            for other in swarm.values()
            if other.key != peer.key  # a tracker does not hand you back yourself
        ][:MAX_PEERS_RETURNED]
        body = struct.pack(">III", self.interval, leechers, seeders)
        body += b"".join(other.compact_ipv4 for other in peers)
        await self._reply(struct.pack(">II", ACTION_ANNOUNCE, transaction_id) + body, addr)

    async def _handle_scrape(
        self, connection_id: int, transaction_id: int, data: bytes, addr: tuple[str, int]
    ) -> None:
        self.scrape_count += 1
        if self.require_connection_id and connection_id not in self.issued_connection_ids:
            await self._error(transaction_id, "Connection ID missmatch.", addr)
            return
        body = data[SCRAPE_REQUEST_HEADER_SIZE:]
        if not body or len(body) % INFO_HASH_SIZE:
            await self._error(transaction_id, "scrape request has the wrong length", addr)
            return
        entries = b""
        for offset in range(0, len(body), INFO_HASH_SIZE):
            info_hash = body[offset : offset + INFO_HASH_SIZE]
            seeders, leechers = self.counts(info_hash)
            # BEP 15's middle counter is "completed": how often the torrent has
            # been finished, not how many are seeding now.
            entries += struct.pack(">III", seeders, len(self.swarms.get(info_hash, {})), leechers)
        await self._reply(struct.pack(">II", ACTION_SCRAPE, transaction_id) + entries, addr)

    # ------------------------------------------------------------- sending

    async def _error(self, transaction_id: int, message: str, addr: tuple[str, int]) -> None:
        payload = struct.pack(">II", ACTION_ERROR, transaction_id) + message.encode("utf-8")
        await self._reply(payload, addr)

    async def _reply(self, payload: bytes, addr: tuple[str, int]) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.transport is not None and not self.transport.is_closing():
            self.transport.sendto(payload, addr)


def _prune(swarm: dict[tuple[str, int], UdpPeer], peer_ttl: float) -> None:
    """Forget peers that stopped announcing."""
    cutoff = time.time() - peer_ttl
    for key, peer in list(swarm.items()):
        if peer.last_seen < cutoff:
            del swarm[key]


def main(argv: list[str] | None = None) -> int:
    """Serve a UDP tracker until interrupted."""
    parser = argparse.ArgumentParser(description="Local UDP BitTorrent tracker (BEP 15)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--seeders", type=int, default=0, help="extra seeders to report")
    parser.add_argument("--leechers", type=int, default=0, help="extra leechers to report")
    parser.add_argument("--interval", type=int, default=DEFAULT_ANNOUNCE_INTERVAL)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    async def serve() -> None:
        tracker = MockUdpTracker(
            args.host,
            args.port,
            interval=args.interval,
            extra_seeders=args.seeders,
            extra_leechers=args.leechers,
        )
        url = await tracker.start()
        logger.info("UDP tracker listening on %s", url)
        try:
            await asyncio.Event().wait()
        finally:
            await tracker.stop()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
