"""Aggregated metrics snapshots for the UI — and for anyone else who asks.

Every number in a :class:`MetricsSnapshot` comes from somewhere specific:

* **Rates are measured**, by :class:`~app.statistics.speed.SpeedMeter`, from
  byte-carrying events on the bus (`PIECE_BLOCK_RECEIVED`, `PIECE_UPLOADED`).
  Nothing is smoothed into existence and nothing is remembered once the bytes
  stop: an idle torrent reports 0 B/s, not the speed it used to have.
* **Progress is counted**, from the pieces that verified, not from blocks that
  arrived. A block that failed its hash never made the torrent faster.
* **Peers are the live connections**, as the peer manager holds them right
  now; there is no "estimated swarm size" anywhere in this module.

The collector never blocks and never raises at a caller: a source that has
gone away, or one that was never wired, reads as zero rather than as an
exception in a UI refresh.

**ETA is honest.** With no measured download rate there is no ETA — not
"unknown", not a guess from the file size, and certainly not infinity:
``eta_seconds`` is ``None`` until there is a rate to divide by.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import StatsConfig
from app.core.constants import (
    DEFAULT_INSTANT_WINDOW_SECONDS,
    DEFAULT_LONG_WINDOW_SECONDS,
    DEFAULT_STATS_WINDOW_SECONDS,
)
from app.core.event_bus import EventBus
from app.core.events import Event, EventType, make_event
from app.statistics.history import HistoryBook
from app.statistics.speed import SpeedMeter, SpeedRates
from app.torrent import Torrent

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

# Series the graphs draw. Named here so the UI and the collector agree on
# spelling without importing each other's string literals.
SERIES_DOWNLOAD_RATE = "download_rate"
SERIES_UPLOAD_RATE = "upload_rate"
SERIES_DOWNLOAD_INSTANT = "download_rate_instant"
SERIES_UPLOAD_INSTANT = "upload_rate_instant"
SERIES_PEERS = "peers"
SERIES_PROGRESS = "progress"
SERIES_ETA = "eta"

# Events that tell us bytes moved. Both carry ``length`` in their data.
_BYTE_EVENTS: dict[EventType, str] = {
    EventType.PIECE_BLOCK_RECEIVED: "download",
    EventType.PIECE_UPLOADED: "upload",
}


def _round(value: float, digits: int = 3) -> float:
    """Round for transport: a graph does not need more than three decimals."""
    return round(float(value), digits)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """One read of everything worth showing, at one moment.

    Attributes:
        timestamp: When the sample was taken, on the collector's clock.
        elapsed: Seconds since the collector started watching.
        download: Measured download rates and the bytes received.
        upload: Measured upload rates and the bytes sent.
        progress: Fraction of the torrent's bytes verified, ``[0.0, 1.0]``.
        total_bytes: Size of the torrent's payload.
        verified_bytes: Bytes belonging to verified pieces.
        remaining_bytes: Bytes still to fetch.
        pieces_total / pieces_verified / pieces_missing: The same story, in
            pieces, because a piece is what a peer can actually offer.
        wasted_bytes: Bytes that arrived and were thrown away.
        blocks_received: Blocks accepted into a piece being assembled.
        blocks_duplicate: Blocks that arrived twice (endgame, or a retry).
        peers_connected: Live peer connections.
        peers_candidates: Known peers not currently connected.
        peers_unchoked: Peers willing to serve us right now.
        peers_interesting: Peers that have told us they want our pieces.
        upload_queue_depth: Requests waiting to be served by us.
        requests_refused: Requests we refused, whatever the reason.
        complete: Whether every piece is verified.
        eta_seconds: Seconds until completion, or ``None`` when there is no
            rate to compute one from.
        share_ratio: Bytes sent divided by bytes received; ``None`` until
            something has been received.
    """

    timestamp: float = 0.0
    elapsed: float = 0.0
    download: SpeedRates = field(default_factory=SpeedRates)
    upload: SpeedRates = field(default_factory=SpeedRates)
    progress: float = 0.0
    total_bytes: int = 0
    verified_bytes: int = 0
    remaining_bytes: int = 0
    pieces_total: int = 0
    pieces_verified: int = 0
    pieces_missing: int = 0
    wasted_bytes: int = 0
    blocks_received: int = 0
    blocks_duplicate: int = 0
    peers_connected: int = 0
    peers_candidates: int = 0
    peers_unchoked: int = 0
    peers_interesting: int = 0
    upload_queue_depth: int = 0
    requests_refused: int = 0
    complete: bool = False
    eta_seconds: float | None = None
    share_ratio: float | None = None

    @property
    def state(self) -> str:
        """A plain word for what the torrent is doing."""
        if self.complete:
            return "seeding"
        if self.peers_connected == 0:
            return "waiting"
        if self.download.displayed > 0:
            return "downloading"
        return "stalled"

    @property
    def active(self) -> bool:
        """Whether bytes are moving in either direction."""
        return self.download.displayed > 0 or self.upload.displayed > 0

    @property
    def waste_ratio(self) -> float:
        """Wasted bytes as a fraction of everything received; ``0.0`` if none."""
        received = self.blocks_received
        if received <= 0:
            return 0.0
        return self.wasted_bytes / max(1, self.wasted_bytes + received)

    def as_dict(self) -> dict[str, Any]:
        """The snapshot as plain JSON-friendly values, for the bus and the UI."""
        return {
            "timestamp": _round(self.timestamp),
            "elapsed": _round(self.elapsed),
            "download_rate": _round(self.download.displayed, 1),
            "download_rate_instant": _round(self.download.instant, 1),
            "download_rate_long": _round(self.download.long, 1),
            "download_average": _round(self.download.average, 1),
            "upload_rate": _round(self.upload.displayed, 1),
            "upload_rate_instant": _round(self.upload.instant, 1),
            "upload_rate_long": _round(self.upload.long, 1),
            "upload_average": _round(self.upload.average, 1),
            "downloaded_bytes": self.download.total,
            "uploaded_bytes": self.upload.total,
            "progress": _round(self.progress, 5),
            "total_bytes": self.total_bytes,
            "verified_bytes": self.verified_bytes,
            "remaining_bytes": self.remaining_bytes,
            "pieces_total": self.pieces_total,
            "pieces_verified": self.pieces_verified,
            "pieces_missing": self.pieces_missing,
            "wasted_bytes": self.wasted_bytes,
            "blocks_received": self.blocks_received,
            "blocks_duplicate": self.blocks_duplicate,
            "peers_connected": self.peers_connected,
            "peers_candidates": self.peers_candidates,
            "peers_unchoked": self.peers_unchoked,
            "peers_interesting": self.peers_interesting,
            "upload_queue_depth": self.upload_queue_depth,
            "requests_refused": self.requests_refused,
            "complete": self.complete,
            "state": self.state,
            "eta_seconds": None if self.eta_seconds is None else _round(self.eta_seconds, 1),
            "share_ratio": None if self.share_ratio is None else _round(self.share_ratio, 4),
        }


class MetricsCollector:
    """Watches a torrent and says what it saw.

    Args:
        torrent: The torrent being measured; supplies sizes and piece counts.
        download: Optional download manager; supplies progress, pieces and
            download-side counters.
        upload: Optional upload manager; supplies upload counters, the queue
            and the ``have`` bitfield.
        peers: Optional peer manager; supplies connection counts.
        event_bus: Optional bus. When given, the collector counts bytes from
            ``PIECE_BLOCK_RECEIVED`` / ``PIECE_UPLOADED`` events and publishes
            a ``STATS_SAMPLE`` event on every sample.
        config: Sampling interval, window length and history size.
        clock: Time source; injectable so tests can run faster than real time.
        windows: Rate windows (see :class:`~app.statistics.speed.SpeedMeter`).

    Example:
        >>> collector = MetricsCollector(torrent, download=manager)  # doctest: +SKIP
        >>> snapshot = collector.sample()                             # doctest: +SKIP
        >>> snapshot.state                                            # doctest: +SKIP
        'waiting'
    """

    def __init__(
        self,
        torrent: Torrent,
        *,
        download: Any = None,
        upload: Any = None,
        peers: Any = None,
        event_bus: EventBus | None = None,
        config: StatsConfig | None = None,
        clock: Clock = time.monotonic,
        windows: tuple[float, ...] = (
            DEFAULT_INSTANT_WINDOW_SECONDS,
            DEFAULT_STATS_WINDOW_SECONDS,
            DEFAULT_LONG_WINDOW_SECONDS,
        ),
    ) -> None:
        self._torrent = torrent
        self._download = download
        self._upload = upload
        self._peers = peers
        self._bus = event_bus
        self._config = config or StatsConfig()
        self._clock = clock
        self._started_at = clock()
        self._download_speed = SpeedMeter(windows, clock=clock)
        self._upload_speed = SpeedMeter(windows, clock=clock)
        self._history = HistoryBook(self._config.history_samples, clock=clock)
        self._interval = self._config.sample_interval
        self._subscriptions: list[Any] = []
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._last: MetricsSnapshot | None = None
        if self._bus is not None:
            self._listen()

    # ------------------------------------------------------------------ wiring

    def _listen(self) -> None:
        """Count the bytes that the engines announce on the event bus."""
        assert self._bus is not None
        for event_type in _BYTE_EVENTS:
            self._subscriptions.append(self._bus.subscribe(event_type, self._on_event))

    def _on_event(self, event: Event) -> None:
        """Bus handler: a byte-carrying event is a byte that moved."""
        direction = _BYTE_EVENTS.get(event.type)
        if direction is None:
            return
        length = event.data.get("length")
        if not isinstance(length, int) or length <= 0:
            return
        if direction == "download":
            self._download_speed.add(length)
        else:
            self._upload_speed.add(length)

    def close(self) -> None:
        """Stop listening. Safe to call twice."""
        if self._bus is None:
            return
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []

    # ---------------------------------------------------------------- counting

    @property
    def torrent(self) -> Torrent:
        """The torrent being measured."""
        return self._torrent

    @property
    def config(self) -> StatsConfig:
        """Sampling settings in force."""
        return self._config

    @property
    def history(self) -> HistoryBook:
        """The recorded series, for graphs."""
        return self._history

    @property
    def download_speed(self) -> SpeedMeter:
        """The download rate meters."""
        return self._download_speed

    @property
    def upload_speed(self) -> SpeedMeter:
        """The upload rate meters."""
        return self._upload_speed

    @property
    def last(self) -> MetricsSnapshot | None:
        """The most recent sample, ``None`` before the first one."""
        return self._last

    @property
    def running(self) -> bool:
        """Whether the sampling loop is alive."""
        return self._task is not None and not self._task.done()

    def elapsed(self, *, now: float | None = None) -> float:
        """Seconds since this collector started watching."""
        stamp = self._clock() if now is None else now
        return max(0.0, stamp - self._started_at)

    def note_download(self, length: int, *, now: float | None = None) -> None:
        """Count ``length`` received bytes that did not come from the bus."""
        self._download_speed.add(length, now=now)

    def note_upload(self, length: int, *, now: float | None = None) -> None:
        """Count ``length`` sent bytes that did not come from the bus."""
        self._upload_speed.add(length, now=now)

    # --------------------------------------------------------------- sampling

    def snapshot(self, *, now: float | None = None) -> MetricsSnapshot:
        """Read the current metrics without recording them."""
        return self.sample(now=now, record=False)

    def sample(
        self,
        *,
        now: float | None = None,
        record: bool = True,
        emit: bool = True,
    ) -> MetricsSnapshot:
        """Take one sample.

        Args:
            now: Timestamp to use; the clock by default.
            record: Whether to append the rates to the history series.
            emit: Whether to publish a ``STATS_SAMPLE`` event.

        Returns:
            The snapshot, built from what the sources report right now.
        """
        stamp = self._clock() if now is None else now
        download = self._download_speed.rates(now=stamp)
        upload = self._upload_speed.rates(now=stamp)
        verified = self._verified_bytes()
        total = self._torrent.total_length
        progress = (verified / total) if total else 1.0
        pieces_verified, pieces_missing = self._piece_counts()
        pieces_total = self._torrent.piece_count
        # Derived from the pieces we can account for, and from nothing else:
        # asking the engine "are you done?" would answer "yes" for a download
        # that has not counted a single piece yet, which is a lie the UI would
        # have repeated.
        complete = bool(pieces_total > 0 and pieces_missing == 0 and progress >= 1.0)
        remaining = max(0, total - verified)

        # An ETA needs a rate, and a rate is a measurement: without one the
        # honest answer is "unknown", which is what ``None`` means here. The
        # session average is deliberately not used — a torrent that has
        # stalled is not "slowly finishing", it is waiting, and saying
        # "12 minutes" would be a guess dressed as a measurement.
        rate = self._eta_rate(download, now=stamp)
        if complete or remaining <= 0:
            eta: float | None = 0.0
        elif rate > 0:
            eta = remaining / rate
        else:
            eta = None
        share: float | None = upload.total / download.total if download.total > 0 else None

        snapshot = MetricsSnapshot(
            timestamp=stamp,
            elapsed=self.elapsed(now=stamp),
            download=download,
            upload=upload,
            progress=min(1.0, max(0.0, progress)),
            total_bytes=total,
            verified_bytes=verified,
            remaining_bytes=remaining,
            pieces_total=pieces_total,
            pieces_verified=pieces_verified,
            pieces_missing=pieces_missing,
            wasted_bytes=self._int_from(self._download, ("stats", "wasted_bytes")),
            blocks_received=self._int_from(self._download, ("stats", "blocks_received")),
            blocks_duplicate=self._int_from(self._download, ("stats", "blocks_duplicate")),
            peers_connected=self._peers_connected(),
            peers_candidates=self._peers_candidates(),
            peers_unchoked=self._peers_unchoked(),
            peers_interesting=self._peers_interesting(),
            upload_queue_depth=self._int_from(self._upload, ("stats", "queue_depth")),
            requests_refused=self._requests_refused(),
            complete=complete,
            eta_seconds=eta,
            share_ratio=share,
        )
        if record:
            self._record(snapshot)
        if emit:
            self._publish(snapshot)
        self._last = snapshot
        return snapshot

    # ------------------------------------------------------------------- loop

    async def start(self, *, interval: float | None = None) -> None:
        """Start sampling in the background."""
        if self._task is not None:
            return
        if interval is not None:
            self._interval = interval
        self._task = asyncio.create_task(self._loop(), name="metrics-collector")

    async def stop(self) -> None:
        """Stop sampling, after the sample in progress has finished.

        The loop is *told* to stop rather than simply cancelled, so a sample
        that is halfway through a read is not abandoned mid-number. It checks
        the flag between samples and its wait is bounded, so it comes back
        within one interval without needing to be forced.
        """
        task, self._task = self._task, None
        if task is None:
            return
        self._stopping = True
        self._wake.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - stopping must not fail
            logger.warning("metrics collector stopped with an error: %s", exc)
        finally:
            self._stopping = False

    async def _loop(self) -> None:
        """Sample on a schedule, and wake early when told to stop.

        One failed sample is logged and survived: a statistics loop that stops
        because a source was briefly unreadable is a graph that lies by going
        quiet.
        """
        while not self._stopping:
            try:
                self.sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must not die
                logger.warning("metrics sampling failed: %s", exc)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                continue
            self._wake.clear()

    # ---------------------------------------------------------------- helpers

    def _eta_rate(self, rates: SpeedRates, *, now: float) -> float:
        """The rate an ETA is allowed to divide by.

        The long window is the steady one, but until a full window of history
        exists it divides by seconds that have not happened yet and so
        under-reports: half a second into a download the 5 s window reads a
        fifth of the real speed. Until the window has filled, the instant rate
        is the only measurement that is actually about now.
        """
        windows = self._download_speed.windows
        short_window = windows[1] if len(windows) > 1 else windows[0]
        if self._download_speed.elapsed(now=now) >= short_window:
            return rates.short
        return rates.instant or rates.short

    def _record(self, snapshot: MetricsSnapshot) -> None:
        """Append the interesting numbers to their series."""
        stamp = snapshot.timestamp
        self._history.record(SERIES_DOWNLOAD_RATE, snapshot.download.displayed, now=stamp)
        self._history.record(SERIES_UPLOAD_RATE, snapshot.upload.displayed, now=stamp)
        self._history.record(SERIES_DOWNLOAD_INSTANT, snapshot.download.instant, now=stamp)
        self._history.record(SERIES_UPLOAD_INSTANT, snapshot.upload.instant, now=stamp)
        self._history.record(SERIES_PEERS, float(snapshot.peers_connected), now=stamp)
        self._history.record(SERIES_PROGRESS, snapshot.progress, now=stamp)
        if snapshot.eta_seconds is not None:
            self._history.record(SERIES_ETA, snapshot.eta_seconds, now=stamp)

    def _publish(self, snapshot: MetricsSnapshot) -> None:
        """Announce the sample, for the UI bridge and the logs."""
        if self._bus is None:
            return
        self._bus.emit(
            make_event(
                EventType.STATS_SAMPLE,
                message=(
                    f"{snapshot.state}: {snapshot.progress:.1%}, "
                    f"{snapshot.download.displayed / 1024:.0f} KiB/s down"
                ),
                torrent_id=self._torrent.hex_info_hash,
                level=logging.DEBUG,
                data=snapshot.as_dict(),
            )
        )

    def _verified_bytes(self) -> int:
        """Bytes belonging to verified pieces, by piece geometry."""
        indexes = self._verified_indexes()
        if indexes is None:
            return 0
        return sum(self._torrent.piece_size(index) for index in indexes)

    def _verified_indexes(self) -> tuple[int, ...] | None:
        """Indexes of the pieces we hold, or ``None`` if nothing says.

        The download engine knows which pieces verified; a seeding-only setup
        only has the ``have`` bitfield. When neither is wired there is no
        answer, and guessing one would be the lie this module exists to avoid.
        """
        pieces = getattr(self._download, "verified_pieces", None)
        if callable(pieces):
            pieces = pieces()
        if pieces:
            return tuple(int(index) for index in pieces)
        have = getattr(self._upload, "have", None)
        if have is not None and hasattr(have, "has"):
            return tuple(index for index in range(self._torrent.piece_count) if have.has(index))
        return None

    def _piece_counts(self) -> tuple[int, int]:
        """(verified, missing) piece counts, honest when nothing says."""
        indexes = self._verified_indexes()
        if indexes is None:
            return 0, self._torrent.piece_count
        verified = len(indexes)
        return verified, max(0, self._torrent.piece_count - verified)

    def _connections(self) -> tuple[Any, ...]:
        """Live peer connections, however the peer manager spells it."""
        if self._peers is None:
            return ()
        connections = getattr(self._peers, "connections", ())
        return tuple(connection for connection in connections if _is_connected(connection))

    def _peers_connected(self) -> int:
        return len(self._connections())

    def _peers_candidates(self) -> int:
        """Peers we know about but are not talking to."""
        if self._peers is None:
            return 0
        stats = getattr(self._peers, "stats", None)
        candidates = getattr(stats, "candidates", None)
        if isinstance(candidates, int):
            return candidates
        known = getattr(self._peers, "candidates", ())
        try:
            return len(tuple(known))
        except TypeError:
            return 0

    def _peers_unchoked(self) -> int:
        """Peers willing to serve us: the ones that matter for a rate."""
        if self._peers is None:
            return 0
        unchoked = getattr(self._peers, "unchoked_peers", None)
        if callable(unchoked):
            try:
                return len(tuple(unchoked()))
            except TypeError:
                return 0
        return sum(1 for connection in self._connections() if not _is_choked(connection))

    def _peers_interesting(self) -> int:
        """Peers that have told us they want our pieces."""
        return sum(
            1
            for connection in self._connections()
            if bool(getattr(getattr(connection, "session", None), "peer_interested", False))
        )

    def _requests_refused(self) -> int:
        """Requests we refused, whatever the reason."""
        rejected = getattr(getattr(self._upload, "stats", None), "requests_rejected", None)
        if isinstance(rejected, dict):
            return int(sum(rejected.values()))
        return 0

    def _int_from(self, source: Any, path: tuple[str, ...]) -> int:
        """Read a counter off a source that may not be there after all."""
        if source is None:
            return 0
        value: Any = source
        for attribute in path:
            value = getattr(value, attribute, None)
            if value is None:
                return 0
        return int(value) if isinstance(value, (int, float)) else 0


def _is_connected(connection: Any) -> bool:
    return bool(getattr(connection, "connected", True))


def _is_choked(connection: Any) -> bool:
    choked = getattr(connection, "choked", None)
    if choked is not None:
        return bool(choked)
    session = getattr(connection, "session", None)
    return bool(getattr(session, "peer_choking", True))
