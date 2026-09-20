"""Uploading, over real sockets, to a peer that is not us.

`MockLeecher` opens a TCP connection, shakes hands, says it is interested and
asks for blocks — the way a real peer would. On our side, the real listener,
the real peer manager and the real upload engine answer it. Nothing here is
simulated except the fact that both ends are in this process.

These tests are the upload engine's definition of done: a leecher that walks
away with the right bytes, a leecher that gets choked, and a leecher that
tries to make us read outside the torrent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.core.config import NetworkConfig, StorageConfig, UploadConfig
from app.core.event_bus import EventBus
from app.core.peer_id import generate_peer_id
from app.peer.bitfield import Bitfield
from app.peer.connection import SwarmContext
from app.peer.discovery.listener import PeerListener
from app.peer.discovery.peer_manager import PeerManager
from app.storage.manager import StorageManager
from app.torrent import Torrent
from app.upload.manager import UploadManager

from tests.mocks.mock_leecher import MockLeecher

BLOCK = 16 * 1024


async def store_payload(storage: StorageManager, torrent: Torrent, payload: bytes) -> Bitfield:
    """Write every piece to disk and return what we hold."""
    await storage.prepare()
    have = Bitfield(torrent.piece_count)
    for index in range(torrent.piece_count):
        start = torrent.piece_offset(index)
        await storage.write_piece(index, payload[start : start + torrent.piece_size(index)])
        have.set(index)
    return have


class TestUploadingToAPeer:
    async def test_a_leecher_walks_away_with_the_bytes(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        have = await store_payload(storage, sample_torrent, payload)
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(
            sample_torrent,
            storage=storage,
            peers=peers,
            config=UploadConfig(choke_interval=0.05),
            event_bus=EventBus(),
            have=have,
        )
        peers.on_request = upload.on_request
        peers.on_cancel = upload.on_cancel
        peers.on_disconnect = upload.on_disconnect

        listener = PeerListener(peers, host="127.0.0.1", port=0)
        port = await listener.start()
        leecher = MockLeecher(
            info_hash=sample_torrent.info_hash,
            piece_length=sample_torrent.piece_length,
            payload=payload,
        )
        try:
            assert await leecher.connect("127.0.0.1", port)
            await upload.start()
            await leecher.interested()
            assert await leecher.wait_for_unchoke()

            count = await leecher.request_piece(4)
            blocks = await leecher.wait_for_blocks(count)
            await upload.flush()
        finally:
            await upload.stop()
            await listener.stop()
            await peers.stop()
            await leecher.aclose()
            await storage.aclose()

        assert len(blocks) == count
        assert leecher.valid is True  # every byte matches the real payload
        assert upload.stats.blocks_served == count
        assert upload.stats.bytes_uploaded == sample_torrent.piece_size(4)
        assert listener.stats.accepted == 1

    async def test_an_interested_peer_is_unchoked_and_a_bored_one_is_not(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        have = await store_payload(storage, sample_torrent, payload)
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(
            sample_torrent,
            storage=storage,
            peers=peers,
            config=UploadConfig(choke_interval=0.05, slots=1),
            have=have,
        )
        listener = PeerListener(peers, host="127.0.0.1", port=0)
        port = await listener.start()
        want = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )
        bored = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )
        try:
            assert await want.connect("127.0.0.1", port)
            assert await bored.connect("127.0.0.1", port)
            await upload.start()
            await want.interested()
            await bored.interested(False)

            assert await want.wait_for_unchoke()
            await asyncio.sleep(0.15)  # long enough for a choke decision
            assert bored.unchoked is False
            assert upload.unchoked_peers() == (want_key(upload),)
        finally:
            await upload.stop()
            await listener.stop()
            await peers.stop()
            await want.aclose()
            await bored.aclose()
            await storage.aclose()

    async def test_a_peer_we_have_choked_gets_nothing(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        have = await store_payload(storage, sample_torrent, payload)
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(
            sample_torrent,
            storage=storage,
            peers=peers,
            config=UploadConfig(choke_interval=3600.0, slots=0),  # nobody, ever
            have=have,
        )
        peers.on_request = upload.on_request
        listener = PeerListener(peers, host="127.0.0.1", port=0)
        port = await listener.start()
        leecher = MockLeecher(
            info_hash=sample_torrent.info_hash,
            piece_length=sample_torrent.piece_length,
            payload=payload,
        )
        try:
            assert await leecher.connect("127.0.0.1", port)
            await upload.start()
            await upload.pump()  # decide choking once, with nobody allowed
            await leecher.interested()
            await leecher.request(0, 0, BLOCK)

            await asyncio.sleep(0.2)
            await upload.flush()
        finally:
            await upload.stop()
            await listener.stop()
            await peers.stop()
            await leecher.aclose()
            await storage.aclose()

        assert leecher.blocks == []
        assert upload.stats.requests_rejected.get("choked") == 1
        assert upload.stats.blocks_served == 0

    async def test_a_finished_piece_is_announced_to_the_swarm(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        await storage.prepare()
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(
            sample_torrent,
            storage=storage,
            peers=peers,
            config=UploadConfig(choke_interval=3600.0),
        )
        listener = PeerListener(peers, host="127.0.0.1", port=0)
        port = await listener.start()
        leecher = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )
        try:
            assert await leecher.connect("127.0.0.1", port)
            await upload.start()
            await upload.pump()  # track the peer so there is someone to tell

            upload.note_piece_verified(7)

            assert await leecher.wait_for_have(7)
            assert upload.have.count == 1
        finally:
            await upload.stop()
            await listener.stop()
            await peers.stop()
            await leecher.aclose()
            await storage.aclose()


class TestAHostilePeer:
    async def test_requests_outside_the_torrent_are_refused(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        """A stranger asking for piece 9,999 must not reach our disk."""
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        have = await store_payload(storage, sample_torrent, payload)
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(
            sample_torrent,
            storage=storage,
            peers=peers,
            config=UploadConfig(choke_interval=0.05, slots=4),
            have=have,
        )
        peers.on_request = upload.on_request
        peers.on_cancel = upload.on_cancel
        listener = PeerListener(peers, host="127.0.0.1", port=0)
        port = await listener.start()
        leecher = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )
        try:
            assert await leecher.connect("127.0.0.1", port)
            await upload.start()
            await leecher.interested()
            assert await leecher.wait_for_unchoke()

            # A block running past the end of a piece, and a piece that does
            # not exist. Both are refused; neither is read from disk.
            await leecher.request(0, sample_torrent.piece_size(0) - 8, BLOCK)
            await leecher.request(9_999, 0, BLOCK)
            await asyncio.sleep(0.2)
            await upload.flush()
        finally:
            await upload.stop()
            await listener.stop()
            await peers.stop()
            await leecher.aclose()
            await storage.aclose()

        assert upload.stats.requests_rejected.get("bad_length") == 1
        assert upload.stats.requests_rejected.get("bad_index") == 1
        assert upload.stats.blocks_served == 0
        assert leecher.blocks == []

    async def test_a_peer_asking_for_the_wrong_torrent_is_refused_at_the_door(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(sample_torrent, storage=storage, peers=peers, have=Bitfield(1))
        listener = PeerListener(peers, host="127.0.0.1", port=0)
        port = await listener.start()
        stranger = MockLeecher(
            info_hash=sample_torrent.info_hash,
            piece_length=sample_torrent.piece_length,
            wrong_info_hash=bytes(range(20)),
        )
        try:
            await stranger.connect("127.0.0.1", port)

            assert stranger.handshake_failed is True
            assert peers.connections == ()
            assert listener.stats.handshake_failures == 1
        finally:
            await upload.stop()
            await listener.stop()
            await peers.stop()
            await stranger.aclose()
            await storage.aclose()

    async def test_a_queue_cannot_grow_without_limit(
        self, sample_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=tmp_path / "state"),
        )
        have = await store_payload(storage, sample_torrent, payload)
        context = SwarmContext.from_torrent(sample_torrent)
        peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
        upload = UploadManager(
            sample_torrent,
            storage=storage,
            peers=peers,
            config=UploadConfig(
                choke_interval=0.05, slots=4, max_requests_per_peer=2, queue_timeout=3600.0
            ),
            have=have,
        )
        peers.on_request = upload.on_request
        listener = PeerListener(peers, host="127.0.0.1", port=0)
        port = await listener.start()
        leecher = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )
        try:
            assert await leecher.connect("127.0.0.1", port)
            await upload.start()
            await leecher.interested()
            assert await leecher.wait_for_unchoke()

            for index in range(6):
                await leecher.request(index, 0, BLOCK)
            await asyncio.sleep(0.1)
        finally:
            await upload.stop()
            await listener.stop()
            await peers.stop()
            await leecher.aclose()
            await storage.aclose()

        assert upload.stats.requests_rejected.get("queue_full") >= 1


def want_key(upload: UploadManager) -> str:
    """The single peer we decided to unchoke."""
    (key,) = upload.unchoked_peers()
    return key


if __name__ == "__main__":  # pragma: no cover - convenience for debugging
    pytest.main([__file__, "-v"])
