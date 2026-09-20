"""Standalone HTTP BitTorrent tracker for local testing (TRD §42).

The point of this tool is to make the client testable **without the public
internet** and without a real swarm: it speaks the actual tracker protocol over
real HTTP, so announce parsing, percent-encoding, compact peers and retries are
all exercised end to end.

Run it directly::

    python tools/mock_tracker.py --port 8000 --seeders 5 --leechers 2

or embed it in tests::

    tracker = MockTracker(port=0)
    url = await tracker.start()   # serves on its own thread and loop
    ...
    await tracker.stop()

It tracks a real swarm per info hash: peers that announce are registered and
returned to later announces, so two local seeders can discover each other
exactly as they would against a public tracker.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Final

from aiohttp import web
from app.bencode import encode
from app.core.constants import INFO_HASH_SIZE, MAX_PORT, MIN_PORT, PEER_ID_SIZE
from app.tracker.base import DEFAULT_ANNOUNCE_INTERVAL

logger = logging.getLogger(__name__)

DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 8000
DEFAULT_PEER_TTL: Final[float] = 300.0
MAX_PEERS_RETURNED: Final[int] = 200


@dataclass(slots=True)
class RegisteredPeer:
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


class MockTracker:
    """An in-process HTTP tracker.

    Args:
        host: Bind address.
        port: Bind port; ``0`` picks a free port (what tests use).
        interval: ``interval`` value returned to clients.
        min_interval: Optional ``min interval`` value.
        delay: Seconds to sleep before responding, to exercise client timeouts.
        extra_seeders: Added to the seeder count reported to clients.
        extra_leechers: Added to the leecher count.
        peer_ttl: Seconds before a peer that stops announcing is forgotten.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        interval: int = DEFAULT_ANNOUNCE_INTERVAL,
        min_interval: int | None = None,
        delay: float = 0.0,
        extra_seeders: int = 0,
        extra_leechers: int = 0,
        peer_ttl: float = DEFAULT_PEER_TTL,
        http_error: int | None = None,
        warning: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.interval = interval
        self.min_interval = min_interval
        self.delay = delay
        self.extra_seeders = extra_seeders
        self.extra_leechers = extra_leechers
        self.peer_ttl = peer_ttl
        self.http_error = http_error
        self.warning = warning

        self.swarms: dict[bytes, dict[tuple[str, int], RegisteredPeer]] = {}
        self.announce_count = 0
        self.scrape_count = 0
        self.last_request: dict[str, str] | None = None
        self.last_headers: dict[str, str] | None = None

        self._app = web.Application()
        self._app.router.add_get("/announce", self.handle_announce)
        self._app.router.add_get("/scrape", self.handle_scrape)
        self._app.router.add_get("/stats", self.handle_stats)
        # Endpoints used by tests to exercise client failure handling.
        self._app.router.add_get("/fail", self.handle_fail)
        self._app.router.add_get("/error", self.handle_error)
        self._app.router.add_get("/big", self.handle_big)
        self._app.router.add_get("/malformed-peers", self.handle_malformed_peers)
        self._app.router.add_get("/garbage", self.handle_garbage)
        self._runner: web.AppRunner | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> str:
        """Start serving on its own thread and return the base URL.

        The server deliberately runs on a **separate thread with its own event
        loop**. A tracker is a separate process in reality, and code under test
        often owns the loop it runs on — the CLI's ``asyncio.run`` being the
        obvious example — so sharing a loop would deadlock: the client would
        block the very loop that has to serve it.
        """
        if self._thread is not None:
            return self.url

        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, args=(ready,), name="mock-tracker", daemon=True
        )
        self._thread.start()
        if not ready.wait(timeout=10):
            raise RuntimeError("mock tracker did not start within 10 seconds")
        return self.url

    def _serve(self, ready: threading.Event) -> None:
        """Run the server loop until :meth:`stop` is called."""
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._run(ready))
        finally:
            self._loop.close()
            self._loop = None

    async def _run(self, ready: threading.Event) -> None:
        """Set up the site, wait for the stop signal, then tear it down."""
        self._stop_event = asyncio.Event()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

        addresses = self._runner.addresses
        if addresses and isinstance(addresses[0], tuple):
            self.port = int(addresses[0][1])
        ready.set()

        await self._stop_event.wait()
        await self._runner.cleanup()
        self._runner = None

    async def stop(self) -> None:
        """Stop serving and join the server thread."""
        if self._thread is None or self._loop is None or self._stop_event is None:
            return
        self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._stop_event = None

    async def __aenter__(self) -> MockTracker:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # ------------------------------------------------------------- accessors

    @property
    def url(self) -> str:
        """Base URL, e.g. ``http://127.0.0.1:8000``."""
        return f"http://{self.host}:{self.port}"

    @property
    def announce_url(self) -> str:
        """The announce URL clients should be given."""
        return f"{self.url}/announce"

    @property
    def scrape_url(self) -> str:
        """The scrape URL derived from the announce URL."""
        return f"{self.url}/scrape"

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
    ) -> RegisteredPeer:
        """Pre-register a peer, so announces return a known, deterministic set."""
        swarm = self.swarms.setdefault(info_hash, {})
        peer = RegisteredPeer(
            peer_id=peer_id or (b"M" * PEER_ID_SIZE),
            host=host,
            port=port,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left,
        )
        swarm[peer.key] = peer
        return peer

    def peers_for(self, info_hash: bytes) -> tuple[RegisteredPeer, ...]:
        """Currently registered peers for a torrent."""
        return tuple(self.swarms.get(info_hash, {}).values())

    def counts(self, info_hash: bytes) -> tuple[int, int]:
        """``(seeders, leechers)`` currently registered."""
        peers = self.peers_for(info_hash)
        seeders = sum(1 for peer in peers if peer.left == 0)
        return seeders + self.extra_seeders, (len(peers) - seeders) + self.extra_leechers

    # -------------------------------------------------------------- handlers

    async def handle_announce(self, request: web.Request) -> web.Response:
        """Handle one announce, returning a bencoded tracker response."""
        self.announce_count += 1
        if self.http_error is not None:
            return web.Response(status=self.http_error, body=b"forced error")
        query = dict(_parse_query(request.query_string))
        self.last_request = query
        self.last_headers = dict(request.headers)

        if self.delay:
            await asyncio.sleep(self.delay)

        try:
            info_hash = _required_bytes(query, "info_hash", size=INFO_HASH_SIZE)
            peer_id = _required_bytes(query, "peer_id", size=PEER_ID_SIZE)
            port = _required_int(query, "port", minimum=MIN_PORT, maximum=MAX_PORT)
        except ValueError as exc:
            return _failure(str(exc), status=400)

        uploaded = _optional_int(query, "uploaded", default=0)
        downloaded = _optional_int(query, "downloaded", default=0)
        left = _optional_int(query, "left", default=0)
        event = query.get("event")
        num_want = min(_optional_int(query, "numwant", default=50), MAX_PEERS_RETURNED)
        compact = _optional_int(query, "compact", default=1) != 0

        swarm = self.swarms.setdefault(info_hash, {})
        self._prune(swarm)
        host = request.remote or "127.0.0.1"
        key = (host, port)

        if event == "stopped":
            swarm.pop(key, None)
        else:
            swarm[key] = RegisteredPeer(
                peer_id=peer_id,
                host=host,
                port=port,
                uploaded=uploaded,
                downloaded=downloaded,
                left=left,
            )

        others = [peer for peer_key, peer in swarm.items() if peer_key != key][:num_want]
        seeders, leechers = self.counts(info_hash)

        document: dict[bytes, Any] = {
            b"interval": self.interval,
            b"complete": seeders,
            b"incomplete": leechers,
        }
        if self.min_interval is not None:
            document[b"min interval"] = self.min_interval
        if self.warning is not None:
            document[b"warning message"] = self.warning.encode("utf-8")
        if compact:
            document[b"peers"] = b"".join(_compact_ipv4(peer) for peer in others)
        else:
            document[b"peers"] = [
                {b"peer id": peer.peer_id, b"ip": peer.host.encode(), b"port": peer.port}
                for peer in others
            ]
        return web.Response(body=encode(document), content_type="application/x-bittorrent")

    async def handle_scrape(self, request: web.Request) -> web.Response:
        """Handle a scrape request for one or more info hashes."""
        self.scrape_count += 1
        if self.http_error is not None:
            return web.Response(status=self.http_error, body=b"forced error")
        if self.delay:
            await asyncio.sleep(self.delay)

        info_hashes: list[bytes] = []
        for value in [
            value for name, value in _parse_query(request.query_string) if name == "info_hash"
        ]:
            try:
                info_hashes.append(_decode_bytes(value, "info_hash", size=INFO_HASH_SIZE))
            except ValueError:
                continue

        files: dict[bytes, Any] = {}
        for info_hash in info_hashes:
            self._prune(self.swarms.setdefault(info_hash, {}))
            seeders, leechers = self.counts(info_hash)
            files[info_hash] = {
                b"complete": seeders,
                b"incomplete": leechers,
                b"downloaded": 0,
            }
        return web.Response(body=encode({b"files": files}), content_type="application/x-bittorrent")

    async def handle_fail(self, request: web.Request) -> web.Response:
        """Return a bencoded tracker failure reason."""
        self.announce_count += 1
        return _failure("torrent not registered with this tracker")

    async def handle_error(self, request: web.Request) -> web.Response:
        """Return an HTTP error."""
        self.announce_count += 1
        return web.Response(status=500, body=b"internal error")

    async def handle_big(self, request: web.Request) -> web.Response:
        """Return an absurdly large response body."""
        return web.Response(body=b"x" * (4 * 1024 * 1024))

    async def handle_malformed_peers(self, request: web.Request) -> web.Response:
        """Return a compact peer list whose length is not a multiple of six."""
        return web.Response(
            body=encode({b"interval": self.interval, b"peers": b"\x01\x02\x03\x04\x05"})
        )

    async def handle_garbage(self, request: web.Request) -> web.Response:
        """Return a body that is not bencode at all."""
        return web.Response(body=b"<html>not a tracker</html>")

    async def handle_stats(self, request: web.Request) -> web.Response:
        """JSON diagnostics, used by tests and manual debugging."""
        import json

        payload = {
            "announce_count": self.announce_count,
            "scrape_count": self.scrape_count,
            "last_request": self.last_request,
            "swarms": {
                info_hash.hex(): [
                    {"host": peer.host, "port": peer.port, "left": peer.left}
                    for peer in swarm.values()
                ]
                for info_hash, swarm in self.swarms.items()
            },
        }
        return web.Response(body=json.dumps(payload).encode(), content_type="application/json")

    # -------------------------------------------------------------- internals

    @staticmethod
    def _prune(swarm: dict[tuple[str, int], RegisteredPeer]) -> None:
        cutoff = time.time() - DEFAULT_PEER_TTL
        for key, peer in list(swarm.items()):
            if peer.last_seen < cutoff:
                del swarm[key]


def _compact_ipv4(peer: RegisteredPeer) -> bytes:
    """Encode one peer as a 6-byte compact IPv4 record."""
    return socket.inet_aton(peer.host) + peer.port.to_bytes(2, "big")


def _parse_query(query_string: str) -> list[tuple[str, str]]:
    """Split a raw query string, leaving percent-escapes alone.

    A tracker query is not text: ``info_hash`` and ``peer_id`` are twenty
    arbitrary bytes, and the only reason they travel in a URL at all is that
    somebody percent-encoded them. aiohttp's ``request.query`` hands back
    *decoded* text, which silently turns a byte into a character and back into
    a different number of bytes — an announce that worked for years starts
    failing length checks. Parsing ``query_string`` keeps the bytes the client
    sent, which is what ``_decode_bytes`` expects.
    """
    from urllib.parse import unquote

    pairs: list[tuple[str, str]] = []
    for pair in query_string.split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        pairs.append((unquote(name), value))
    return pairs


def _decode_bytes(value: str, field_name: str, *, size: int) -> bytes:
    """Decode a percent-encoded query value into raw bytes of the exact size."""
    from urllib.parse import unquote_to_bytes

    raw = unquote_to_bytes(value)
    if len(raw) != size:
        raise ValueError(f"{field_name} must be {size} bytes, got {len(raw)}")
    return raw


def _required_bytes(query: dict[str, str], field_name: str, *, size: int) -> bytes:
    value = query.get(field_name)
    if value is None:
        raise ValueError(f"missing required parameter '{field_name}'")
    return _decode_bytes(value, field_name, size=size)


def _required_int(query: dict[str, str], field_name: str, *, minimum: int, maximum: int) -> int:
    raw = query.get(field_name)
    if raw is None:
        raise ValueError(f"missing required parameter '{field_name}'")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"parameter '{field_name}' is not an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"parameter '{field_name}' must be {minimum}-{maximum}")
    return value


def _optional_int(query: dict[str, str], field_name: str, *, default: int) -> int:
    raw = query.get(field_name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _failure(reason: str, *, status: int = 200) -> web.Response:
    """Return a bencoded tracker failure."""
    return web.Response(
        body=encode({b"failure reason": reason.encode("utf-8")}),
        content_type="application/x-bittorrent",
        status=status,
    )


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Run a local HTTP BitTorrent tracker.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=int, default=DEFAULT_ANNOUNCE_INTERVAL)
    parser.add_argument("--delay", type=float, default=0.0, help="delay before responding")
    parser.add_argument("--seeders", type=int, default=0, help="seeders to report additionally")
    parser.add_argument("--leechers", type=int, default=0, help="leechers to report additionally")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    async def serve() -> None:
        tracker = MockTracker(
            args.host,
            args.port,
            interval=args.interval,
            delay=args.delay,
            extra_seeders=args.seeders,
            extra_leechers=args.leechers,
        )
        await tracker.start()
        print(f"mock tracker listening on {tracker.announce_url}")
        print("press Ctrl+C to stop")
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(3600)
        await tracker.stop()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
