"""Tracker protocol models and the client interface (TRD §31).

A tracker is a rendezvous service: the client tells it "I want this torrent,
here is the port I listen on" and receives a list of peers plus how long to wait
before asking again. That is all it does — no tracker ever sees file contents.

Two models live here:

* :class:`AnnounceRequest` / :class:`AnnounceResponse` — the wire shapes shared
  by every tracker protocol (HTTP now, UDP in M14).
* :class:`PeerAddress` — a discovered peer, which the peer manager (M5) consumes
  without caring which tracker, DHT node or PEX exchange produced it.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import quote, quote_from_bytes, urlsplit

from app.core.constants import INFO_HASH_SIZE, MAX_PORT, MIN_PORT, PEER_ID_SIZE
from app.tracker.errors import UnsupportedTrackerError

DEFAULT_NUM_WANT: Final[int] = 50
DEFAULT_ANNOUNCE_INTERVAL: Final[int] = 1800  # 30 minutes, the usual default


class TrackerEvent(StrEnum):
    """The ``event`` parameter of an announce (BEP 3).

    Omitted entirely for periodic announces, which is why the request field is
    ``TrackerEvent | None`` rather than defaulting to a value.
    """

    STARTED = "started"
    STOPPED = "stopped"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class PeerAddress:
    """A peer discovered from any source.

    Attributes:
        host: IP address or hostname.
        port: Listening port.
        peer_id: Peer id announced by the tracker, when it provides one.
        source: Where the peer came from — ``"tracker"``, ``"dht"``, ``"pex"``.
            Recorded because the UI shows provenance, and because a peer's
            trustworthiness depends on where it was learned.
    """

    host: str
    port: int
    peer_id: bytes | None = None
    source: str = "tracker"

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("peer host must not be empty")
        if not MIN_PORT <= self.port <= MAX_PORT:
            raise ValueError(f"peer port {self.port} is outside {MIN_PORT}-{MAX_PORT}")
        if self.peer_id is not None and len(self.peer_id) != PEER_ID_SIZE:
            raise ValueError(f"peer id must be {PEER_ID_SIZE} bytes, got {len(self.peer_id)}")

    @property
    def address(self) -> tuple[str, int]:
        """The ``(host, port)`` pair, the identity used for deduplication."""
        return (self.host, self.port)

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class AnnounceRequest:
    """The parameters of an announce, before URL encoding.

    ``info_hash`` and ``peer_id`` are raw 20-byte values on the wire; the HTTP
    client percent-encodes them (see :meth:`to_query`). Passing them through as
    text would corrupt every announce, since arbitrary bytes are not UTF-8.
    """

    info_hash: bytes
    peer_id: bytes
    port: int
    uploaded: int = 0
    downloaded: int = 0
    left: int = 0
    event: TrackerEvent | None = None
    compact: bool = True
    num_want: int = DEFAULT_NUM_WANT
    key: str | None = None
    tracker_id: bytes | None = None

    def __post_init__(self) -> None:
        if len(self.info_hash) != INFO_HASH_SIZE:
            raise ValueError(f"info_hash must be {INFO_HASH_SIZE} bytes, got {len(self.info_hash)}")
        if len(self.peer_id) != PEER_ID_SIZE:
            raise ValueError(f"peer_id must be {PEER_ID_SIZE} bytes, got {len(self.peer_id)}")
        if not MIN_PORT <= self.port <= MAX_PORT:
            raise ValueError(f"port {self.port} is outside {MIN_PORT}-{MAX_PORT}")
        if min(self.uploaded, self.downloaded, self.left) < 0:
            raise ValueError("byte counters must not be negative")
        if self.num_want < 0:
            raise ValueError("num_want must not be negative")

    @property
    def is_seeding(self) -> bool:
        """True when nothing is left to download."""
        return self.left == 0

    def to_query(self) -> dict[str, str]:
        """Render the announce as percent-encoded query parameters.

        Values are returned pre-encoded because the byte fields are not text:
        re-encoding an already-encoded value would turn ``%12`` into ``%2512``.
        """
        query: dict[str, str] = {
            "info_hash": quote_from_bytes(self.info_hash),
            "peer_id": quote_from_bytes(self.peer_id),
            "port": str(self.port),
            "uploaded": str(self.uploaded),
            "downloaded": str(self.downloaded),
            "left": str(self.left),
            "compact": "1" if self.compact else "0",
            "numwant": str(self.num_want),
        }
        if self.event is not None:
            query["event"] = self.event.value
        if self.key:
            query["key"] = quote(self.key, safe="")
        if self.tracker_id:
            query["trackerid"] = quote_from_bytes(self.tracker_id)
        return query


@dataclass(frozen=True, slots=True)
class AnnounceResponse:
    """A parsed, validated announce response.

    Attributes:
        interval: Seconds the client should wait before the next announce.
        peers: Peers returned by the tracker (already filtered and capped).
        complete: Number of seeders in the swarm, if the tracker reports it.
        incomplete: Number of leechers, if reported.
        min_interval: Floor the client must respect; overrides a smaller
            ``interval`` (some trackers send both, inconsistently).
        tracker_id: Opaque id to send back on the next announce.
        warning_message: Human-readable warning the tracker asked us to surface.
    """

    interval: int
    peers: tuple[PeerAddress, ...] = ()
    complete: int | None = None
    incomplete: int | None = None
    min_interval: int | None = None
    tracker_id: bytes | None = None
    warning_message: str | None = None

    @property
    def seeders(self) -> int:
        """Number of seeders; 0 when the tracker did not say."""
        return self.complete or 0

    @property
    def leechers(self) -> int:
        """Number of leechers; 0 when the tracker did not say."""
        return self.incomplete or 0

    @property
    def swarm_reported(self) -> bool:
        """Whether the tracker reported the swarm size at all.

        Plenty of trackers answer with peers but no ``complete``/``incomplete``.
        The UI must distinguish "zero peers in the swarm" from "the tracker
        never told us" — showing 0 for the second case is a fabricated number.
        """
        return self.complete is not None or self.incomplete is not None


@dataclass(frozen=True, slots=True)
class ScrapeResponse:
    """Swarm counters for one torrent, from a scrape request."""

    complete: int = 0
    incomplete: int = 0
    downloaded: int | None = None
    name: str | None = None


class TrackerState(StrEnum):
    """Health of a tracker, for the UI's tracker explorer (PRD §10.7)."""

    UNKNOWN = "unknown"
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"


@dataclass(slots=True)
class TrackerStatus:
    """Mutable health record for one tracker URL."""

    url: str
    state: TrackerState = TrackerState.UNKNOWN
    last_announce_at: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    seeders: int = 0
    leechers: int = 0
    peers_returned: int = 0
    latency_ms: float | None = None
    next_announce_at: float | None = None

    def record_success(
        self,
        response: AnnounceResponse,
        *,
        latency_ms: float,
        next_announce_at: float | None = None,
    ) -> None:
        """Update the record after a successful announce."""
        now = time.time()
        self.state = TrackerState.WARNING if response.warning_message else TrackerState.OK
        self.last_announce_at = now
        self.last_success_at = now
        self.last_error = None
        self.consecutive_failures = 0
        self.seeders = response.seeders
        self.leechers = response.leechers
        self.peers_returned = len(response.peers)
        self.latency_ms = latency_ms
        self.next_announce_at = next_announce_at

    def record_failure(self, error: str) -> None:
        """Update the record after a failed announce."""
        self.state = TrackerState.FAILED
        self.last_announce_at = time.time()
        self.last_error = error
        self.consecutive_failures += 1
        self.latency_ms = None


class Tracker(ABC):
    """Interface every tracker client implements.

    Subclasses are transport-specific (HTTP/HTTPS and UDP); the tracker manager
    works against this interface and never learns the difference.
    """

    #: URL schemes this client can speak.
    SUPPORTED_SCHEMES: tuple[str, ...] = ()

    def __init__(self, url: str) -> None:
        if not url:
            raise UnsupportedTrackerError("tracker URL must not be empty")
        scheme = urlsplit(url).scheme.lower()
        if self.SUPPORTED_SCHEMES and scheme not in self.SUPPORTED_SCHEMES:
            raise UnsupportedTrackerError(
                f"{type(self).__name__} cannot speak {scheme or 'a scheme-less URL'}://"
            )
        self.url = url

    @property
    def scheme(self) -> str:
        """Lower-cased URL scheme."""
        return urlsplit(self.url).scheme.lower()

    @property
    def host(self) -> str:
        """Tracker hostname, for logging and status display."""
        return urlsplit(self.url).hostname or self.url

    @abstractmethod
    async def announce(self, request: AnnounceRequest) -> AnnounceResponse:
        """Send an announce and return the parsed response."""

    @abstractmethod
    async def scrape(self, info_hashes: Sequence[bytes]) -> Mapping[bytes, ScrapeResponse]:
        """Scrape swarm counters for one or more info hashes."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""

    async def __aenter__(self) -> Tracker:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __str__(self) -> str:
        return self.url


def build_query_string(params: Mapping[str, str]) -> str:
    """Join pre-encoded parameters into a query string.

    Values must already be percent-encoded (see :meth:`AnnounceRequest.to_query`);
    only the keys are encoded here, since those are ours.
    """
    return "&".join(f"{quote(key, safe='')}={value}" for key, value in params.items())


def append_query(url: str, params: Mapping[str, str]) -> str:
    """Append parameters to a URL, preserving any query it already has.

    Trackers are often configured with a passkey in the query string
    (``/announce?passkey=abc``), so the separator must be chosen, not assumed.
    """
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{build_query_string(params)}"
