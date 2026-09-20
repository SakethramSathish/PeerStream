"""Torrent engine: one torrent, every moving part, one start and one stop.

Below this module the client is a set of subsystems that know nothing about each
other: a tracker manager that finds peers, a peer manager that keeps sockets, a
download engine that assembles pieces, an upload engine that serves them,
storage that owns the disk, and a collector that measures the lot.

This module is where they become a client, and it is deliberately the *only*
place that knows the wiring:

* the peer manager's ``on_block`` goes to the download engine,
* its ``on_request`` goes to the upload engine,
* its ``on_disconnect`` goes to **both** — which is why the engine owns that
  callback instead of letting either engine claim it,
* a piece that verifies is announced to the upload engine, because a peer
  cannot ask for a piece nobody told it about,
* and every tracker answer is fed back into the swarm, not just the first one.

Everything is injected, so a test can hand the engine any of these parts it
likes — including stand-ins — and check the wiring rather than the network.

The engine also owns one ordering rule that no subsystem can enforce for
itself: **progress on disk is adopted before the download engine is built**,
because the download engine decides what is missing at construction time. That
is why :func:`build_engine` is a coroutine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from app.core.config import Config, StorageConfig
from app.core.event_bus import EventBus
from app.core.events import Event, EventType, make_event
from app.core.peer_id import generate_peer_id
from app.download.manager import DownloadManager
from app.peer.bitfield import Bitfield
from app.peer.connection import SwarmContext
from app.peer.discovery.listener import PeerListener
from app.peer.discovery.peer_manager import PeerManager
from app.peer.messages import Cancel, Request
from app.statistics.metrics import MetricsCollector, MetricsSnapshot
from app.storage.manager import StorageManager
from app.torrent import Torrent
from app.tracker.base import PeerAddress, TrackerEvent
from app.tracker.manager import AnnounceOutcome, TrackerManager
from app.upload.manager import UploadManager

logger = logging.getLogger(__name__)

# How long ``wait_until_complete()`` waits when the caller does not say. Long,
# because a torrent is allowed to take hours, and finite, because a deadlock
# must be reported rather than waited out.
DEFAULT_WAIT_TIMEOUT: Final[float] = 3600.0


class TorrentState(StrEnum):
    """What one torrent is doing, in one word.

    The UI shows this and the tests assert on it. It is derived from the
    engines every time it is asked for, so it cannot drift out of step with
    the thing it describes.
    """

    IDLE = "idle"
    STARTING = "starting"
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ResumeSummary:
    """What a previous run left on disk.

    Attributes:
        pieces: Pieces adopted from disk and re-verified.
        uploaded: Bytes this torrent served before this process existed. Not
            part of any rate — those are measured — but part of what the swarm
            is owed, so it is reported to trackers.
        saved_at: When the state file was written.
    """

    pieces: int = 0
    uploaded: int = 0
    saved_at: float = 0.0

    @property
    def empty(self) -> bool:
        """Whether there was anything to resume."""
        return self.pieces == 0 and self.uploaded == 0


class Engine:
    """Drives one torrent from start to finish, and then keeps seeding it.

    Args:
        torrent: The torrent to run.
        storage: Where verified pieces are written and read back.
        peers: The peer manager that owns the connections.
        download: The download engine.
        upload: The upload engine.
        metrics: The collector measuring the lot.
        tracker: Announces and returns peers; optional, so a swarm can be
            assembled by hand (tests, DHT-only, private swarms).
        listener: Optional inbound listener; omit for a leech-only client.
        event_bus: Optional bus; events are dropped when it is omitted.
        resumed: What a previous run left on disk.

    Example:
        >>> engine = Engine(torrent, storage=storage, peers=peers, download=download,  # doctest: +SKIP
        ...                 upload=upload, metrics=metrics)
        >>> await engine.start()                                                       # doctest: +SKIP
        >>> await engine.wait_until_complete(timeout=60.0)                             # doctest: +SKIP
        >>> await engine.stop()                                                        # doctest: +SKIP
    """

    def __init__(
        self,
        torrent: Torrent,
        *,
        storage: StorageManager,
        peers: PeerManager,
        download: DownloadManager,
        upload: UploadManager,
        metrics: MetricsCollector,
        tracker: TrackerManager | None = None,
        listener: PeerListener | None = None,
        event_bus: EventBus | None = None,
        resumed: ResumeSummary | None = None,
    ) -> None:
        self._torrent = torrent
        self._storage = storage
        self._peers = peers
        self._download = download
        self._upload = upload
        self._metrics = metrics
        self._tracker = tracker
        self._listener = listener
        self._bus = event_bus
        self._resumed = resumed or ResumeSummary()

        self._running = False
        self._paused = False
        self._started_once = False
        self._stopped = False
        self._error: str | None = None
        self._port: int = 0
        self._tracker_task: asyncio.Task[None] | None = None
        self._tracker_stop: asyncio.Event | None = None
        self._peers_task: asyncio.Task[None] | None = None
        self._subscriptions: list[Any] = []

    # ------------------------------------------------------------------ access

    @property
    def torrent(self) -> Torrent:
        """The torrent being run."""
        return self._torrent

    @property
    def info_hash(self) -> bytes:
        """The torrent's identity on the wire."""
        return self._torrent.info_hash

    @property
    def hex_info_hash(self) -> str:
        """The torrent's identity as text, for logs and state files."""
        return self._torrent.hex_info_hash

    @property
    def storage(self) -> StorageManager:
        return self._storage

    @property
    def peers(self) -> PeerManager:
        return self._peers

    @property
    def download(self) -> DownloadManager:
        return self._download

    @property
    def upload(self) -> UploadManager:
        return self._upload

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    @property
    def tracker(self) -> TrackerManager | None:
        return self._tracker

    @property
    def listener(self) -> PeerListener | None:
        return self._listener

    @property
    def port(self) -> int:
        """The port we accept connections on; ``0`` when we do not listen."""
        return self._port

    @property
    def running(self) -> bool:
        """Whether the engine is up. A paused engine is still up."""
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def error(self) -> str | None:
        """Why this torrent is not running, when it is not."""
        return self._error

    @property
    def complete(self) -> bool:
        """Whether every piece is verified and on disk."""
        return bool(self._download.complete)

    @property
    def progress(self) -> float:
        """Fraction of the torrent's bytes verified, ``[0.0, 1.0]``."""
        return float(self._download.progress)

    @property
    def resumed(self) -> ResumeSummary:
        """What was adopted from disk before this run started."""
        return self._resumed

    @property
    def state(self) -> TorrentState:
        """What the torrent is doing, derived from the engines themselves."""
        if self._error is not None:
            return TorrentState.ERROR
        if self._paused:
            return TorrentState.PAUSED
        if not self._running:
            return TorrentState.STOPPED if self._started_once else TorrentState.IDLE
        if self.complete:
            return TorrentState.SEEDING
        if not self._moved:
            return TorrentState.STARTING
        return TorrentState.DOWNLOADING

    @property
    def _moved(self) -> bool:
        """Whether a single byte has crossed the wire in this run.

        "Starting" is not a timer; it is the honest answer to "has anything
        happened yet", and it is counted, not guessed.
        """
        return bool(self._download.stats.blocks_received or self._upload.stats.blocks_served)

    def snapshot(self) -> MetricsSnapshot:
        """One honest read of everything worth showing."""
        self._sync_have()
        return self._metrics.snapshot()

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Bring the torrent up: disk, swarm, engines, measurement.

        Safe to call twice; starting a paused engine resumes it.
        """
        if self._running and not self._paused:
            return
        self._paused = False
        self._stopped = False
        self._error = None
        if not self._started_once:
            try:
                await self._prepare()
            except Exception as exc:
                self._fail(f"could not start: {exc}")
                raise
            self._wire()
            self._started_once = True

        self._running = True
        await self._start_tasks()
        if not self._resumed.empty:
            self._emit(
                EventType.RESUME_LOADED,
                f"resumed {self._resumed.pieces} piece(s) from disk",
                data={"pieces": self._resumed.pieces, "uploaded": self._resumed.uploaded},
            )
        self._emit(
            EventType.TORRENT_STARTED,
            f"{self._torrent.name}: started ({self._torrent.piece_count} pieces)",
            data={"pieces": self._torrent.piece_count, "port": self._port},
        )

    async def stop(self) -> None:
        """Stop everything, in the order that loses the least.

        Safe to call twice, and safe to call on an engine that never started.
        Stopping is reversible: the wiring and the subscriptions survive, so
        :meth:`start` can bring the torrent back up.
        """
        if self._stopped:
            return
        self._stopped = True
        self._paused = False
        self._running = False
        await self._stop_tasks()
        if self._started_once:
            await self._save_progress()
        if self._started_once:
            self._emit(
                EventType.TORRENT_STOPPED,
                f"{self._torrent.name}: stopped",
                data={"progress": round(self.progress, 4)},
            )

    async def aclose(self) -> None:
        """Stop the engine and release everything it owns: disk and trackers.

        The engine's HTTP sessions live as long as the engine does, and not a
        moment longer — a tracker left open is a socket the OS keeps for us.
        """
        await self.stop()
        self._unsubscribe()
        await self._storage.aclose()
        if self._tracker is not None:
            await self._tracker.aclose()

    async def pause(self) -> None:
        """Stop transferring without letting go of the torrent."""
        if not self._running or self._paused:
            return
        self._paused = True
        await self._stop_tasks()
        await self._save_progress()
        self._emit(EventType.TORRENT_PAUSED, f"{self._torrent.name}: paused")

    async def resume(self) -> None:
        """Continue a paused torrent."""
        await self.start()

    async def wait_until_complete(self, timeout: float | None = None) -> bool:
        """Wait for every piece to verify. Returns ``False`` on timeout."""
        return await self._download.wait_until_complete(
            timeout=DEFAULT_WAIT_TIMEOUT if timeout is None else timeout
        )

    async def __aenter__(self) -> Engine:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ----------------------------------------------------------------- actions

    async def announce(self, event: TrackerEvent | None = TrackerEvent.STARTED) -> int:
        """Announce now, and feed whatever peers come back into the swarm.

        Returns:
            How many of the returned peers were new to us.
        """
        if self._tracker is None:
            return 0
        state = self._tracker_state()
        outcome = await self._tracker.announce(
            uploaded=state["uploaded"],
            downloaded=state["downloaded"],
            left=state["left"],
            event=event,
        )
        return self._adopt_peers(outcome)

    def add_peers(
        self, addresses: list[PeerAddress] | tuple[PeerAddress, ...], *, source: str
    ) -> int:
        """Add peers from anywhere: DHT, PEX, a magnet link, or a test."""
        return int(self._peers.add_peers(list(addresses), source=source))

    # ----------------------------------------------------------------- wiring

    async def _prepare(self) -> None:
        """Make sure the files exist, then tell the upload side what we hold."""
        await self._storage.prepare()
        self._sync_have()

    def _wire(self) -> None:
        """Point every callback at the engine that should receive it."""
        self._peers.on_block = self._download.on_block
        self._peers.on_have = self._download.on_have
        # Both engines care when a peer leaves: the downloader must re-plan,
        # the uploader must drop its queue. One callback, two owners.
        self._peers.on_disconnect = self._on_disconnect
        self._peers.on_request = self._upload.on_request
        self._peers.on_cancel = self._on_cancel
        if self._bus is not None:
            self._subscriptions.append(
                self._bus.subscribe(EventType.PIECE_VERIFIED, self._on_piece_verified)
            )

    def _on_disconnect(self, peer: Any, reason: str = "") -> None:
        """One peer leaving is news to both engines."""
        self._download.on_disconnect(peer, reason)
        self._upload.on_disconnect(peer, reason)

    def _on_cancel(self, peer: Any, message: Cancel) -> None:
        """A cancel carries the same fields as a request; only the name differs."""
        self._upload.on_cancel(peer, cast("Request", message))

    def _on_piece_verified(self, event: Event) -> None:
        """A piece we can prove we have is a piece we can offer."""
        index = event.data.get("index")
        if isinstance(index, int):
            self._upload.note_piece_verified(index)

    def _sync_have(self) -> None:
        """Tell the upload side about pieces that verified since the last look.

        The event bus is the fast path, but an engine built without one must
        still be able to seed — and a piece that verified while nobody was
        looking is exactly the piece a leecher is waiting for.
        """
        for index in self._download.verified_pieces:
            if not self._upload.have.has(index):
                self._upload.note_piece_verified(index)

    def _adopt_peers(self, outcome: AnnounceOutcome) -> int:
        """Feed tracker peers into the swarm. Returns how many were new."""
        if not outcome.peers:
            return 0
        return self.add_peers(outcome.peers, source="tracker")

    def _adopt(self, outcome: AnnounceOutcome) -> None:
        """Tracker callback: the count is none of the announce loop's business."""
        self._adopt_peers(outcome)

    def _tracker_state(self) -> dict[str, int]:
        """Progress for the next announce: measured, not remembered."""
        verified = int(self._storage.downloaded_bytes)
        return {
            "uploaded": self._resumed.uploaded + int(self._upload.stats.bytes_uploaded),
            "downloaded": verified,
            "left": max(0, self._torrent.total_length - verified),
        }

    # ------------------------------------------------------------------ tasks

    async def _start_tasks(self) -> None:
        if self._listener is not None and self._port == 0:
            self._port = await self._listener.start()
            if self._tracker is not None:
                # Announce the port we actually got, not the one we asked for.
                self._tracker.port = self._port
            # Same reason: in a local swarm a tracker or a pex message hands our
            # own listener back to us, and dialling ourselves costs a slot.
            self._peers.our_port = self._port
        if self._peers_task is None:
            self._peers_task = self._peers.start()
        if self._tracker is not None and self._tracker_task is None:
            self._tracker_task, self._tracker_stop = self._tracker.start_periodic(
                self._tracker_state, on_peers=self._adopt
            )
        await self._download.start()
        await self._upload.start()
        await self._metrics.start()

    async def _stop_tasks(self) -> None:
        """Stop in the order that never leaves a socket wondering."""
        with contextlib.suppress(Exception):
            await self._metrics.stop()
        with contextlib.suppress(Exception):
            await self._download.stop()
        with contextlib.suppress(Exception):
            await self._upload.stop()
        if self._tracker_task is not None:
            if self._tracker_stop is not None:
                self._tracker_stop.set()
            self._tracker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._tracker_task
            self._tracker_task = None
            self._tracker_stop = None
        if self._peers_task is not None:
            await self._peers.stop()
            self._peers_task = None
        if self._listener is not None:
            await self._listener.stop()
            self._port = 0

    async def _save_progress(self) -> None:
        """Persist what we have, so the next run starts from here."""
        with contextlib.suppress(Exception):  # a read-only disk must not stop us
            await self._storage.save_resume(uploaded=self._tracker_state()["uploaded"])

    def _unsubscribe(self) -> None:
        for subscription in self._subscriptions:
            if self._bus is not None:
                self._bus.unsubscribe(subscription)
        self._subscriptions = []

    # ----------------------------------------------------------------- events

    def _fail(self, reason: str) -> None:
        """Record why this torrent is not running, and say so out loud."""
        self._error = reason
        self._running = False
        self._emit(EventType.TORRENT_STOPPED, reason, level=logging.ERROR)

    def _emit(
        self,
        event_type: EventType,
        message: str,
        *,
        level: int = logging.INFO,
        data: dict[str, object] | None = None,
    ) -> None:
        if self._bus is None:
            return
        self._bus.emit(
            make_event(
                event_type,
                message=message,
                torrent_id=self._torrent.hex_info_hash,
                level=level,
                data=data,
            )
        )


async def build_engine(
    torrent: Torrent,
    *,
    download_directory: Path | str,
    config: Config | None = None,
    event_bus: EventBus | None = None,
    trackers: Any = None,
    peer_id: bytes | None = None,
    listen: bool = True,
    host: str = "0.0.0.0",
    port: int = 0,
    resume: bool = True,
    session: Any = None,
) -> Engine:
    """Assemble a real engine for one torrent, with every dependency injected.

    This is the factory the services and the CLI use, and the only place that
    knows what a working engine is made of. It is a coroutine for one reason:
    the download engine decides what is missing when it is *constructed*, so
    progress on disk has to be adopted before that happens.

    Args:
        torrent: The torrent to run.
        download_directory: Where the data goes.
        config: Application configuration; defaults are used when omitted.
        event_bus: Optional bus every subsystem publishes to.
        trackers: Explicit tracker tiers; derived from the torrent when omitted.
        peer_id: Our peer id; generated when omitted.
        listen: Whether to accept inbound connections (seeding needs it).
        host: Interface to listen on.
        port: Port to listen on; ``0`` picks a free one.
        resume: Whether to adopt (and re-verify) progress left by a past run.
        session: Shared HTTP session for the trackers.

    Returns:
        An engine that has not been started.
    """
    settings = config or Config()
    directory = Path(download_directory)
    storage = StorageManager(
        torrent,
        directory,
        config=_storage_settings(settings, directory),
        event_bus=event_bus,
    )
    await storage.prepare()

    resumed = ResumeSummary()
    if resume:
        state = await storage.load_resume(verify=True)
        if state is not None:
            resumed = ResumeSummary(
                pieces=len(state.completed_pieces),
                uploaded=int(getattr(state, "uploaded", 0) or 0),
                saved_at=float(getattr(state, "saved_at", 0.0) or 0.0),
            )

    context = SwarmContext.from_torrent(torrent)
    peers = PeerManager(
        context,
        peer_id=peer_id or generate_peer_id(),
        config=settings.network,
        event_bus=event_bus,
        our_port=port,
    )
    tracker = TrackerManager(
        torrent,
        trackers=trackers or (),
        config=settings.tracker,
        peer_id=peer_id or peers.peer_id,
        port=port,
        event_bus=event_bus,
        session=session,
    )
    download = DownloadManager(
        torrent,
        storage=storage,
        peers=peers,
        config=settings.download,
        network=settings.network,
        event_bus=event_bus,
    )
    upload = UploadManager(
        torrent,
        storage=storage,
        peers=peers,
        config=settings.upload,
        event_bus=event_bus,
    )
    metrics = MetricsCollector(
        torrent,
        download=download,
        upload=upload,
        peers=peers,
        event_bus=event_bus,
        config=settings.stats,
    )
    listener = PeerListener(peers, host=host, port=port) if listen else None

    return Engine(
        torrent,
        storage=storage,
        peers=peers,
        download=download,
        upload=upload,
        metrics=metrics,
        tracker=tracker,
        listener=listener,
        event_bus=event_bus,
        resumed=resumed,
    )


def _storage_settings(config: Config, directory: Path) -> StorageConfig:
    """Storage settings for one torrent, keeping the shared state directory."""
    settings = config.storage
    return StorageConfig(
        download_directory=directory,
        state_directory=settings.state_directory,
        preallocate_files=settings.preallocate_files,
        verify_before_write=settings.verify_before_write,
        resume_autosave_seconds=settings.resume_autosave_seconds,
    )


def build_have(torrent: Torrent, indexes: tuple[int, ...] = ()) -> Bitfield:
    """A bitfield of the pieces we hold, empty by default.

    Claiming a piece we do not have would be a lie to the swarm, so the safe
    default is the one that says nothing.
    """
    have = Bitfield(torrent.piece_count)
    for index in indexes:
        have.set(index)
    return have
