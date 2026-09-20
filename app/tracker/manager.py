"""Tracker manager: tiers, health, retries and scheduling (TRD §31, §43).

One manager per torrent. It owns the tracker list derived from the torrent's
``announce``/``announce-list``, decides which tracker to talk to, remembers what
each one last told us, and keeps announcing on schedule.

Behaviour worth knowing:

**Tiers are tried in order, and only until one succeeds.** BEP 12 says every
tracker in a tier should be tried before moving to the next tier; in practice
clients announce to all tiers' first entries and use whichever answer arrives.
This implementation tries trackers in tier order and stops at the first success,
which keeps announce traffic (and the load we put on public trackers) low.

**Failures back off, successes reset.** A tracker that fails repeatedly is
retried after an exponentially growing delay, capped at
``max_announce_interval``. Existing peer connections are never torn down by a
tracker failure — that is the whole point of keeping them separate.

**Intervals are clamped.** Trackers have been known to send ``interval: 0`` or
absurdly large values; the configured min/max bounds (multiplied by
``announce_interval_multiplier``) always win.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import aiohttp

from app.core.config import TrackerConfig
from app.core.constants import DEFAULT_LISTEN_PORT
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.core.peer_id import generate_peer_id
from app.torrent.metadata import Torrent
from app.tracker.base import (
    AnnounceRequest,
    AnnounceResponse,
    PeerAddress,
    Tracker,
    TrackerEvent,
    TrackerStatus,
)
from app.tracker.errors import TrackerError, UnsupportedTrackerError
from app.tracker.factory import build_tracker

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AnnounceOutcome:
    """The result of one successful announce.

    Attributes:
        tracker: The tracker that answered.
        response: The parsed response.
        peers: Every peer the tracker returned.
        new_peers: Peers not seen before from any tracker — what the peer
            manager actually cares about.
        latency_ms: Round-trip time of the announce.
        next_announce_in: Seconds to wait before the next announce.
    """

    tracker: Tracker
    response: AnnounceResponse
    peers: tuple[PeerAddress, ...]
    new_peers: tuple[PeerAddress, ...]
    latency_ms: float
    next_announce_in: float


@dataclass(slots=True)
class TrackerManager:
    """Owns the trackers for one torrent and drives announces.

    Args:
        torrent: The torrent being announced.
        trackers: Explicit trackers, grouped in tiers. Derived from the torrent
            when omitted.
        config: Tracker settings (intervals, timeouts, retries).
        peer_id: Our peer id; generated when omitted.
        port: The port we accept peer connections on.
        event_bus: Optional bus for tracker events.
        session: Optional shared HTTP session; when omitted each tracker owns
            (and closes) its own. Either way the caller never closes it here.
    """

    torrent: Torrent
    trackers: tuple[tuple[Tracker, ...], ...] = ()
    config: TrackerConfig = field(default_factory=TrackerConfig)
    peer_id: bytes = field(default_factory=generate_peer_id)
    port: int = DEFAULT_LISTEN_PORT
    event_bus: EventBus | None = None
    session: aiohttp.ClientSession | None = None

    _status: dict[str, TrackerStatus] = field(default_factory=dict, init=False, repr=False)
    _seen_peers: set[tuple[str, int]] = field(default_factory=set, init=False, repr=False)
    _periodic_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.trackers:
            self.trackers = build_tracker_tiers(
                self.torrent,
                config=self.config,
                event_bus=self.event_bus,
                session=self.session,
            )
        for tier in self.trackers:
            for tracker in tier:
                self._status.setdefault(tracker.url, TrackerStatus(url=tracker.url))

    # ------------------------------------------------------------- accessors

    @property
    def all_trackers(self) -> tuple[Tracker, ...]:
        """Every tracker, flattened across tiers."""
        return tuple(tracker for tier in self.trackers for tracker in tier)

    @property
    def statuses(self) -> tuple[TrackerStatus, ...]:
        """Health record for every tracker, in announce order."""
        return tuple(self._status[tracker.url] for tracker in self.all_trackers)

    @property
    def known_peers(self) -> tuple[PeerAddress, ...]:
        """Peers this manager has seen, in discovery order.

        Useful for provenance-free inspection: an announce answer, a scrape
        answer, the union. The swarm itself belongs to
        :class:`~app.peer.discovery.peer_manager.PeerManager`, which is where
        these addresses go to be deduplicated and dialled.
        """
        return tuple(PeerAddress(host=host, port=port) for host, port in sorted(self._seen_peers))

    @property
    def seeders(self) -> int:
        """Best seeder count reported by any tracker."""
        return max((status.seeders for status in self._status.values()), default=0)

    @property
    def leechers(self) -> int:
        """Best leecher count reported by any tracker."""
        return max((status.leechers for status in self._status.values()), default=0)

    def status_for(self, url: str) -> TrackerStatus | None:
        """Health record for a specific tracker URL."""
        return self._status.get(url)

    # -------------------------------------------------------------- announce

    async def announce(
        self,
        *,
        uploaded: int = 0,
        downloaded: int = 0,
        left: int | None = None,
        event: TrackerEvent | None = None,
        num_want: int = 50,
    ) -> AnnounceOutcome:
        """Announce to the trackers, stopping at the first successful answer.

        Args:
            uploaded: Bytes uploaded so far.
            downloaded: Bytes downloaded so far.
            left: Bytes still needed; derived from the torrent when omitted.
            event: Announce event (started/stopped/completed) or ``None``.
            num_want: How many peers to ask for.

        Returns:
            The outcome of the successful announce.

        Raises:
            TrackerError: If every tracker failed, or none is usable.
        """
        if not self.all_trackers:
            raise TrackerError(
                f"torrent {self.torrent.name!r} has no usable trackers "
                "(and this announce was not given the DHT to fall back on)"
            )

        request = AnnounceRequest(
            info_hash=self.torrent.info_hash,
            peer_id=self.peer_id,
            port=self.port,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left if left is not None else max(self.torrent.total_length - downloaded, 0),
            event=event,
            num_want=num_want,
        )

        last_error: Exception | None = None
        for tier in self.trackers:
            for tracker in tier:
                status = self._status[tracker.url]
                try:
                    started = time.perf_counter()
                    response = await tracker.announce(request)
                    latency_ms = (time.perf_counter() - started) * 1000
                except TrackerError as exc:
                    status.record_failure(str(exc))
                    logger.warning("announce to %s failed: %s", tracker.url, exc)
                    last_error = exc
                    continue

                delay = self.next_delay(response)
                status.record_success(
                    response, latency_ms=latency_ms, next_announce_at=time.time() + delay
                )
                new_peers = self._register_peers(response.peers)
                logger.info(
                    "announce to %s: %d peers (%d new), %d seeders, next in %.0fs",
                    tracker.host,
                    len(response.peers),
                    len(new_peers),
                    response.seeders,
                    delay,
                )
                return AnnounceOutcome(
                    tracker=tracker,
                    response=response,
                    peers=response.peers,
                    new_peers=new_peers,
                    latency_ms=latency_ms,
                    next_announce_in=delay,
                )

        reason = f"all trackers failed (last error: {last_error})" if last_error else "no trackers"
        if self.event_bus is not None:
            self.event_bus.emit(
                make_event(
                    EventType.TRACKER_FAILED,
                    message=reason,
                    level=logging.ERROR,
                    torrent_id=self.torrent.hex_info_hash,
                    data={"torrent": self.torrent.name},
                )
            )
        raise TrackerError(reason)

    def next_delay(self, response: AnnounceResponse) -> float:
        """Seconds until the next announce, honouring configured bounds.

        ``min interval`` takes precedence when the tracker sends it (BEP 3 says
        clients must not announce more often than that).
        """
        interval = float(response.interval)
        if response.min_interval:
            interval = max(interval, float(response.min_interval))
        interval *= self.config.announce_interval_multiplier
        return min(
            max(interval, float(self.config.min_announce_interval)),
            float(self.config.max_announce_interval),
        )

    def backoff_delay(self, tracker_url: str) -> float:
        """Exponential backoff for a failing tracker, capped by configuration."""
        failures = self._status[tracker_url].consecutive_failures
        # 2.0 ** n keeps this a float: int ** int is typed as Any in typeshed.
        delay = float(self.config.min_announce_interval) * (2.0 ** min(failures, 6))
        return min(delay, float(self.config.max_announce_interval))

    # ------------------------------------------------------------- scheduling

    async def run_periodic(
        self,
        state_provider: Callable[[], dict[str, int]],
        *,
        stop_event: asyncio.Event | None = None,
        on_peers: Callable[[AnnounceOutcome], None] | None = None,
    ) -> None:
        """Announce forever until cancelled or ``stop_event`` is set.

        Args:
            state_provider: Returns ``{"uploaded", "downloaded", "left"}`` at
                each announce, so the manager never caches stale progress.
            stop_event: Optional external stop signal.
            on_peers: Called with every successful outcome, so the caller can
                feed fresh peers into the swarm. Re-announcing is how a client
                hears about peers that arrived after the first announce, so
                dropping the answer would make the loop decorative. A callback
                that raises is logged, not fatal: the next announce is only
                ``interval`` away.
        """
        stop = stop_event or asyncio.Event()
        self._stop_event = stop
        while not stop.is_set():
            state = state_provider()
            try:
                outcome = await self.announce(
                    uploaded=state.get("uploaded", 0),
                    downloaded=state.get("downloaded", 0),
                    left=state.get("left"),
                )
                delay = outcome.next_announce_in
                if on_peers is not None:
                    self._notify(outcome, on_peers)
            except TrackerError:
                delay = (
                    self.backoff_delay(self.all_trackers[0].url)
                    if self.all_trackers
                    else (float(self.config.min_announce_interval))
                )
            # Cancellation is deliberately not caught: it must propagate so
            # stop_periodic() sees a cleanly cancelled task.
            await self._wait(delay, stop)

    def start_periodic(
        self,
        state_provider: Callable[[], dict[str, int]],
        *,
        on_peers: Callable[[AnnounceOutcome], None] | None = None,
    ) -> tuple[asyncio.Task[None], asyncio.Event]:
        """Start the announce loop in the background.

        Args:
            state_provider: Fresh progress for every announce.
            on_peers: Called with each successful outcome.

        Returns:
            ``(task, stop_event)``; set the event then await the task to stop.
        """
        stop_event = asyncio.Event()
        self._periodic_task = asyncio.create_task(
            self.run_periodic(state_provider, stop_event=stop_event, on_peers=on_peers),
            name=f"tracker-announce-{self.torrent.hex_info_hash[:8]}",
        )
        return self._periodic_task, stop_event

    @staticmethod
    def _notify(outcome: AnnounceOutcome, handler: Callable[[AnnounceOutcome], None]) -> None:
        """Hand an outcome to a callback that is not allowed to stop us."""
        try:
            handler(outcome)
        except Exception as exc:  # noqa: BLE001 - a subscriber must not break announcing
            logger.warning("peer callback failed: %s", exc)

    async def stop_periodic(self) -> None:
        """Stop the background announce loop, if running."""
        if self._periodic_task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._periodic_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._periodic_task
        self._periodic_task = None
        self._stop_event = None

    # -------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        """Stop announcing and close every tracker we own."""
        await self.stop_periodic()
        for tracker in self.all_trackers:
            await tracker.aclose()

    # -------------------------------------------------------------- internals

    def _register_peers(self, peers: Sequence[PeerAddress]) -> tuple[PeerAddress, ...]:
        """Record peers and return those we had not seen before."""
        new: list[PeerAddress] = []
        for peer in peers:
            if peer.address in self._seen_peers:
                continue
            self._seen_peers.add(peer.address)
            new.append(peer)
        return tuple(new)

    @staticmethod
    async def _wait(delay: float, stop: asyncio.Event) -> None:
        """Sleep for ``delay`` seconds, waking early if ``stop`` is set."""
        if delay <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)


def build_tracker_tiers(
    torrent: Torrent,
    *,
    config: TrackerConfig | None = None,
    event_bus: EventBus | None = None,
    session: aiohttp.ClientSession | None = None,
) -> tuple[tuple[Tracker, ...], ...]:
    """Build tracker tiers from a torrent's announce metadata.

    The primary ``announce`` URL becomes tier 0 when it is not already the first
    entry of ``announce-list``. Each URL is handed to
    :func:`~app.tracker.factory.build_tracker`, which picks the client from the
    scheme, so a torrent's tier can mix HTTP and UDP trackers freely. Schemes
    this client cannot speak are skipped with a warning rather than failing the
    torrent — an unsupported tracker is not a reason to refuse to open a file.

    Args:
        torrent: Source of the tracker URLs.
        config: Tracker settings (timeouts, peer caps).
        event_bus: Optional bus for tracker events.
        session: Optional shared HTTP session.

    Returns:
        Trackers grouped by tier. Empty when the torrent has no usable trackers.
    """
    settings = config or TrackerConfig()
    tiers: list[list[str]] = [list(tier) for tier in torrent.announce_list]

    if torrent.announce:
        urls_in_tiers = {url for tier in tiers for url in tier}
        if torrent.announce not in urls_in_tiers:
            tiers.insert(0, [torrent.announce])

    built: tuple[tuple[Tracker, ...], ...] = ()
    for tier in tiers:
        trackers: list[Tracker] = []
        for url in tier:
            try:
                trackers.append(
                    build_tracker(
                        url,
                        config=settings,
                        session=session,
                        event_bus=event_bus,
                    )
                )
            except UnsupportedTrackerError as exc:
                logger.warning("skipping tracker %s: %s", url, exc)
        if trackers:
            built += (tuple(trackers),)
    return built
