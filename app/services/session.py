"""A session: the shared bits that outlive any single torrent.

One client owns one session. The session owns the things every torrent has in
common — the configuration, the event bus, which network interfaces to listen
on — and it owns the set of torrents currently known. It is the layer that
makes "add a torrent" a one-liner for the UI and "shut everything down cleanly"
a single ``aclose()``.

The session is deliberately *not* a torrent manager: it holds engines, and each
engine manages itself. What the session adds is the cross-torrent view — how
much bandwidth the whole client is using, and the guarantee that nothing is
left running when the session closes.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Config
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.core.peer_id import generate_peer_id
from app.discovery.dht.node import DhtNode
from app.discovery.dht_announcer import DhtAnnouncer
from app.discovery.magnet_resolver import MagnetResolution, MagnetResolver
from app.services.engine import Engine, TorrentState, build_engine
from app.services.torrent_service import (
    FileView,
    PeerView,
    PieceMap,
    TorrentService,
)
from app.torrent import Torrent
from app.torrent.magnet import MagnetUri, parse_magnet
from app.tracker.base import PeerAddress, Tracker, TrackerStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionTotals:
    """Bandwidth across every torrent in the session. All of it measured."""

    torrents: int
    active: int
    download_rate: float
    upload_rate: float
    downloaded_bytes: int
    uploaded_bytes: int

    def as_dict(self) -> dict[str, object]:
        """A plain, JSON-friendly view."""
        return {
            "torrents": self.torrents,
            "active": self.active,
            "download_rate": round(self.download_rate, 3),
            "upload_rate": round(self.upload_rate, 3),
            "downloaded_bytes": self.downloaded_bytes,
            "uploaded_bytes": self.uploaded_bytes,
        }


class Session:
    """Shared configuration, bus, and set of torrents.

    Args:
        config: The client's configuration.
        event_bus: Optional bus; one is created if not supplied, because a
            session without one cannot tell the UI anything.
        download_directory: Overrides ``config.storage.download_directory``.

    Example:
        >>> async with Session() as s:                 # doctest: +SKIP
        ...     s.add_torrent(torrent)                 # doctest: +SKIP
        ...     await s.start_all()                    # doctest: +SKIP
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        event_bus: EventBus | None = None,
        download_directory: str | Path | None = None,
    ) -> None:
        self._config = config or Config()
        if download_directory is not None:
            directory = Path(download_directory).expanduser().resolve()
            # Config paths are stored as strings; a Path here would be rejected.
            self._config = self._config.with_overrides(
                storage={"download_directory": str(directory)}
            )
        self._bus = event_bus or EventBus()
        self._owns_bus = event_bus is None
        self._services: dict[str, TorrentService] = {}
        self._dht: DhtNode | None = None
        self._peer_id = generate_peer_id()
        self._resolver = MagnetResolver(peer_id=self._peer_id, config=self._config)
        # Nodeless on purpose: torrents are added before the DHT is known to
        # bind, and an announcer with no node simply announces nothing.
        self._announcer = DhtAnnouncer(
            interval=self._config.dht.announce_interval, event_bus=self._bus
        )

    # ------------------------------------------------------------ properties

    @property
    def config(self) -> Config:
        return self._config

    @property
    def peer_id(self) -> bytes:
        """Our peer id: one per session, shared with every torrent.

        Trackers and peers remember us by this id, so a fresh one per torrent
        would look like several clients behind one address.
        """
        return self._peer_id

    @property
    def event_bus(self) -> EventBus:
        return self._bus

    @property
    def services(self) -> tuple[TorrentService, ...]:
        """Every torrent in the session, in the order they were added."""
        return tuple(self._services.values())

    @property
    def engines(self) -> tuple[Engine, ...]:
        return tuple(service.engine for service in self._services.values())

    def __len__(self) -> int:
        return len(self._services)

    # --------------------------------------------------------------- torrents

    async def add_torrent(
        self,
        torrent: Torrent,
        *,
        start: bool = True,
        directory: str | Path | None = None,
        listen: bool = True,
        resume: bool = True,
        trackers: Sequence[Sequence[Tracker]] | None = None,
    ) -> Engine:
        """Build an engine for ``torrent`` and register it with the session.

        Args:
            torrent: The torrent to run.
            start: Whether to start transferring immediately.
            directory: Where the data goes; the session's directory by default.
            listen: Whether to accept inbound connections (seeding needs it).
            resume: Whether to adopt (and re-verify) progress from a past run.
            trackers: Explicit tracker tiers; the torrent's own are used when
                omitted. Each tier is tried before the next.

        Returns:
            The engine, so callers that want to drive it directly can.

        Raises:
            ValueError: If this torrent is already in the session.
        """
        hex_info_hash = torrent.hex_info_hash
        if hex_info_hash in self._services:
            raise ValueError(f"torrent is already in this session: {hex_info_hash}")
        where = (
            Path(directory).resolve()
            if directory is not None
            else self._config.storage.download_directory
        )
        engine = await build_engine(
            torrent,
            download_directory=where,
            config=self._config,
            event_bus=self._bus,
            trackers=trackers,
            listen=listen,
            resume=resume,
        )
        self._services[hex_info_hash] = TorrentService(engine, event_bus=self._bus)
        self._emit(
            EventType.TORRENT_ADDED,
            f"added {torrent.name}",
            hex_info_hash,
            {"piece_count": torrent.piece_count, "length": torrent.total_length},
        )
        if start:
            await engine.start()
        # Registered last, so the first announce pass reads a port that is
        # actually bound. A torrent added paused registers too, and the announcer
        # retries it until it is listening rather than forgetting it.
        self._publish(engine)
        return engine

    async def add_magnet(
        self,
        magnet: MagnetUri | str,
        *,
        start: bool = True,
        directory: str | Path | None = None,
        listen: bool = True,
        resume: bool = True,
        peers: Sequence[PeerAddress] = (),
    ) -> tuple[Engine, MagnetResolution]:
        """Resolve a magnet link and add the torrent it describes.

        The torrent does not exist until a peer hands over its info dictionary,
        so this is the one "add" that can fail for reasons outside our control:
        nobody answered, or the bytes they sent were not the torrent the magnet
        named. Both raise :class:`~app.peer.errors.MetadataError` and neither
        leaves anything behind.

        Args:
            magnet: The link, or the URI text; parsed when given as a string.
            start: Whether to start downloading once the metadata is in.
            directory: Where the data goes.
            listen: Whether to accept inbound connections.
            resume: Whether to adopt progress from a past run.
            peers: Addresses to try in addition to the magnet's own hints.

        Returns:
            The engine, and the resolution it came from — the latter because a
            caller may want to know which peer supplied the metadata and what
            the magnet's name was.

        Raises:
            MetadataError: No peer supplied metadata matching the info hash.
            ValueError: The torrent is already in the session.
        """
        uri = parse_magnet(magnet) if isinstance(magnet, str) else magnet
        hex_info_hash = uri.hex_info_hash
        if hex_info_hash in self._services:
            raise ValueError(f"torrent is already in this session: {hex_info_hash}")

        self._emit(
            EventType.DHT_QUERY,
            f"resolving magnet for {uri.name}",
            hex_info_hash,
            {"trackers": len(uri.trackers), "peers": len(uri.peers)},
        )
        resolution = await self._resolver.resolve(uri, peers=peers)
        self._emit(
            EventType.TORRENT_ADDED,
            f"metadata for {resolution.torrent.name} arrived from {resolution.metadata.address}",
            hex_info_hash,
            {
                "bytes": resolution.metadata.size,
                "peers": len(resolution.peers),
                "sources": ",".join(resolution.sources),
                "private": resolution.torrent.private,
                "elapsed": round(resolution.elapsed, 3),
            },
        )
        engine = await self.add_torrent(
            resolution.torrent,
            start=start,
            directory=directory,
            listen=listen,
            resume=resume,
        )
        if resolution.peers:
            # We met these peers while looking for the metadata; they are the
            # only swarm we know of, and a magnet has no tracker to ask instead.
            engine.add_peers(list(resolution.peers), source="magnet")
        return engine, resolution

    # -------------------------------------------------------------------- dht

    @property
    def dht(self) -> DhtNode | None:
        """The DHT node, when one is running."""
        return self._dht

    @property
    def announcer(self) -> DhtAnnouncer:
        """What we have published to the DHT, and how the last pass went.

        Always present, even with no node: an empty registry is a true answer to
        "are we findable?", and a panel that had to handle None would guess.
        """
        return self._announcer

    def _publish(self, engine: Engine) -> None:
        """Offer a torrent to the DHT announce loop.

        Two things decide whether it is really published, and both are measured
        later rather than assumed now: a private torrent is refused outright
        (BEP 27 means trackers only), and one that is not listening has no port
        to hand out, so the announcer skips it and says so.
        """
        torrent = engine.torrent
        self._announcer.register(
            torrent.info_hash, lambda: engine.port, private=bool(torrent.private)
        )
        self._announcer.poke()

    async def start_dht(self) -> DhtNode | None:
        """Start the DHT node, if the configuration asks for one.

        Bootstrapping talks to three well-known routers over UDP. On a network
        where that is blocked the node still starts — it simply knows nobody —
        and magnet resolution falls back to the link's own trackers and peers.

        Returns:
            The node, or None when DHT is disabled in the configuration.
        """
        if not self._config.dht.enabled:
            return None
        if self._dht is not None and self._dht.bound:
            return self._dht

        node = DhtNode(
            host="0.0.0.0",
            port=self._config.dht.port,
            bootstrap_nodes=self._config.dht.bootstrap_nodes,
        )
        try:
            await node.start()
        except Exception as error:  # noqa: BLE001 - a DHT that will not start is not fatal
            self._emit(EventType.DHT_FAILED, f"DHT did not start: {error}", level=logging.WARNING)
            logger.warning("DHT did not start: %s", error)
            await node.aclose()
            return None

        self._dht = node
        self._resolver.dht = node
        self._announcer.attach(node)
        self._announcer.start()
        self._emit(
            EventType.DHT_STARTED,
            f"DHT listening on {node.address[1]}, {node.size} nodes known",
            data={"node_id": node.hex_id, "port": node.address[1]},
        )
        return node

    async def stop_dht(self) -> None:
        """Close the DHT node, if one is running. Safe to call twice.

        Announcing stops with it. BEP 5 has no goodbye, so the entries we
        published stay on other nodes until their own TTLs expire them; that is
        the protocol, not something we can undo from here.
        """
        if self._dht is not None:
            await self._announcer.stop()
            self._announcer.attach(None)
            await self._dht.aclose()
            self._dht = None
            self._resolver.dht = None

    def _emit(
        self,
        event_type: EventType,
        message: str,
        torrent_id: str = "",
        data: dict[str, object] | None = None,
        *,
        level: int = logging.INFO,
    ) -> None:
        """Publish a session-level event."""
        self._bus.emit(
            make_event(event_type, message=message, torrent_id=torrent_id, level=level, data=data)
        )

    def get(self, hex_info_hash: str) -> TorrentService | None:
        """The torrent with this info hash, if the session has it.

        The lookup is case-insensitive, because a hash pasted out of a magnet
        link arrives in whatever case it was written in.
        """
        return self._services.get(hex_info_hash.lower())

    # ------------------------------------------------------------ swarm views

    async def peers_view(self, hex_info_hash: str) -> tuple[PeerView, ...]:
        """Read one torrent's swarm, on the engine's loop.

        A coroutine, and not because there is anything to await: reading live
        connection state has to happen on the loop that owns it, and being a
        coroutine is what makes submitting it the only way to call it.
        """
        service = self.get(hex_info_hash)
        return () if service is None else service.peers_view()

    async def piece_map(self, hex_info_hash: str) -> PieceMap | None:
        """Read one torrent's pieces, on the engine's loop. ``None`` if gone."""
        service = self.get(hex_info_hash)
        return None if service is None else service.piece_map()

    async def files_view(self, hex_info_hash: str) -> tuple[FileView, ...]:
        """Read one torrent's files, on the engine's loop."""
        service = self.get(hex_info_hash)
        return () if service is None else service.files_view()

    async def trackers_view(self, hex_info_hash: str) -> tuple[TrackerStatus, ...]:
        """Read one torrent's tracker health, on the engine's loop."""
        service = self.get(hex_info_hash)
        return () if service is None else service.trackers_view()

    async def remove(
        self, hex_info_hash: str, *, delete_data: bool = False
    ) -> TorrentService | None:
        """Remove a torrent, stopping it first. Returns what was removed."""
        service = self._services.pop(hex_info_hash.lower(), None)
        if service is None:
            return None
        self._services.pop(service.hex_info_hash, None)
        self._announcer.unregister(service.torrent.info_hash)
        await service.remove(delete_data=delete_data)
        self._emit(
            EventType.TORRENT_REMOVED,
            f"removed {service.torrent.name}",
            service.hex_info_hash,
            {"delete_data": delete_data},
        )
        return service

    # ---------------------------------------------------------------- control

    async def start_all(self) -> int:
        """Start every torrent that is not already running. Returns how many."""
        started = 0
        for service in self._services.values():
            await service.start()
            started += 1
        return started

    async def stop_all(self) -> None:
        """Stop every torrent. Data, resume state, and the torrents themselves stay."""
        for service in reversed(self._services.values()):
            with _swallow(service.torrent.name):
                await service.stop()

    async def aclose(self) -> None:
        """Stop everything and release the bus. Safe to call twice."""
        await self.stop_dht()
        await self._announcer.aclose()
        for service in reversed(tuple(self._services.values())):
            with _swallow(service.torrent.name):
                await service.aclose()
        self._services.clear()
        if self._owns_bus:
            self._emit(EventType.SYSTEM_STOPPED, "session closed")
            await self._bus.aclose()

    async def __aenter__(self) -> Session:
        self._emit(
            EventType.SYSTEM_STARTED,
            "session started",
            data={"download_directory": str(self._config.storage.download_directory)},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------ inspection

    def totals(self) -> SessionTotals:
        """Measured bandwidth summed over every torrent in the session."""
        download_rate = 0.0
        upload_rate = 0.0
        downloaded = 0
        uploaded = 0
        active = 0
        for service in self._services.values():
            engine = service.engine
            snapshot = engine.snapshot()
            download_rate += snapshot.download.short
            upload_rate += snapshot.upload.short
            downloaded += snapshot.download.total
            uploaded += snapshot.upload.total
            if engine.state in {
                TorrentState.STARTING,
                TorrentState.DOWNLOADING,
                TorrentState.SEEDING,
            }:
                active += 1
        return SessionTotals(
            torrents=len(self._services),
            active=active,
            download_rate=download_rate,
            upload_rate=upload_rate,
            downloaded_bytes=downloaded,
            uploaded_bytes=uploaded,
        )


@contextlib.contextmanager
def _swallow(label: str) -> Iterator[None]:
    """Log and drop an exception raised while shutting ``label`` down.

    One torrent failing to stop must not leave the others running — that is
    the whole reason this exists. Used around per-torrent shutdown only.
    """
    try:
        yield
    except Exception as error:  # noqa: BLE001 - a shutdown must finish
        logger.warning("closing %s failed: %s", label, error)
