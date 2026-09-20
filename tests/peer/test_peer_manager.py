"""Integration tests for the peer manager against real seeders.

The manager's job is bookkeeping under unreliable conditions: duplicate
addresses, dead addresses, peers that hang up mid-transfer, and a hard cap on
how many connections we are allowed to hold. All of it is exercised here with
real sockets rather than mocks of the connection object, because most of these
bugs are timing and lifecycle bugs.
"""

from __future__ import annotations

import asyncio

import pytest
from app.core.config import NetworkConfig
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.peer.connection import SwarmContext
from app.peer.discovery.peer_manager import PeerCandidate, PeerManager
from app.peer.messages import Have, Interested, Request
from app.peer.messages import Piece as PieceMessage
from app.tracker.base import PeerAddress

from tests.mocks.mock_peer import MockPeer
from tests.peer.conftest import TEST_INFO_HASH, TEST_PEER_ID, wait_until

PIECE_LENGTH = 16 * 1024


@pytest.fixture
def context(payload: bytes) -> SwarmContext:
    return SwarmContext(
        info_hash=TEST_INFO_HASH,
        piece_count=(len(payload) + PIECE_LENGTH - 1) // PIECE_LENGTH,
        piece_length=PIECE_LENGTH,
        name="test.bin",
    )


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def events(bus: EventBus) -> list[Event]:
    seen: list[Event] = []
    bus.subscribe_all(lambda event: seen.append(event))
    return seen


def make_manager(
    context: SwarmContext,
    *,
    bus: EventBus | None = None,
    config: NetworkConfig | None = None,
    **kwargs: object,
) -> PeerManager:
    return PeerManager(
        context,
        peer_id=TEST_PEER_ID,
        config=config or NetworkConfig(),
        event_bus=bus,
        **kwargs,  # type: ignore[arg-type]
    )


async def start_seeder(payload: bytes, **kwargs: object) -> MockPeer:
    peer = MockPeer(payload, info_hash=TEST_INFO_HASH, piece_length=PIECE_LENGTH, **kwargs)  # type: ignore[arg-type]
    await peer.start()
    return peer


class TestDiscovery:
    def test_deduplicates_addresses(self, context: SwarmContext) -> None:
        manager = make_manager(context)
        address = PeerAddress(host="10.0.0.1", port=6881)

        assert manager.add_peers([address]) == 1
        assert manager.add_peers([address, PeerAddress(host="10.0.0.1", port=6881)]) == 0
        assert manager.stats.candidates == 1
        assert manager.stats.discovered == 1

    def test_ignores_our_own_loopback_port(self, context: SwarmContext) -> None:
        manager = make_manager(context, our_port=6881)
        added = manager.add_peers(
            [
                PeerAddress(host="127.0.0.1", port=6881),
                PeerAddress(host="127.0.0.1", port=6999),
            ]
        )
        assert added == 1
        assert manager.candidates[0].address.port == 6999

    def test_records_provenance(self, context: SwarmContext) -> None:
        manager = make_manager(context)
        manager.add_peers([PeerAddress(host="10.0.0.1", port=6881, source="pex")], source="dht")
        assert manager.candidates[0].address.source == "dht"

    def test_emits_discovery_events(
        self, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        manager = make_manager(context, bus=bus)
        manager.add_peers([PeerAddress(host="10.0.0.1", port=6881)])

        discovered = [event for event in events if event.type is EventType.PEER_DISCOVERED]
        assert len(discovered) == 1
        assert discovered[0].data["host"] == "10.0.0.1"


class TestConnecting:
    async def test_fill_connects_to_available_peers(
        self, context: SwarmContext, payload: bytes
    ) -> None:
        first = await start_seeder(payload)
        second = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([first.address, second.address])
            connections = await manager.fill()

            assert len(connections) == 2
            assert manager.stats.connected == 2
            assert all(connection.connected for connection in connections)
            assert manager.candidates == ()
        finally:
            await manager.stop()
            await first.stop()
            await second.stop()

    async def test_fill_respects_the_slot_cap(self, context: SwarmContext, payload: bytes) -> None:
        first = await start_seeder(payload)
        second = await start_seeder(payload)
        manager = make_manager(context, config=NetworkConfig(max_peers_per_torrent=1))
        try:
            manager.add_peers([first.address, second.address])
            connections = await manager.fill()

            assert len(connections) == 1
            assert manager.stats.candidates == 1
            assert await manager.fill() == ()  # no slots left
        finally:
            await manager.stop()
            await first.stop()
            await second.stop()

    async def test_connect_to_reuses_an_existing_connection(
        self, context: SwarmContext, payload: bytes
    ) -> None:
        seeder = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([seeder.address])
            first = await manager.connect_to(seeder.address)
            again = await manager.connect_to(seeder.address)
            assert first is again
            assert manager.stats.connected == 1
        finally:
            await manager.stop()
            await seeder.stop()

    async def test_a_dead_address_is_recorded_and_dropped(
        self, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        config = NetworkConfig(max_peer_failures=1, reconnect_delay=30.0)
        manager = make_manager(context, config=config, bus=bus)
        try:
            manager.add_peers([PeerAddress(host="127.0.0.1", port=1)])
            assert await manager.fill() == ()

            assert manager.stats.candidates == 0
            assert any(event.type is EventType.PEER_FAILED for event in events)
        finally:
            await manager.stop()

    async def test_a_failing_candidate_backs_off(self, context: SwarmContext) -> None:
        config = NetworkConfig(max_peer_failures=3, reconnect_delay=30.0)
        manager = make_manager(context, config=config)
        try:
            manager.add_peers([PeerAddress(host="127.0.0.1", port=1)])
            await manager.fill()

            candidate = manager.candidates[0]
            assert candidate.failures == 1
            assert not candidate.available  # backed off, so fill() skips it
            assert await manager.fill() == ()
        finally:
            await manager.stop()

    async def test_receiving_a_block_reaches_the_callback(
        self, context: SwarmContext, payload: bytes
    ) -> None:
        seeder = await start_seeder(payload)
        blocks: list[PieceMessage] = []
        manager = make_manager(context, on_block=lambda _peer, message: blocks.append(message))
        try:
            manager.add_peers([seeder.address])
            (connection,) = await manager.fill()

            await connection.send_interested()
            await wait_until(lambda: not connection.choked)
            await connection.send(Request(index=0, begin=0, length=2048))
            await wait_until(lambda: bool(blocks))

            assert blocks[0].data == payload[:2048]
        finally:
            await manager.stop()
            await seeder.stop()


class TestReaping:
    async def test_a_disconnected_peer_frees_its_slot(
        self, context: SwarmContext, payload: bytes
    ) -> None:
        seeder = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([seeder.address])
            (connection,) = await manager.fill()
            await wait_until(lambda: connection.bitfield.count == context.piece_count)

            await seeder.stop()
            await wait_until(lambda: manager.stats.connected == 0)

            assert manager.stats.candidates == 1
            candidate = manager.candidates[0]
            assert candidate.failures == 0  # it did complete a handshake
            assert not candidate.available  # but it waits out a backoff
        finally:
            await manager.stop()

    async def test_the_disconnect_callback_fires(
        self, context: SwarmContext, payload: bytes
    ) -> None:
        seeder = await start_seeder(payload)
        seen: list[tuple[str, str]] = []
        manager = make_manager(
            context,
            on_disconnect=lambda connection, reason: seen.append((str(connection.address), reason)),
        )
        try:
            manager.add_peers([seeder.address])
            await manager.fill()
            await seeder.stop()
            await wait_until(lambda: bool(seen))
            assert seen[0][0] == str(seeder.address)
        finally:
            await manager.stop()


class TestQueries:
    async def test_peers_holding_and_unchoked(self, context: SwarmContext, payload: bytes) -> None:
        seeder = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([seeder.address])
            (connection,) = await manager.fill()
            await wait_until(lambda: connection.bitfield.count == context.piece_count)

            assert manager.peers_holding(0) == (connection,)
            assert manager.unchoked_peers() == ()  # it has not unchoked us yet

            await connection.send_interested()
            await wait_until(lambda: not connection.choked)
            assert manager.unchoked_peers() == (connection,)
        finally:
            await manager.stop()
            await seeder.stop()

    async def test_connection_for(self, context: SwarmContext, payload: bytes) -> None:
        seeder = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([seeder.address])
            (connection,) = await manager.fill()

            assert manager.connection_for(seeder.address) is connection
            assert manager.connection_for(PeerAddress(host="10.9.9.9", port=1)) is None
        finally:
            await manager.stop()
            await seeder.stop()


class TestBroadcast:
    async def test_reaches_every_connected_peer(
        self, context: SwarmContext, payload: bytes
    ) -> None:
        first = await start_seeder(payload)
        second = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([first.address, second.address])
            await manager.fill()
            await wait_until(lambda: first.connection_count == 1 and second.connection_count == 1)

            assert await manager.broadcast(Interested()) == 2
        finally:
            await manager.stop()
            await first.stop()
            await second.stop()

    async def test_a_dead_peer_does_not_stop_the_others(
        self, context: SwarmContext, payload: bytes
    ) -> None:
        seeder = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([seeder.address])
            (connection,) = await manager.fill()
            await wait_until(lambda: connection.bitfield.count == context.piece_count)

            await connection.aclose()
            manager._connections[connection.address.address] = connection  # simulate a dead socket

            assert await manager.broadcast(Have(index=0)) == 0
        finally:
            await manager.stop()
            await seeder.stop()


class TestMaintenance:
    async def test_maintain_keeps_filling(self, context: SwarmContext, payload: bytes) -> None:
        seeder = await start_seeder(payload)
        manager = make_manager(context)
        try:
            task = manager.start(interval=0.05)
            manager.add_peers([seeder.address])
            await wait_until(lambda: manager.stats.connected == 1)

            await manager.stop()
            assert task.done()
            assert manager.stats.connected == 0
        finally:
            await seeder.stop()

    async def test_start_twice_is_refused(self, context: SwarmContext) -> None:
        manager = make_manager(context)
        try:
            task = manager.start(interval=0.05)
            with pytest.raises(RuntimeError, match="already running"):
                manager.start()
            await manager.stop()
            assert task.done()
        finally:
            await manager.stop()

    async def test_close_all(self, context: SwarmContext, payload: bytes) -> None:
        seeder = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([seeder.address])
            (connection,) = await manager.fill()
            await manager.close_all()

            assert manager.connections == ()
            assert connection.closed
        finally:
            await seeder.stop()


class TestCandidate:
    def test_failure_backoff_grows_and_can_drop(self) -> None:
        candidate = PeerCandidate(address=PeerAddress(host="10.0.0.1", port=6881))

        assert candidate.available
        assert candidate.record_failure("refused", max_failures=3, reconnect_delay=10.0) is False
        assert candidate.failures == 1
        assert not candidate.available

        candidate.record_failure("timeout", max_failures=3, reconnect_delay=10.0)
        assert candidate.record_failure("reset", max_failures=3, reconnect_delay=10.0) is True

    def test_success_clears_failures(self) -> None:
        candidate = PeerCandidate(address=PeerAddress(host="10.0.0.1", port=6881))
        candidate.record_failure("refused", max_failures=3, reconnect_delay=10.0)
        candidate.record_success()

        assert candidate.failures == 0
        assert candidate.available
        assert candidate.last_error is None

    def test_disconnect_keeps_a_handshaked_peer_but_waits(self) -> None:
        candidate = PeerCandidate(address=PeerAddress(host="10.0.0.1", port=6881))
        spent = candidate.note_disconnect(
            "peer closed", max_failures=3, reconnect_delay=10.0, handshaked=True
        )

        assert spent is False
        assert candidate.failures == 0
        assert not candidate.available  # redial later, not instantly

    def test_disconnect_counts_a_peer_that_never_answered(self) -> None:
        candidate = PeerCandidate(address=PeerAddress(host="10.0.0.1", port=6881))
        spent = candidate.note_disconnect(
            "handshake failed", max_failures=1, reconnect_delay=10.0, handshaked=False
        )

        assert spent is True
        assert candidate.failures == 1


class TestStats:
    def test_counts(self, context: SwarmContext, payload: bytes) -> None:
        manager = make_manager(context, max_connections=8)
        manager.add_peers(
            [PeerAddress(host="10.0.0.1", port=6881), PeerAddress(host="10.0.0.2", port=6882)]
        )

        stats = manager.stats
        assert stats.discovered == 2
        assert stats.candidates == 2
        assert stats.connected == 0
        assert stats.max_connections == 8

    async def test_counts_a_peer_with_nothing(self, context: SwarmContext, payload: bytes) -> None:
        from app.peer.bitfield import Bitfield

        seeder = await start_seeder(payload, bitfield=Bitfield(context.piece_count))
        manager = make_manager(context)
        try:
            manager.add_peers([seeder.address])
            await manager.fill()

            assert manager.stats.connected == 1
            assert manager.stats.uninteresting == 1
            assert manager.peers_holding(0) == ()
        finally:
            await manager.stop()
            await seeder.stop()


async def test_connections_are_closed_when_the_manager_stops(
    context: SwarmContext, payload: bytes
) -> None:
    seeder = await start_seeder(payload)
    manager = make_manager(context)
    try:
        manager.add_peers([seeder.address])
        (connection,) = await manager.fill()
        await manager.stop()

        assert connection.closed
        assert manager.connections == ()
    finally:
        await seeder.stop()
        await asyncio.sleep(0)  # let any reap task settle


class TestFillLimits:
    async def test_fill_honours_an_explicit_limit(
        self, context: SwarmContext, payload: bytes
    ) -> None:
        first = await start_seeder(payload)
        second = await start_seeder(payload)
        manager = make_manager(context)
        try:
            manager.add_peers([first.address, second.address])
            connections = await manager.fill(limit=1)

            assert len(connections) == 1
            assert manager.stats.candidates == 1
        finally:
            await manager.stop()
            await first.stop()
            await second.stop()
