"""Integration tests for peer connections against a real seeder.

Every test here runs the client and the seeder over a real TCP socket, with
the real handshake, the real framing and the real state machine in between.
:class:`tests.mocks.mock_peer.MockPeer` is a seeder that serves bytes from a
payload, so a block that arrives can be compared against what it should be.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import pytest
from app.core.config import NetworkConfig
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.peer.bitfield import Bitfield
from app.peer.connection import PeerConnection, SwarmContext
from app.peer.errors import (
    HandshakeError,
    PeerConnectionError,
    PeerDisconnected,
    PeerTimeoutError,
)
from app.peer.messages import Bitfield as BitfieldMessage
from app.peer.messages import KeepAlive, Piece, Request
from app.peer.protocol import PeerStream
from app.peer.state import ConnectionState
from app.tracker.base import PeerAddress

from tests.mocks.mock_peer import MockPeer
from tests.peer.conftest import (
    TEST_INFO_HASH,
    TEST_PEER_ID,
    FailingReader,
    FailingWriter,
    FakePeer,
    wait_until,
)

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
async def seeder(payload: bytes) -> MockPeer:
    peer = MockPeer(payload, info_hash=TEST_INFO_HASH, piece_length=PIECE_LENGTH)
    await peer.start()
    try:
        yield peer
    finally:
        await peer.stop()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def events(bus: EventBus) -> list[Event]:
    seen: list[Event] = []
    bus.subscribe_all(lambda event: seen.append(event))
    return seen


def make_connection(
    address: PeerAddress,
    context: SwarmContext,
    *,
    bus: EventBus | None = None,
    config: NetworkConfig | None = None,
    on_block: Callable[[PeerConnection, Piece], None] | None = None,
    on_have: Callable[[PeerConnection, int], None] | None = None,
) -> PeerConnection:
    return PeerConnection(
        address,
        context,
        peer_id=TEST_PEER_ID,
        config=config or NetworkConfig(),
        event_bus=bus,
        on_block=on_block,
        on_have=on_have,
    )


async def shutdown(connection: PeerConnection, task: asyncio.Task[None] | None = None) -> None:
    """Close a connection and settle its read-loop task.

    ``aclose`` cancels the loop, so awaiting the task afterwards would raise
    ``CancelledError``; tests that end a connection this way stay quiet about
    it, since being cancelled is exactly what they asked for.
    """
    await connection.aclose()
    if task is None:
        return
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class TestHandshake:
    async def test_completes_a_real_handshake(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        connection = make_connection(seeder.address, context)
        await connection.connect()
        try:
            assert connection.connected
            assert connection.peer_id == seeder.peer_id
            assert connection.session.state is ConnectionState.CONNECTED
            assert connection.latency_ms is not None
        finally:
            await connection.aclose()

    async def test_receives_the_seeders_bitfield(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        connection = make_connection(seeder.address, context)
        await connection.connect()
        task = connection.start()
        try:
            await wait_until(lambda: connection.bitfield.count == context.piece_count)
            assert connection.progress == 1.0
        finally:
            await shutdown(connection, task)

    async def test_publishes_lifecycle_events(
        self, seeder: MockPeer, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        connection = make_connection(seeder.address, context, bus=bus)
        await connection.connect()
        try:
            types = [event.type for event in events]
            assert EventType.PEER_CONNECTING in types
            assert EventType.PEER_CONNECTED in types
            assert EventType.PEER_HANDSHAKE in types
        finally:
            await connection.aclose()

    async def test_wrong_info_hash_is_rejected(
        self, payload: bytes, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        peer = MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            wrong_info_hash=bytes(range(20, 40)),
        )
        await peer.start()
        connection = make_connection(peer.address, context, bus=bus)
        try:
            with pytest.raises(HandshakeError, match="info_hash mismatch"):
                await connection.connect()
            assert any(event.type is EventType.PEER_FAILED for event in events)
            assert not connection.connected
        finally:
            await connection.aclose()
            await peer.stop()

    async def test_a_refused_connection_is_reported(
        self, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        connection = make_connection(PeerAddress(host="127.0.0.1", port=1), context, bus=bus)
        with pytest.raises(PeerConnectionError):
            await connection.connect()

        failures = [event for event in events if event.type is EventType.PEER_FAILED]
        assert failures
        assert "127.0.0.1:1" in failures[0].message

    async def test_a_silent_peer_times_out(self, context: SwarmContext) -> None:
        """A peer that accepts the connection and says nothing wastes a slot."""
        peer = FakePeer(reply_to_handshake=False)
        await peer.start()
        connection = make_connection(
            PeerAddress(host=peer.host, port=peer.port),
            context,
            config=NetworkConfig(handshake_timeout=0.2),
        )
        try:
            with pytest.raises(PeerTimeoutError):
                await connection.connect()
        finally:
            await connection.aclose()
            await peer.stop()

    async def test_a_peer_hanging_up_mid_handshake_is_a_disconnect(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        """Closed during the handshake: not a timeout, and not our fault."""
        peer = MockPeer(
            payload, info_hash=TEST_INFO_HASH, piece_length=PIECE_LENGTH, refuse_handshake=True
        )
        await peer.start()
        connection = make_connection(peer.address, context)
        try:
            with pytest.raises(PeerDisconnected):
                await connection.connect()
        finally:
            await connection.aclose()
            await peer.stop()


class TestMessaging:
    async def test_interested_is_answered_with_unchoke(
        self, seeder: MockPeer, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        connection = make_connection(seeder.address, context, bus=bus)
        await connection.connect()
        task = connection.start()
        try:
            await connection.send_interested()
            await wait_until(lambda: not connection.choked)

            assert connection.session.am_interested is True
            assert any(event.type is EventType.PEER_UNCHOKED for event in events)
        finally:
            await shutdown(connection, task)

    async def test_requesting_a_block_returns_the_real_bytes(
        self, seeder: MockPeer, context: SwarmContext, payload: bytes
    ) -> None:
        blocks: list[Piece] = []
        connection = make_connection(
            seeder.address, context, on_block=lambda _peer, message: blocks.append(message)
        )
        await connection.connect()
        task = connection.start()
        try:
            await connection.send_interested()
            await wait_until(lambda: not connection.choked)
            await connection.send(Request(index=0, begin=0, length=1024))

            await wait_until(lambda: bool(blocks))
            assert blocks[0].index == 0
            assert blocks[0].begin == 0
            assert blocks[0].data == payload[:1024]
            assert connection.session.downloaded == 1024
        finally:
            await shutdown(connection, task)

    async def test_the_block_callback_receives_the_connection(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        """The engine needs to know *which* peer sent a block (M7)."""
        seen: list[object] = []
        connection = make_connection(
            seeder.address, context, on_block=lambda peer, message: seen.append(peer)
        )
        await connection.connect()
        task = connection.start()
        try:
            await connection.send_interested()
            await wait_until(lambda: not connection.choked)
            await connection.send(Request(index=0, begin=0, length=1024))

            await wait_until(lambda: bool(seen))
            assert seen[0] is connection
        finally:
            await shutdown(connection, task)

    async def test_a_new_have_reaches_the_callback(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        announced: list[int] = []
        connection = make_connection(
            seeder.address, context, on_have=lambda peer, index: announced.append(index)
        )
        await connection.connect()
        task = connection.start()
        try:
            await seeder.send_have(3)

            await wait_until(lambda: bool(announced))
            assert announced == [3]
            assert connection.bitfield.has(3)
        finally:
            await shutdown(connection, task)

    async def test_a_repeated_have_is_not_announced_again(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        """Only a *new* piece changes rarity; repeats are noise."""
        announced: list[int] = []
        connection = make_connection(
            seeder.address, context, on_have=lambda peer, index: announced.append(index)
        )
        await connection.connect()
        task = connection.start()
        try:
            await seeder.send_have(3)
            await wait_until(lambda: bool(announced))
            await seeder.send_have(3)
            await asyncio.sleep(0.05)

            assert announced == [3]
        finally:
            await shutdown(connection, task)

    async def test_blocks_can_span_pieces_and_offsets(
        self, seeder: MockPeer, context: SwarmContext, payload: bytes
    ) -> None:
        blocks: list[Piece] = []
        connection = make_connection(
            seeder.address, context, on_block=lambda _peer, message: blocks.append(message)
        )
        await connection.connect()
        task = connection.start()
        try:
            await connection.send_interested()
            await wait_until(lambda: not connection.choked)
            await connection.send(Request(index=1, begin=512, length=2048))
            await wait_until(lambda: bool(blocks))

            expected = payload[PIECE_LENGTH + 512 : PIECE_LENGTH + 512 + 2048]
            assert blocks[0].data == expected
        finally:
            await shutdown(connection, task)

    async def test_sending_choke_updates_our_side(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        connection = make_connection(seeder.address, context)
        await connection.connect()
        try:
            assert connection.session.am_choking is True
            await connection.set_choking(False)
            assert connection.session.am_choking is False
            await connection.send_interested(False)
            assert connection.session.am_interested is False
        finally:
            await connection.aclose()

    async def test_sending_before_connecting_fails(self, context: SwarmContext) -> None:
        connection = make_connection(PeerAddress(host="127.0.0.1", port=1), context)
        with pytest.raises(PeerDisconnected, match="not connected"):
            await connection.send(KeepAlive())

    async def test_a_have_message_adds_to_the_bitfield(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        """``have`` is how a peer tells us about pieces it finished later."""
        partial = Bitfield.from_indices([0], context.piece_count)
        peer = MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            bitfield=partial,
        )
        await peer.start()
        connection = make_connection(peer.address, context)
        try:
            await connection.connect()
            task = connection.start()
            await wait_until(lambda: connection.bitfield.count == 1)

            assert not connection.bitfield.has(7)
            assert await peer.send_have(7) == 1
            await wait_until(lambda: connection.bitfield.has(7))
        finally:
            await shutdown(connection, task)
            await peer.stop()


class TestLifecycle:
    async def test_closing_reports_the_disconnect(
        self, seeder: MockPeer, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        connection = make_connection(seeder.address, context, bus=bus)
        await connection.connect()
        task = connection.start()
        await wait_until(lambda: connection.bitfield.count == context.piece_count)

        await shutdown(connection, task)

        assert connection.closed
        disconnects = [e for e in events if e.type is EventType.PEER_DISCONNECTED]
        assert disconnects
        assert "disconnected" in disconnects[0].message

    async def test_aclose_is_idempotent(self, seeder: MockPeer, context: SwarmContext) -> None:
        connection = make_connection(seeder.address, context)
        await connection.connect()
        await connection.aclose()
        await connection.aclose()
        assert connection.closed

    async def test_run_before_connect_is_a_programming_error(self, context: SwarmContext) -> None:
        connection = make_connection(PeerAddress(host="127.0.0.1", port=1), context)
        with pytest.raises(RuntimeError, match="connect"):
            await connection.run()

    async def test_start_twice_is_refused(self, seeder: MockPeer, context: SwarmContext) -> None:
        connection = make_connection(seeder.address, context)
        await connection.connect()
        task = connection.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                connection.start()
        finally:
            await shutdown(connection, task)

    async def test_the_peer_hanging_up_ends_the_loop(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        connection = make_connection(seeder.address, context)
        await connection.connect()
        task = connection.start()
        await wait_until(lambda: connection.bitfield.count == context.piece_count)

        await seeder.stop()
        await task

        assert connection.closed
        assert connection.disconnect_reason

    async def test_an_idle_peer_is_dropped(self, payload: bytes, context: SwarmContext) -> None:
        """A peer that says nothing at all must not hold a slot forever."""
        peer = MockPeer(
            payload, info_hash=TEST_INFO_HASH, piece_length=PIECE_LENGTH, serve_requests=False
        )
        await peer.start()
        config = NetworkConfig(idle_timeout=0.3, keepalive_interval=10.0)
        connection = make_connection(peer.address, context, config=config)
        try:
            await connection.connect()
            task = connection.start()
            await task
            assert connection.disconnect_reason is not None
            assert connection.disconnect_reason.startswith("idle")
        finally:
            await connection.aclose()
            await peer.stop()

    async def test_keep_alives_keep_a_quiet_connection_open(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        peer = MockPeer(
            payload, info_hash=TEST_INFO_HASH, piece_length=PIECE_LENGTH, serve_requests=False
        )
        await peer.start()
        config = NetworkConfig(keepalive_interval=0.2, idle_timeout=5.0)
        connection = make_connection(peer.address, context, config=config)
        try:
            await connection.connect()
            task = connection.start()
            await wait_until(
                lambda: any(isinstance(message, KeepAlive) for message in peer.received)
            )
            assert not connection.closed
        finally:
            await shutdown(connection, task)
            await peer.stop()

    async def test_a_malformed_peer_message_ends_the_connection(
        self, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        """A peer that declares an impossible length is dropped, not trusted."""
        peer = FakePeer()
        await peer.start()
        connection = make_connection(PeerAddress(host=peer.host, port=peer.port), context, bus=bus)
        try:
            await connection.connect()
            task = connection.start()
            await peer.write((2**32 - 1).to_bytes(4, "big"))
            await task

            assert any(event.type is EventType.PEER_FAILED for event in events)
            assert connection.disconnect_reason is not None
            assert "exceeds" in connection.disconnect_reason
        finally:
            await connection.aclose()
            await peer.stop()


class TestInspection:
    async def test_counters_and_description(self, seeder: MockPeer, context: SwarmContext) -> None:
        connection = make_connection(seeder.address, context)
        await connection.connect()
        try:
            assert connection.bytes_sent >= 68  # our handshake
            assert connection.bytes_received >= 68  # theirs
            assert connection.idle_for < 5
            text = str(connection)
            assert "127.0.0.1" in text
            assert "connected" in text
        finally:
            await connection.aclose()

    async def test_defaults_before_connecting(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        connection = make_connection(seeder.address, context)
        assert not connection.connected
        assert connection.closed is False
        assert connection.bytes_sent == 0
        assert connection.bytes_received == 0
        assert connection.latency_ms is None
        assert connection.client == "Unknown"
        assert connection.progress == 0.0


class TestSwarmContext:
    def test_rejects_bad_values(self) -> None:
        with pytest.raises(ValueError, match="info_hash must be 20 bytes"):
            SwarmContext(info_hash=b"short", piece_count=1, piece_length=1)
        with pytest.raises(ValueError, match="piece_count must be positive"):
            SwarmContext(info_hash=TEST_INFO_HASH, piece_count=0, piece_length=1)
        with pytest.raises(ValueError, match="piece_length must be positive"):
            SwarmContext(info_hash=TEST_INFO_HASH, piece_count=1, piece_length=0)

    def test_from_torrent(self, sample_torrent: object) -> None:
        context = SwarmContext.from_torrent(sample_torrent)
        assert context.info_hash == sample_torrent.info_hash  # type: ignore[attr-defined]
        assert context.hex_info_hash == sample_torrent.hex_info_hash  # type: ignore[attr-defined]
        assert context.piece_count == sample_torrent.piece_count  # type: ignore[attr-defined]
        assert context.name == sample_torrent.name  # type: ignore[attr-defined]


class TestProtocolViolations:
    async def test_a_peer_that_breaks_the_protocol_is_dropped(
        self, context: SwarmContext, bus: EventBus, events: list[Event]
    ) -> None:
        """A second bitfield is a protocol violation, not an update."""
        peer = FakePeer()
        await peer.start()
        connection = make_connection(PeerAddress(host=peer.host, port=peer.port), context, bus=bus)
        try:
            await connection.connect()
            task = connection.start()
            await peer.send(BitfieldMessage(data=b"\xff" * 4))
            await wait_until(lambda: connection.bitfield.count > 0)
            await peer.send(BitfieldMessage(data=b"\x00" * 4))
            await task

            failures = [event for event in events if event.type is EventType.PEER_FAILED]
            assert failures
            assert "second bitfield" in connection.disconnect_reason
        finally:
            await connection.aclose()
            await peer.stop()

    async def test_sending_choke_records_our_side(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        connection = make_connection(seeder.address, context)
        await connection.connect()
        try:
            await connection.set_choking(True)
            assert connection.session.am_choking is True
        finally:
            await connection.aclose()


class TestKeepAliveLoop:
    async def test_stops_when_the_connection_is_closed(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        """Called directly: in practice aclose() cancels it before it wakes."""
        config = NetworkConfig(keepalive_interval=0.01)
        connection = make_connection(seeder.address, context, config=config)
        await connection.connect()
        await connection.aclose()

        await asyncio.wait_for(connection._keepalive_loop(), timeout=2.0)

    async def test_gives_up_when_writing_fails(
        self, seeder: MockPeer, context: SwarmContext
    ) -> None:
        """A dead socket must not turn into an unhandled task exception."""
        config = NetworkConfig(keepalive_interval=0.01)
        connection = make_connection(seeder.address, context, config=config)
        await connection.connect()
        connection.stream = PeerStream(FailingReader(OSError()), FailingWriter(BrokenPipeError()))

        await asyncio.wait_for(connection._keepalive_loop(), timeout=2.0)
