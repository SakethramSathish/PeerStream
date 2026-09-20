"""Tests for the incoming-connection listener.

Sockets are real; the peers dialling us are the same `MockLeecher` the upload
tests use. What matters here is not the transfer but the door policy: who gets
in, who is turned away, and whether the listener can be stopped cleanly while
people are still connected.
"""

from __future__ import annotations

import asyncio

import pytest
from app.core.config import NetworkConfig
from app.core.peer_id import generate_peer_id
from app.peer.connection import SwarmContext
from app.peer.discovery.listener import PeerListener
from app.peer.discovery.peer_manager import PeerManager
from app.torrent import Torrent

from tests.mocks.mock_leecher import MockLeecher


@pytest.fixture
def swarm(sample_torrent: Torrent):
    """A peer manager and its listener, both stopped after the test."""
    peers = PeerManager(
        SwarmContext.from_torrent(sample_torrent),
        peer_id=generate_peer_id(),
        config=NetworkConfig(),
    )
    listener = PeerListener(peers, host="127.0.0.1", port=0)
    yield peers, listener


class TestListening:
    async def test_the_port_is_reported_back(self, swarm, sample_torrent: Torrent) -> None:
        peers, listener = swarm

        port = await listener.start()

        assert port > 0
        assert listener.listening is True
        assert listener.port == port
        await listener.stop()
        assert listener.listening is False
        await peers.stop()

    async def test_starting_twice_changes_nothing(self, swarm) -> None:
        peers, listener = swarm

        first = await listener.start()
        second = await listener.start()

        assert first == second
        await listener.stop()
        await peers.stop()

    async def test_the_context_manager_binds_and_releases(self, swarm) -> None:
        peers, listener = swarm

        async with listener:
            assert listener.listening is True

        assert listener.listening is False
        await peers.stop()


class TestAccepting:
    async def test_a_peer_that_dials_us_is_adopted(self, swarm, sample_torrent: Torrent) -> None:
        peers, listener = swarm
        port = await listener.start()
        leecher = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )

        adopted: tuple = ()
        try:
            assert await leecher.connect("127.0.0.1", port)
            await asyncio.sleep(0.1)
            adopted = peers.connections
            states = [(peer.connected, peer.session.peer_id is not None) for peer in adopted]
            stats = listener.stats
        finally:
            await listener.stop()
            await peers.stop()
            await leecher.aclose()

        assert len(adopted) == 1
        assert stats.accepted == 1
        assert stats.refused == 0
        assert states == [(True, True)]  # connected, and the handshake really happened

    async def test_several_peers_are_adopted_concurrently(
        self, swarm, sample_torrent: Torrent
    ) -> None:
        peers, listener = swarm
        port = await listener.start()
        leechers = [
            MockLeecher(
                info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
            )
            for _ in range(3)
        ]

        count = 0
        try:
            for leecher in leechers:
                assert await leecher.connect("127.0.0.1", port)
            await asyncio.sleep(0.2)
            count = len(peers.connections)
            stats = listener.stats
        finally:
            await listener.stop()
            await peers.stop()
            for leecher in leechers:
                await leecher.aclose()

        assert count == 3
        assert stats.accepted == 3

    async def test_a_peer_asking_for_another_torrent_is_refused(
        self, swarm, sample_torrent: Torrent
    ) -> None:
        peers, listener = swarm
        port = await listener.start()
        stranger = MockLeecher(
            info_hash=sample_torrent.info_hash,
            piece_length=sample_torrent.piece_length,
            wrong_info_hash=bytes(range(20)),
        )

        try:
            await stranger.connect("127.0.0.1", port)
            await asyncio.sleep(0.1)
        finally:
            await listener.stop()
            await peers.stop()
            await stranger.aclose()

        assert stranger.handshake_failed is True
        assert peers.connections == ()
        assert listener.stats.handshake_failures == 1
        assert listener.stats.accepted == 0

    async def test_a_slot_is_not_given_away_twice(self, swarm, sample_torrent: Torrent) -> None:
        """One free slot, two peers: the second is turned away, not queued."""
        peers, listener = swarm
        peers.max_connections = 1
        port = await listener.start()
        first = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )
        second = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )

        count = 0
        try:
            assert await first.connect("127.0.0.1", port)
            await asyncio.sleep(0.1)  # let the adoption finish
            # The second peer is closed out before the handshake even starts:
            # a full house is not an invitation to wait in the hallway.
            assert await second.connect("127.0.0.1", port) is False
            await asyncio.sleep(0.1)
            count = len(peers.connections)
            stats = listener.stats
        finally:
            await listener.stop()
            await peers.stop()
            await first.aclose()
            await second.aclose()

        assert second.handshake_failed is True
        assert count == 1
        assert stats.accepted == 1
        assert stats.refused == 1
        assert stats.handshake_failures == 0  # refused for space, not misbehaviour

    async def test_a_peer_that_says_nothing_is_closed(self, swarm, sample_torrent: Torrent) -> None:
        peers, listener = swarm
        port = await listener.start()
        silent = MockLeecher(
            info_hash=sample_torrent.info_hash,
            piece_length=sample_torrent.piece_length,
            silent=True,
        )

        try:
            await silent.connect("127.0.0.1", port)
            await asyncio.sleep(0.3)
        finally:
            await listener.stop()
            await peers.stop()
            await silent.aclose()

        # The handshake never came, so the connection was never adopted; the
        # socket is still open only because this test is holding it.
        assert listener.stats.accepted == 0
        assert peers.connections == ()


class TestStopping:
    async def test_stopping_does_not_wait_for_the_swarm(
        self, swarm, sample_torrent: Torrent
    ) -> None:
        """Connections belong to the peer manager; the listener must not block."""
        peers, listener = swarm
        port = await listener.start()
        leecher = MockLeecher(
            info_hash=sample_torrent.info_hash, piece_length=sample_torrent.piece_length
        )
        assert await leecher.connect("127.0.0.1", port)
        await asyncio.sleep(0.1)

        await asyncio.wait_for(listener.stop(), timeout=2.0)  # must not deadlock

        assert listener.listening is False
        assert len(peers.connections) == 1  # still connected: not our business
        await peers.stop()
        await leecher.aclose()

    async def test_stopping_without_starting_is_harmless(self, swarm) -> None:
        peers, listener = swarm

        await listener.stop()

        assert listener.listening is False
        await peers.stop()
