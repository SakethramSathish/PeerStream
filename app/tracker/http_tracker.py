"""HTTP/HTTPS tracker client (BEP 3, BEP 23).

An announce is a plain ``GET`` whose query carries the client's identity
(``info_hash``, ``peer_id``) and progress (``uploaded``, ``downloaded``,
``left``). The response is bencoded, which is why the parsing helpers below are
pure functions: they can be tested against recorded byte strings without a
server, and they are the part where tracker-supplied data has to be validated.

Two details that break naive implementations:

**The byte fields must be percent-encoded, not decoded.** ``info_hash`` and
``peer_id`` are arbitrary 20-byte values; treating them as text corrupts them.
:meth:`AnnounceRequest.to_query` produces pre-encoded values and the URL is
assembled with :func:`~app.tracker.base.append_query`, which also handles
trackers that already carry a ``?passkey=...`` query.

**Peers come in three shapes.** A compact string of 6-byte IPv4 records, a
compact string of 18-byte IPv6 records (``peers6``), or the legacy list of
dictionaries. A client that only handles the first silently loses peers on
IPv6-heavy swarms, so all three are supported.
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Mapping, Sequence
from typing import Final

import aiohttp
from aiohttp import ClientTimeout

from app.bencode import decode as bdecode
from app.bencode.errors import BencodeDecodeError
from app.core.constants import INFO_HASH_SIZE, MAX_PORT, MIN_PORT
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
    append_query,
)
from app.tracker.errors import (
    TrackerConnectionError,
    TrackerProtocolError,
    TrackerTimeoutError,
)

logger = logging.getLogger(__name__)

COMPACT_IPV4_RECORD_SIZE: Final[int] = 6  # 4-byte IPv4 + 2-byte port
COMPACT_IPV6_RECORD_SIZE: Final[int] = 18  # 16-byte IPv6 + 2-byte port
DEFAULT_MAX_PEERS: Final[int] = 300
DEFAULT_MAX_RESPONSE_BYTES: Final[int] = 1024 * 1024  # 1 MiB is already absurd
DEFAULT_TIMEOUT: Final[float] = 15.0


def _as_text(value: object) -> str:
    """Render a bencoded scalar as text for error messages."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _as_int(
    value: object,
    *,
    field: str,
    default: int | None = None,
    strict: bool = True,
) -> int | None:
    """Coerce a bencoded integer, rejecting booleans and non-numbers.

    Args:
        value: The decoded value, or ``None`` when the field is absent.
        field: Field name, used in the error message.
        default: Returned when the field is absent (always) or, with
            ``strict=False``, when it is present but not a usable integer.
        strict: When True a malformed value raises; when False it falls back to
            ``default``. Required fields are strict; optional counters are not,
            so one junk field cannot void an otherwise good announce.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        if strict:
            raise TrackerProtocolError(f"tracker response field '{field}' is not an integer")
        return default
    return value


def parse_compact_peers_ipv4(
    blob: bytes, *, max_peers: int = DEFAULT_MAX_PEERS
) -> list[PeerAddress]:
    """Parse the compact IPv4 peer format: 4-byte address + 2-byte big-endian port."""
    if len(blob) % COMPACT_IPV4_RECORD_SIZE:
        raise TrackerProtocolError(
            f"compact IPv4 peer list has {len(blob)} bytes, "
            f"which is not a multiple of {COMPACT_IPV4_RECORD_SIZE}"
        )

    peers: list[PeerAddress] = []
    for offset in range(0, len(blob), COMPACT_IPV4_RECORD_SIZE):
        record = blob[offset : offset + COMPACT_IPV4_RECORD_SIZE]
        host = socket.inet_ntoa(record[:4])
        port = int.from_bytes(record[4:6], "big")
        if port == 0:
            continue  # a peer announcing port 0 is not listening
        peers.append(PeerAddress(host=host, port=port))
        if len(peers) >= max_peers:
            break
    return peers


def parse_compact_peers_ipv6(
    blob: bytes, *, max_peers: int = DEFAULT_MAX_PEERS
) -> list[PeerAddress]:
    """Parse the compact IPv6 peer format: 16-byte address + 2-byte big-endian port."""
    if len(blob) % COMPACT_IPV6_RECORD_SIZE:
        raise TrackerProtocolError(
            f"compact IPv6 peer list has {len(blob)} bytes, "
            f"which is not a multiple of {COMPACT_IPV6_RECORD_SIZE}"
        )

    peers: list[PeerAddress] = []
    for offset in range(0, len(blob), COMPACT_IPV6_RECORD_SIZE):
        record = blob[offset : offset + COMPACT_IPV6_RECORD_SIZE]
        # The length check above guarantees a full 16-byte address here.
        host = socket.inet_ntop(socket.AF_INET6, record[:16])
        port = int.from_bytes(record[16:18], "big")
        if port == 0:
            continue
        peers.append(PeerAddress(host=host, port=port))
        if len(peers) >= max_peers:
            break
    return peers


def parse_dictionary_peers(
    entries: object, *, max_peers: int = DEFAULT_MAX_PEERS
) -> list[PeerAddress]:
    """Parse the legacy ``peers`` list of ``{"ip", "port", "peer id"}`` dicts.

    Entries that are malformed are skipped with a debug log: one bad entry among
    fifty good ones should cost us that peer, not the whole announce.
    """
    if not isinstance(entries, list):
        raise TrackerProtocolError("dictionary peer list is not a list")

    peers: list[PeerAddress] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            logger.debug("skipping peer entry %d: not a dictionary", index)
            continue

        raw_host = entry.get(b"ip")
        port = _as_int(entry.get(b"port"), field=f"peers[{index}].port", default=None)
        if raw_host is None or port is None:
            logger.debug("skipping peer entry %d: missing ip or port", index)
            continue
        if not MIN_PORT <= port <= MAX_PORT:
            logger.debug("skipping peer entry %d: port %s out of range", index, port)
            continue

        host = _as_text(raw_host).strip()
        if not host:
            continue

        raw_peer_id = entry.get(b"peer id")
        peer_id = bytes(raw_peer_id) if isinstance(raw_peer_id, (bytes, bytearray)) else None
        if peer_id is not None and len(peer_id) != 20:
            peer_id = None

        peers.append(PeerAddress(host=host, port=port, peer_id=peer_id))
        if len(peers) >= max_peers:
            break
    return peers


def parse_peers(
    document: Mapping[bytes, object], *, max_peers: int = DEFAULT_MAX_PEERS
) -> tuple[PeerAddress, ...]:
    """Extract peers from a decoded announce response, in any supported form."""
    peers: list[PeerAddress] = []

    raw_ipv4 = document.get(b"peers")
    if isinstance(raw_ipv4, (bytes, bytearray)):
        peers.extend(parse_compact_peers_ipv4(bytes(raw_ipv4), max_peers=max_peers))
    elif isinstance(raw_ipv4, list):
        peers.extend(parse_dictionary_peers(raw_ipv4, max_peers=max_peers))
    elif raw_ipv4 is not None:
        raise TrackerProtocolError(f"'peers' has unsupported type {type(raw_ipv4).__name__}")

    raw_ipv6 = document.get(b"peers6")
    if isinstance(raw_ipv6, (bytes, bytearray)):
        peers.extend(parse_compact_peers_ipv6(bytes(raw_ipv6), max_peers=max_peers))
    elif raw_ipv6 is not None:
        raise TrackerProtocolError(f"'peers6' has unsupported type {type(raw_ipv6).__name__}")

    return tuple(peers[:max_peers])


def parse_announce_response(
    raw: bytes,
    *,
    max_peers: int = DEFAULT_MAX_PEERS,
    default_interval: int = DEFAULT_ANNOUNCE_INTERVAL,
) -> AnnounceResponse:
    """Parse a bencoded announce response into an :class:`AnnounceResponse`.

    Args:
        raw: The exact response body.
        max_peers: Hard cap on how many peers are accepted.
        default_interval: Used when the tracker omits or misreports ``interval``.

    Returns:
        The validated response.

    Raises:
        TrackerProtocolError: If the body is not bencode, is a tracker failure,
            or has an unusable ``interval``.
    """
    try:
        document = bdecode(raw)
    except BencodeDecodeError as exc:
        raise TrackerProtocolError(f"tracker response is not valid bencode: {exc}") from exc

    if not isinstance(document, dict):
        raise TrackerProtocolError(
            f"tracker response must be a dictionary, got {type(document).__name__}"
        )

    if failure := document.get(b"failure reason"):
        raise TrackerProtocolError(f"tracker refused the announce: {_as_text(failure)}")

    interval = _as_int(document.get(b"interval"), field="interval")
    if interval is None or interval <= 0:
        logger.warning(
            "tracker sent an invalid interval (%r); falling back to %d seconds",
            interval,
            default_interval,
        )
        interval = default_interval

    min_interval = _as_int(document.get(b"min interval"), field="min interval")
    if min_interval is not None and min_interval <= 0:
        min_interval = None

    warning = document.get(b"warning message")
    raw_tracker_id = document.get(b"tracker id")
    tracker_id = bytes(raw_tracker_id) if isinstance(raw_tracker_id, (bytes, bytearray)) else None

    return AnnounceResponse(
        interval=interval,
        peers=parse_peers(document, max_peers=max_peers),
        complete=_as_int(document.get(b"complete"), field="complete", strict=False),
        incomplete=_as_int(document.get(b"incomplete"), field="incomplete", strict=False),
        min_interval=min_interval,
        tracker_id=tracker_id,
        warning_message=_as_text(warning) if warning is not None else None,
    )


def parse_scrape_response(raw: bytes) -> Mapping[bytes, ScrapeResponse]:
    """Parse a bencoded scrape response keyed by info hash.

    Raises:
        TrackerProtocolError: If the body is not bencode or lacks ``files``.
    """
    try:
        document = bdecode(raw)
    except BencodeDecodeError as exc:
        raise TrackerProtocolError(f"scrape response is not valid bencode: {exc}") from exc

    if not isinstance(document, dict):
        raise TrackerProtocolError("scrape response must be a dictionary")
    if failure := document.get(b"failure reason"):
        raise TrackerProtocolError(f"tracker refused the scrape: {_as_text(failure)}")

    files = document.get(b"files")
    if files is None:
        raise TrackerProtocolError("scrape response has no 'files' dictionary")
    if not isinstance(files, dict):
        raise TrackerProtocolError("scrape response 'files' is not a dictionary")

    results: dict[bytes, ScrapeResponse] = {}
    for info_hash, entry in files.items():
        if (
            not isinstance(info_hash, (bytes, bytearray))
            or len(info_hash) != INFO_HASH_SIZE
            or not isinstance(entry, dict)
        ):
            logger.debug("skipping malformed scrape entry")
            continue
        name = entry.get(b"name")
        results[bytes(info_hash)] = ScrapeResponse(
            complete=_as_int(entry.get(b"complete"), field="complete", default=0) or 0,
            incomplete=_as_int(entry.get(b"incomplete"), field="incomplete", default=0) or 0,
            downloaded=_as_int(entry.get(b"downloaded"), field="downloaded"),
            name=_as_text(name) if name is not None else None,
        )
    return results


class HttpTracker(Tracker):
    """HTTP/HTTPS tracker client.

    Args:
        url: Announce URL.
        session: Optional shared :class:`aiohttp.ClientSession`. When omitted,
            one is created on first use and closed by :meth:`aclose`.
        timeout: Total request timeout in seconds.
        max_peers: Cap on peers accepted from a response.
        max_response_bytes: Reject larger bodies instead of parsing them.
        event_bus: Optional bus for tracker events.
        agent: ``User-Agent`` header value.
    """

    SUPPORTED_SCHEMES = ("http", "https")

    def __init__(
        self,
        url: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_peers: int = DEFAULT_MAX_PEERS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        event_bus: EventBus | None = None,
        agent: str | None = None,
    ) -> None:
        super().__init__(url)
        self._session = session
        self._owns_session = session is None
        self._timeout = ClientTimeout(total=timeout)
        self._max_peers = max_peers
        self._max_response_bytes = max_response_bytes
        self._event_bus = event_bus
        self._agent = agent or user_agent()

    # ------------------------------------------------------------------ API

    async def announce(self, request: AnnounceRequest) -> AnnounceResponse:
        """Send an announce and return the validated response.

        Raises:
            TrackerTimeoutError: The tracker did not answer in time.
            TrackerConnectionError: Unreachable, or a non-200 status.
            TrackerProtocolError: The response was malformed or a failure.
        """
        session = self._ensure_session()
        url = append_query(self.url, request.to_query())
        self._publish_request(request)

        started = time.perf_counter()
        try:
            async with session.get(url, timeout=self._timeout) as response:
                status = response.status
                raw = await response.read()
        except TimeoutError as exc:
            self._publish_failure(f"timed out after {self._timeout.total}s")
            raise TrackerTimeoutError(
                f"{self.host} timed out after {self._timeout.total}s"
            ) from exc
        except aiohttp.ClientError as exc:
            self._publish_failure(f"connection failed: {exc}")
            raise TrackerConnectionError(f"{self.host} is unreachable: {exc}") from exc

        latency_ms = (time.perf_counter() - started) * 1000

        if status != 200:
            self._publish_failure(f"HTTP {status}")
            raise TrackerConnectionError(f"{self.host} returned HTTP {status}")
        if len(raw) > self._max_response_bytes:
            self._publish_failure("oversized response")
            raise TrackerProtocolError(
                f"tracker response of {len(raw)} bytes exceeds the "
                f"{self._max_response_bytes} byte limit"
            )

        try:
            parsed = parse_announce_response(raw, max_peers=self._max_peers)
        except TrackerProtocolError as exc:
            self._publish_failure(str(exc))
            raise

        self._publish_response(parsed, latency_ms=latency_ms)
        return parsed

    async def scrape(self, info_hashes: Sequence[bytes]) -> Mapping[bytes, ScrapeResponse]:
        """Scrape swarm counters for one or more torrents.

        Raises:
            TrackerConnectionError: Unreachable, or a non-200 status.
            TrackerProtocolError: Malformed response, or the URL has no
                ``scrape`` counterpart we can derive.
        """
        session = self._ensure_session()
        url = self._scrape_url(info_hashes)

        try:
            async with session.get(url, timeout=self._timeout) as response:
                status = response.status
                raw = await response.read()
        except TimeoutError as exc:
            raise TrackerTimeoutError(f"{self.host} timed out while scraping") from exc
        except aiohttp.ClientError as exc:
            raise TrackerConnectionError(f"{self.host} is unreachable: {exc}") from exc

        if status != 200:
            raise TrackerConnectionError(f"{self.host} returned HTTP {status} for scrape")
        return parse_scrape_response(raw)

    async def aclose(self) -> None:
        """Close the session if this tracker created it."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------- internals

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Create the session lazily, since it binds to a running event loop."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self._agent},
                timeout=self._timeout,
            )
            self._owns_session = True
        return self._session

    def _scrape_url(self, info_hashes: Sequence[bytes]) -> str:
        """Derive the scrape URL and append one ``info_hash`` per torrent."""
        if "/announce" not in self.url:
            raise TrackerProtocolError(
                f"cannot derive a scrape URL from {self.url!r} (no '/announce' path)"
            )
        base = self.url.replace("/announce", "/scrape")
        if not info_hashes:
            return base
        from urllib.parse import quote_from_bytes

        query = "&".join(f"info_hash={quote_from_bytes(h)}" for h in info_hashes)
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{query}"

    def _publish_request(self, request: AnnounceRequest) -> None:
        if self._event_bus is None:
            return
        self._event_bus.emit(
            make_event(
                EventType.TRACKER_REQUEST,
                message=f"announce to {self.host} ({request.event or 'periodic'})",
                torrent_id=request.info_hash.hex(),
                data={"url": self.url, "event": request.event.value if request.event else None},
            )
        )

    def _publish_response(self, response: AnnounceResponse, *, latency_ms: float) -> None:
        if self._event_bus is None:
            return
        if response.warning_message:
            self._event_bus.emit(
                make_event(
                    EventType.TRACKER_WARNING,
                    message=response.warning_message,
                    level=logging.WARNING,
                    data={"url": self.url},
                )
            )
        self._event_bus.emit(
            make_event(
                EventType.TRACKER_RESPONSE,
                message=(
                    f"{len(response.peers)} peers, {response.seeders} seeders ({latency_ms:.0f} ms)"
                ),
                data={
                    "url": self.url,
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
