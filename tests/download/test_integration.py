"""Integration tests: the engine against real seeders on real sockets.

No mocks of our own protocol here — the peers are :class:`MockPeer` servers that
speak the actual wire protocol, the socket is a real TCP connection, the blocks
are real bytes, and the check at the end is that the file on disk is
byte-for-byte the payload the torrent's hashes were computed from.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.core.config import DownloadConfig, NetworkConfig, StorageConfig
from app.core.event_bus import EventBus
from app.core.events import EventType
from app.core.peer_id import generate_peer_id
from app.download.manager import DownloadManager
from app.peer.connection import SwarmContext
from app.peer.discovery.peer_manager import PeerManager
from app.storage.manager import StorageManager
from app.torrent import Torrent, parse_torrent

from tests.mocks.mock_peer import MockPeer

pytestmark = pytest.mark.integration


async def start_seeders(payload: bytes, torrent: Torrent, count: int) -> list[MockPeer]:
    """Start ``count`` seeders holding the whole payload."""
    seeders: list[MockPeer] = []
    for _ in range(count):
        seeder = MockPeer(payload, info_hash=torrent.info_hash, piece_length=torrent.piece_length)
        await seeder.start()
        seeders.append(seeder)
    return seeders


def build_engine(
    torrent: Torrent,
    *,
    storage: StorageManager,
    peers: PeerManager,
    config: DownloadConfig | None = None,
    bus: EventBus | None = None,
) -> DownloadManager:
    return DownloadManager(
        torrent,
        storage=storage,
        peers=peers,
        config=config or DownloadConfig(block_size=16 * 1024, max_outstanding_requests=8),
        event_bus=bus,
        block_timeout=5.0,
    )


class TestDownloadFromSeeders:
    async def test_a_whole_torrent_arrives_byte_for_byte(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        seeders = await start_seeders(payload, sample_torrent, 2)
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        await storage.prepare()
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        manager = build_engine(sample_torrent, storage=storage, peers=peers)
        peers.on_block = manager.on_block
        peers.on_disconnect = manager.on_disconnect
        peers.on_have = manager.on_have
        peers.add_peers([seeder.address for seeder in seeders], source="test")

        try:
            await peers.fill()
            assert len(peers.connections) == 2

            await manager.start()
            assert await manager.wait_until_complete(timeout=30.0) is True
            await manager.flush()
            await manager.stop()
        finally:
            await peers.stop()
            for seeder in seeders:
                await seeder.stop()

        assert (storage.root / "payload.bin").read_bytes() == payload
        assert manager.stats.pieces_verified == sample_torrent.piece_count
        assert manager.stats.pieces_failed == 0
        assert manager.stats.blocks_received >= sample_torrent.piece_count
        assert manager.progress == 1.0
        await storage.aclose()

    async def test_a_torrent_with_pieces_straddling_files(
        self, build_torrent, payload: bytes, tmp_path: Path
    ) -> None:
        """100 KiB pieces over four 128 KiB files: every piece crosses files."""
        torrent = parse_torrent(build_torrent(name="bundle", piece_length=100 * 1024, file_count=4))
        seeders = await start_seeders(payload, torrent, 1)
        storage = StorageManager(
            torrent, tmp_path / "dl", config=StorageConfig(state_directory=tmp_path / "state")
        )
        await storage.prepare()
        context = SwarmContext.from_torrent(torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        manager = build_engine(torrent, storage=storage, peers=peers)
        peers.on_block = manager.on_block
        peers.on_disconnect = manager.on_disconnect
        peers.add_peers([seeders[0].address], source="test")

        try:
            await peers.fill()
            await manager.start()
            assert await manager.wait_until_complete(timeout=30.0) is True
            await manager.flush()
            await manager.stop()
        finally:
            await peers.stop()
            for seeder in seeders:
                await seeder.stop()

        for entry in torrent.files:
            on_disk = (storage.root / entry.path).read_bytes()
            assert on_disk == payload[entry.offset : entry.end_offset]
        await storage.aclose()

    async def test_a_seeder_dying_mid_download_costs_nothing_but_time(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        seeders = await start_seeders(payload, sample_torrent, 2)
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        await storage.prepare()
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        manager = build_engine(sample_torrent, storage=storage, peers=peers)
        peers.on_block = manager.on_block
        peers.on_disconnect = manager.on_disconnect
        peers.add_peers([seeder.address for seeder in seeders], source="test")

        try:
            await peers.fill()
            await manager.start()

            deadline = asyncio.get_running_loop().time() + 20.0
            while (
                manager.stats.pieces_verified < 3 and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.02)
            assert manager.stats.pieces_verified >= 3

            # Pull the plug on one seeder; the other must finish the job.
            await seeders[0].stop()

            assert await manager.wait_until_complete(timeout=30.0) is True
            await manager.flush()
            await manager.stop()
        finally:
            await peers.stop()
            for seeder in seeders:
                await seeder.stop()

        assert (storage.root / "payload.bin").read_bytes() == payload
        await storage.aclose()

    async def test_resume_downloads_only_what_is_missing(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        await storage.prepare()
        half = sample_torrent.piece_count // 2
        for index in range(half):
            start = sample_torrent.piece_offset(index)
            await storage.write_piece(
                index, payload[start : start + sample_torrent.piece_size(index)]
            )
        seeders = await start_seeders(payload, sample_torrent, 1)
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        manager = build_engine(sample_torrent, storage=storage, peers=peers)
        peers.on_block = manager.on_block
        peers.on_disconnect = manager.on_disconnect
        peers.add_peers([seeders[0].address], source="test")

        try:
            await peers.fill()
            assert manager.verified_pieces == tuple(range(half))

            await manager.start()
            assert await manager.wait_until_complete(timeout=30.0) is True
            await manager.flush()
            await manager.stop()
        finally:
            await peers.stop()
            for seeder in seeders:
                await seeder.stop()

        # Only the missing pieces were fetched: the ones already on disk were
        # never requested, which is the whole point of resume.
        assert manager.stats.pieces_verified == sample_torrent.piece_count - half
        assert (storage.root / "payload.bin").read_bytes() == payload
        await storage.aclose()

    async def test_a_lying_seeder_never_poisons_the_disk(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        """A peer sending bytes that do not match the hash gets us nowhere."""
        liar = MockPeer(
            bytes(len(payload)),  # all zeroes: valid-looking, wrong bytes
            info_hash=sample_torrent.info_hash,
            piece_length=sample_torrent.piece_length,
        )
        await liar.start()
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        await storage.prepare()
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        manager = build_engine(sample_torrent, storage=storage, peers=peers)
        peers.on_block = manager.on_block
        peers.on_disconnect = manager.on_disconnect
        peers.add_peers([liar.address], source="test")

        try:
            await peers.fill()
            await manager.start()
            deadline = asyncio.get_running_loop().time() + 10.0
            while manager.stats.pieces_failed < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            await manager.stop()
        finally:
            await peers.stop()
            await liar.stop()

        assert manager.stats.pieces_failed >= 2
        assert manager.stats.pieces_verified == 0
        assert manager.complete is False
        # Not one byte of the bad data reached the file.
        assert (storage.root / "payload.bin").read_bytes() == bytes(len(payload))
        assert manager.peer_penalties  # the liar was blamed
        await storage.aclose()

    async def test_completion_publishes_progress_and_resume_state(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        bus = EventBus()
        events: list[object] = []
        bus.subscribe_all(events.append)
        seeders = await start_seeders(payload, sample_torrent, 1)
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        await storage.prepare()
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        manager = build_engine(sample_torrent, storage=storage, peers=peers, bus=bus)
        peers.on_block = manager.on_block
        peers.on_disconnect = manager.on_disconnect
        peers.add_peers([seeders[0].address], source="test")

        try:
            await peers.fill()
            await manager.start()
            assert await manager.wait_until_complete(timeout=30.0) is True
            await manager.flush()
            # Give the loop a tick to notice completion and save state.
            await asyncio.sleep(0.2)
            await manager.stop()
        finally:
            await peers.stop()
            for seeder in seeders:
                await seeder.stop()

        kinds = [event.type for event in events]  # type: ignore[union-attr]
        assert EventType.PIECE_VERIFIED in kinds
        assert EventType.TORRENT_COMPLETED in kinds
        state = await asyncio.to_thread(storage.resume_store.load, sample_torrent.info_hash)
        assert state is not None and state.completed_count == sample_torrent.piece_count
        await storage.aclose()
