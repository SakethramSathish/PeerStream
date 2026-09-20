"""Integration tests: the statistics engine against a real swarm.

The unit tests prove the arithmetic. These prove the wiring: that bytes moving
over a real socket into the real download engine end up counted by the meters,
that a rate really does fall to zero when the swarm stops, and that seeding to
a real leecher shows up on the upload side of the same snapshot.

Nothing here is asserted against a made-up number: every comparison is between
two things that were measured independently — the engine's own block counters
and the collector's byte counters.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from app.core.config import DownloadConfig, NetworkConfig, StorageConfig, UploadConfig
from app.core.event_bus import EventBus
from app.core.peer_id import generate_peer_id
from app.download.manager import DownloadManager
from app.peer.bitfield import Bitfield
from app.peer.connection import SwarmContext
from app.peer.discovery.listener import PeerListener
from app.peer.discovery.peer_manager import PeerManager
from app.peer.messages import Request
from app.statistics.metrics import (
    SERIES_DOWNLOAD_RATE,
    SERIES_ETA,
    SERIES_PEERS,
    SERIES_PROGRESS,
    SERIES_UPLOAD_RATE,
    MetricsCollector,
    MetricsSnapshot,
)
from app.storage.manager import StorageManager
from app.torrent import Torrent
from app.upload.manager import UploadManager

from tests.mocks.mock_leecher import MockLeecher
from tests.mocks.mock_peer import MockPeer

pytestmark = pytest.mark.integration

BLOCK = 16 * 1024


@asynccontextmanager
async def seeding_swarm(
    payload: bytes,
    torrent: Torrent,
    directory: Path,
    *,
    seeds: int = 2,
    windows: tuple[float, ...] = (1.0, 5.0, 30.0),
    delay: float = 0.0,
) -> AsyncIterator[tuple[DownloadManager, PeerManager, StorageManager, MetricsCollector]]:
    """A download from real seeders, with a collector listening to the bus.

    Args:
        delay: How long each seeder takes to answer a block. Loopback is
            faster than any real swarm, so tests that want to watch a download
            in progress — rather than only its result — slow the seeders down.
    """
    seeders = [await _start_seeder(payload, torrent, delay=delay) for _ in range(seeds)]
    storage = StorageManager(
        torrent,
        directory / "dl",
        config=StorageConfig(state_directory=directory / "state"),
    )
    await storage.prepare()
    bus = EventBus()
    context = SwarmContext.from_torrent(torrent)
    peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
    manager = DownloadManager(
        torrent,
        storage=storage,
        peers=peers,
        config=DownloadConfig(block_size=BLOCK, max_outstanding_requests=8),
        event_bus=bus,
        block_timeout=5.0,
    )
    peers.on_block = manager.on_block
    peers.on_disconnect = manager.on_disconnect
    peers.on_have = manager.on_have
    peers.add_peers([seeder.address for seeder in seeders], source="test")
    collector = MetricsCollector(
        torrent, download=manager, peers=peers, event_bus=bus, windows=windows
    )

    try:
        await peers.fill()
        yield manager, peers, storage, collector
    finally:
        await manager.stop()
        await peers.stop()
        for seeder in seeders:
            await seeder.stop()
        await storage.aclose()


async def store_payload(storage: StorageManager, torrent: Torrent, payload: bytes) -> Bitfield:
    """Write every piece to disk, the way a finished download would."""
    await storage.prepare()
    have = Bitfield(torrent.piece_count)
    for index in range(torrent.piece_count):
        start = torrent.piece_offset(index)
        await storage.write_piece(index, payload[start : start + torrent.piece_size(index)])
        have.set(index)
    return have


async def _start_seeder(payload: bytes, torrent: Torrent, *, delay: float = 0.0) -> MockPeer:
    seeder = MockPeer(
        payload,
        info_hash=torrent.info_hash,
        piece_length=torrent.piece_length,
        request_delay=delay,
    )
    await seeder.start()
    return seeder


async def wait_for(
    predicate,
    *,
    timeout: float = 20.0,
    interval: float = 0.02,
) -> bool:
    """Poll until ``predicate`` holds, or give up. Polling, not sleeping blind."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


class TestWatchingADownload:
    async def test_every_block_the_engine_counts_is_a_byte_the_meter_counts(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        """Two independent counts of the same transfer must agree."""
        async with seeding_swarm(payload, sample_torrent, tmp_path) as (
            manager,
            _peers,
            storage,
            collector,
        ):
            await manager.start()
            assert await wait_for(lambda: manager.stats.blocks_received > 0)

            snapshot = collector.sample()

            assert collector.download_speed.total >= storage.downloaded_bytes
            assert snapshot.download.total > 0
            assert snapshot.peers_connected == 2

            await manager.wait_until_complete(timeout=60.0)
            await manager.flush()

            final = collector.sample()
            assert final.complete is True
            assert final.progress == 1.0
            assert final.eta_seconds == 0.0
            # Blocks received count the wire; the bytes they carry is what the
            # torrent is made of, give or take a short last block.
            assert final.download.total >= sample_torrent.total_length

    async def test_a_rate_is_measured_not_assumed(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        async with seeding_swarm(payload, sample_torrent, tmp_path) as (
            manager,
            _peers,
            _storage,
            collector,
        ):
            await manager.start()

            assert await wait_for(lambda: collector.sample().download.short > 0)
            moving = collector.sample()

            assert moving.state in {"downloading", "stalled"}
            assert moving.download.short > 0
            assert moving.eta_seconds is not None  # there is a rate to divide by
            assert moving.eta_seconds > 0.0

            await manager.wait_until_complete(timeout=60.0)

    async def test_the_eta_falls_as_the_torrent_fills(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        async with seeding_swarm(payload, sample_torrent, tmp_path, delay=0.01) as (
            manager,
            _peers,
            _storage,
            collector,
        ):
            await manager.start()
            assert await wait_for(lambda: collector.sample().pieces_verified >= 2)
            early = collector.sample()

            assert await wait_for(
                lambda: (
                    collector.sample().eta_seconds is not None
                    and early.eta_seconds is not None
                    and collector.sample().eta_seconds < early.eta_seconds
                )
            )
            later = collector.sample()

            assert early.eta_seconds is not None and later.eta_seconds is not None
            assert later.eta_seconds < early.eta_seconds
            assert later.progress > early.progress

    async def test_a_stalled_meter_falls_to_zero(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        """The swarm stops; the rate must say so, not remember better times."""
        windows = (0.2, 0.4, 0.8)
        async with seeding_swarm(
            payload, sample_torrent, tmp_path, windows=windows, delay=0.01
        ) as (
            manager,
            peers,
            _storage,
            collector,
        ):
            await manager.start()
            assert await wait_for(lambda: manager.stats.blocks_received > 0, interval=0.002)
            # Stop while the download is still incomplete: a finished torrent
            # has nothing left to estimate, so it would prove nothing.
            assert await wait_for(lambda: collector.sample().download.short > 0)

            await manager.stop()
            await peers.stop()
            await asyncio.sleep(1.0)  # longer than the longest window

            idle = collector.sample()
            print(
                "IDLE",
                idle.pieces_verified,
                idle.pieces_missing,
                idle.pieces_total,
                idle.progress,
                idle.verified_bytes,
                getattr(manager, "complete", None),
                len(manager.verified_pieces),
            )

            assert idle.complete is False
            assert idle.download.short == 0.0
            assert idle.download.instant == 0.0
            assert idle.download.total > 0  # the bytes are still history
            assert idle.eta_seconds is None  # no rate, no estimate

    async def test_the_graphs_fill_and_stay_bounded(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        async with seeding_swarm(payload, sample_torrent, tmp_path) as (
            manager,
            _peers,
            _storage,
            collector,
        ):
            await collector.start(interval=0.02)
            try:
                await manager.start()
                await manager.wait_until_complete(timeout=60.0)
                await asyncio.sleep(0.2)
            finally:
                await collector.stop()

            progress = collector.history.series(SERIES_PROGRESS)
            rates = collector.history.series(SERIES_DOWNLOAD_RATE)

            assert len(progress) > 5
            assert len(rates) <= collector.history.capacity
            assert progress[-1].value >= progress[0].value
            assert [sample.timestamp for sample in progress] == sorted(
                sample.timestamp for sample in progress
            )
            assert max(sample.value for sample in progress) <= 1.0

    async def test_peer_counts_follow_the_connections(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        async with seeding_swarm(payload, sample_torrent, tmp_path, seeds=3) as (
            _manager,
            peers,
            _storage,
            collector,
        ):
            assert collector.sample().peers_connected == 3

            await peers.stop()

            assert collector.sample().peers_connected == 0

    async def test_the_bus_carries_samples_the_ui_could_draw(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        async with seeding_swarm(payload, sample_torrent, tmp_path) as (
            manager,
            _peers,
            _storage,
            collector,
        ):
            await manager.start()
            await collector.start(interval=0.05)
            try:
                assert await wait_for(lambda: collector.last is not None)
                await manager.wait_until_complete(timeout=60.0)
                await asyncio.sleep(0.1)
            finally:
                await collector.stop()

            last = collector.last
            assert last is not None
            exported = last.as_dict()

            assert exported["pieces_total"] == sample_torrent.piece_count
            assert exported["downloaded_bytes"] > 0
            assert exported["state"] in {"downloading", "seeding", "stalled", "waiting"}
            assert isinstance(exported["eta_seconds"], (float, type(None)))


class TestWatchingAnUpload:
    async def test_seeding_to_a_leecher_shows_up_as_upload_rate(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        """We are the seeder this time: the meters must count what we sent."""
        storage = StorageManager(
            sample_torrent,
            tmp_path / "seed",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        have = await store_payload(storage, sample_torrent, payload)

        bus = EventBus()
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(
            sample_torrent,
            storage=storage,
            peers=peers,
            config=UploadConfig(choke_interval=0.2),
            event_bus=bus,
            have=have,
        )
        peers.on_request = upload.on_request
        peers.on_cancel = upload.on_cancel
        peers.on_disconnect = upload.on_disconnect
        collector = MetricsCollector(
            sample_torrent, upload=upload, peers=peers, event_bus=bus, windows=(1.0, 5.0, 30.0)
        )
        listener = PeerListener(peers, host="127.0.0.1", port=0)
        port = await listener.start()
        leecher = MockLeecher(
            info_hash=sample_torrent.info_hash,
            piece_length=sample_torrent.piece_length,
            payload=payload,
        )
        try:
            await upload.start()
            assert await leecher.connect("127.0.0.1", port)
            await leecher.interested()
            assert await leecher.wait_for_unchoke()
            await leecher.request_piece(2)
            assert await wait_for(lambda: collector.sample().upload.total > 0)

            snapshot = collector.sample()

            # The upload engine counts bytes itself; the collector counts them
            # from the events. Two counts, one number.
            assert snapshot.upload.total == upload.stats.bytes_uploaded
            assert snapshot.upload.short > 0
            assert snapshot.verified_bytes == sample_torrent.total_length
            assert snapshot.complete is True
            assert snapshot.state == "seeding"
            assert snapshot.share_ratio is None  # nothing downloaded, no ratio
        finally:
            await leecher.aclose()
            await upload.stop()
            await listener.stop()
            await peers.stop()
            await storage.aclose()

    async def test_a_refused_request_is_counted_as_one(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        """A hostile peer's request is refused, and the refusal is visible."""
        storage = StorageManager(
            sample_torrent,
            tmp_path / "seed",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        await storage.prepare()
        bus = EventBus()
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(
            sample_torrent,
            storage=storage,
            peers=peers,
            config=UploadConfig(choke_interval=0.2),
            event_bus=bus,
        )
        collector = MetricsCollector(sample_torrent, upload=upload, peers=peers, event_bus=bus)
        try:
            # Nothing sampled yet, so nothing has been plotted.
            assert collector.history.series(SERIES_UPLOAD_RATE) == ()

            await upload.pump()  # no pieces held: every request is refused

            @dataclass(slots=True)
            class Address:
                host: str = "127.0.0.1"
                port: int = 51413

            @dataclass(slots=True)
            class Session:
                peer_interested: bool = True

            @dataclass(slots=True)
            class Stranger:
                """A peer asking for a piece this seeder does not have."""

                address: Address = field(default_factory=Address)
                session: Session = field(default_factory=Session)
                connected: bool = True

            upload.on_request(Stranger(), Request(index=0, begin=0, length=BLOCK))

            snapshot = collector.sample()

            assert snapshot.requests_refused == 1
            assert snapshot.upload.total == 0  # refused, never served
            # The graph now has a point, and the point is zero: nothing left us.
            assert [sample.value for sample in collector.history.series(SERIES_UPLOAD_RATE)] == [
                0.0
            ]
        finally:
            await peers.stop()
            await storage.aclose()


class TestTheWholePicture:
    async def test_a_sample_has_every_series_a_graph_needs(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        async with seeding_swarm(payload, sample_torrent, tmp_path) as (
            manager,
            _peers,
            _storage,
            collector,
        ):
            await manager.start()
            await collector.start(interval=0.05)
            try:
                assert await wait_for(lambda: collector.sample().peers_connected > 0)
                assert await wait_for(lambda: collector.sample().download.short > 0)
                assert await wait_for(lambda: collector.history.series(SERIES_ETA) != ())
            finally:
                await collector.stop()

            names = set(collector.history.names)

            assert {
                SERIES_DOWNLOAD_RATE,
                SERIES_UPLOAD_RATE,
                SERIES_PEERS,
                SERIES_PROGRESS,
            } <= names
            assert SERIES_ETA in names

    def test_a_snapshot_never_invents_a_number(self, sample_torrent: Torrent) -> None:
        """With nothing wired up, every number is either zero or absent."""
        collector = MetricsCollector(sample_torrent)

        snapshot: MetricsSnapshot = collector.sample(now=0.0)

        assert snapshot.download.short == 0.0
        assert snapshot.peers_connected == 0
        assert snapshot.eta_seconds is None
        assert snapshot.share_ratio is None
        assert snapshot.state == "waiting"
