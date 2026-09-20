"""Peer exchange through the manager, against real seeders on loopback.

The codec and the ledger have their own tests. What is checked here is the
policy the manager owns: which peers it is willing to vouch for, which peers it
is willing to listen to, and what happens when the torrent is private and the
answer to both is "nobody".
"""

from __future__ import annotations

import pytest
from app.core.config import NetworkConfig
from app.core.event_bus import EventBus
from app.discovery.pex import FLAG_REACHABLE, FLAG_SEED, UT_PEX, decode_pex
from app.peer.connection import SwarmContext
from app.peer.discovery.peer_manager import PeerManager
from app.tracker.base import PeerAddress

from tests.mocks.mock_peer import MockPeer
from tests.peer.conftest import TEST_INFO_HASH, TEST_PEER_ID, wait_until

PIECE_LENGTH = 16 * 1024
OUR_PEX_ID = 1
THEIR_PEX_ID = 5


@pytest.fixture
def context(payload: bytes) -> SwarmContext:
    return SwarmContext(
        info_hash=TEST_INFO_HASH,
        piece_count=(len(payload) + PIECE_LENGTH - 1) // PIECE_LENGTH,
        piece_length=PIECE_LENGTH,
        name="test.bin",
    )


@pytest.fixture
def private_context(payload: bytes) -> SwarmContext:
    return SwarmContext(
        info_hash=TEST_INFO_HASH,
        piece_count=(len(payload) + PIECE_LENGTH - 1) // PIECE_LENGTH,
        piece_length=PIECE_LENGTH,
        name="private.bin",
        private=True,
    )


def make_manager(context: SwarmContext, **kwargs: object) -> PeerManager:
    return PeerManager(
        context,
        peer_id=TEST_PEER_ID,
        config=NetworkConfig(),
        event_bus=EventBus(),
        **kwargs,  # type: ignore[arg-type]
    )


async def start_seeder(payload: bytes, **kwargs: object) -> MockPeer:
    peer = MockPeer(payload, info_hash=TEST_INFO_HASH, piece_length=PIECE_LENGTH, **kwargs)  # type: ignore[arg-type]
    await peer.start()
    return peer


def pex_seeder_kwargs(peers: tuple[PeerAddress, ...] = ()) -> dict[str, object]:
    return {"extensions": {UT_PEX: THEIR_PEX_ID}, "pex_peers": peers}


def pex_received(peer: MockPeer) -> bytes:
    """The body of the ``ut_pex`` message this peer got, waiting for it to land.

    ``exchange_peers`` returning is not the same moment as the peer's read loop
    having the bytes, so the assertion waits rather than races.
    """
    bodies = [body for identifier, body in peer.extension_messages if identifier != 0]
    if not bodies:
        raise AssertionError(f"{peer.address} was never sent a ut_pex message")
    return bodies[0]


async def wait_for_pex(peer: MockPeer) -> bytes:
    """Wait for the peer's read loop to have the message, then hand it back."""
    await wait_until(lambda: any(identifier != 0 for identifier, _ in peer.extension_messages))
    return pex_received(peer)


async def connect(manager: PeerManager, peer: MockPeer) -> None:
    await manager.connect_to(peer.address)


async def connected_and_negotiated(manager: PeerManager, count: int) -> None:
    """Wait until every connection has finished the BEP 10 handshake.

    A manager that sends before then would have to guess the peer's ids, so
    "two connections" and "two peers we can send to" are different moments.
    """
    await wait_until(
        lambda: (
            len(manager.connections) == count
            and all(connection.extensions.can_send(UT_PEX) for connection in manager.connections)
        )
    )


class TestPolicy:
    def test_an_open_torrent_exchanges_peers(self, context: SwarmContext) -> None:
        assert make_manager(context).pex_enabled is True

    def test_a_private_torrent_does_not(self, private_context: SwarmContext) -> None:
        # BEP 27. The swarm is closed on purpose; volunteering its members to
        # strangers would undo the one thing the flag asks for.
        assert make_manager(private_context).pex_enabled is False

    async def test_a_private_torrent_offers_no_extension_at_all(
        self, payload: bytes, private_context: SwarmContext
    ) -> None:
        manager = make_manager(private_context)
        peer = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, peer)
            await wait_until(lambda: bool(manager.connections))

            connection = manager.connections[0]
            assert connection.extensions.ours == {}, "nothing advertised, so nothing to send"
            assert peer.client_supports_extensions is False
            assert await manager.exchange_peers() == 0
        finally:
            await manager.close_all()
            await peer.stop()

    async def test_an_open_torrent_advertises_ut_pex(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        manager = make_manager(context)
        peer = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, peer)
            await wait_until(lambda: bool(peer.their_extensions))

            assert peer.their_extensions == {UT_PEX: OUR_PEX_ID}
        finally:
            await manager.close_all()
            await peer.stop()


class TestWhatWeAdvertise:
    async def test_a_connected_peer_is_advertised_to_the_others(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        manager = make_manager(context)
        first = await start_seeder(payload, **pex_seeder_kwargs())
        second = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, first)
            await connect(manager, second)
            await connected_and_negotiated(manager, 2)

            sent = await manager.exchange_peers()

            assert sent == 2, "both peers speak ut_pex"
            told = decode_pex(await wait_for_pex(first))
            assert [peer.port for peer in told.added] == [second.port]
            assert all(peer.source == "pex" for peer in told.added)
        finally:
            await manager.close_all()
            await first.stop()
            await second.stop()

    async def test_nobody_is_told_about_itself(self, payload: bytes, context: SwarmContext) -> None:
        manager = make_manager(context)
        first = await start_seeder(payload, **pex_seeder_kwargs())
        second = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, first)
            await connect(manager, second)
            await connected_and_negotiated(manager, 2)

            await manager.exchange_peers()

            for peer in (first, second):
                told = decode_pex(await wait_for_pex(peer))
                assert peer.port not in [address.port for address in told.added]
        finally:
            await manager.close_all()
            await first.stop()
            await second.stop()

    async def test_a_candidate_we_never_dialled_is_not_advertised(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        # BEP 11 wants live connections, not a list of addresses somebody gave
        # us. Advertising unverified candidates is how PEX becomes a way to aim
        # a swarm at a third party.
        manager = make_manager(context)
        peer = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            manager.add_peers([PeerAddress(host="203.0.113.50", port=6881)], source="tracker")
            await connect(manager, peer)
            await wait_until(lambda: bool(manager.connections))

            assert await manager.exchange_peers() == 0, "one peer, told about nobody"
        finally:
            await manager.close_all()
            await peer.stop()

    async def test_a_seeder_is_flagged_as_one(self, payload: bytes, context: SwarmContext) -> None:
        manager = make_manager(context)
        seeder = await start_seeder(payload, **pex_seeder_kwargs())
        listener = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, seeder)
            await connect(manager, listener)
            await connected_and_negotiated(manager, 2)
            await wait_until(
                lambda: all(connection.session.is_seed for connection in manager.connections)
            )

            await manager.exchange_peers()

            told = decode_pex(await wait_for_pex(listener))
            assert told.flags == bytes([FLAG_SEED | FLAG_REACHABLE]), (
                "we dialled it, it answered, and its bitfield is complete"
            )
        finally:
            await manager.close_all()
            await seeder.stop()
            await listener.stop()

    async def test_the_interval_is_respected_across_passes(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        manager = make_manager(context)
        first = await start_seeder(payload, **pex_seeder_kwargs())
        second = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, first)
            await connect(manager, second)
            await connected_and_negotiated(manager, 2)

            assert await manager.exchange_peers() == 2
            await wait_for_pex(first)
            await wait_for_pex(second)

            third = await start_seeder(payload, **pex_seeder_kwargs())
            await connect(manager, third)
            await connected_and_negotiated(manager, 3)

            # One message, to the peer that has never had one. The minute is per
            # peer, so the two that were just told are not told again.
            assert await manager.exchange_peers() == 1
            await wait_for_pex(third)
            assert await manager.exchange_peers() == 0
            assert len([pair for pair in first.extension_messages if pair[0] != 0]) == 1
        finally:
            await manager.close_all()
            await first.stop()
            await second.stop()
            await third.stop()


class TestWhatWeAccept:
    async def test_a_peers_contacts_become_candidates(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        offered = PeerAddress(host="203.0.113.9", port=6881)
        manager = make_manager(context)
        peer = await start_seeder(payload, **pex_seeder_kwargs((offered,)))
        try:
            await connect(manager, peer)

            await wait_until(lambda: manager.pex_received > 0)

            assert manager.pex_received == 1
            sources = {candidate.address.source for candidate in manager.candidates}
            assert sources == {"pex"}
            assert [candidate.address.host for candidate in manager.candidates] == ["203.0.113.9"]
        finally:
            await manager.close_all()
            await peer.stop()

    async def test_a_duplicate_host_is_taken_once(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        # BEP 11's security note: one host on many ports is how a peer turns a
        # single address into a hundred dial attempts.
        offered = tuple(PeerAddress(host="203.0.113.9", port=port) for port in (6881, 6882, 6883))
        manager = make_manager(context)
        peer = await start_seeder(payload, **pex_seeder_kwargs(offered))
        try:
            await connect(manager, peer)

            await wait_until(lambda: manager.pex_received >= 3)

            assert len(manager.candidates) == 1
        finally:
            await manager.close_all()
            await peer.stop()

    async def test_an_unreadable_message_costs_nothing(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        manager = make_manager(context)
        peer = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, peer)
            await wait_until(lambda: bool(manager.connections))
            connection = manager.connections[0]

            manager._on_extension(connection, UT_PEX, b"d5:added3:xy")

            assert manager.pex_received == 0
            assert connection.closed is False, "a bad message is not a bad peer"
        finally:
            await manager.close_all()
            await peer.stop()

    async def test_an_extension_we_do_not_know_is_ignored(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        manager = make_manager(context)
        peer = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, peer)
            await wait_until(lambda: bool(manager.connections))
            connection = manager.connections[0]

            manager._on_extension(connection, "lt_donthave", b"de")

            assert manager.pex_received == 0
            assert connection.closed is False
        finally:
            await manager.close_all()
            await peer.stop()

    async def test_a_peer_that_left_is_retracted_from_the_others(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        manager = make_manager(context)
        first = await start_seeder(payload, **pex_seeder_kwargs())
        second = await start_seeder(payload, **pex_seeder_kwargs())
        try:
            await connect(manager, first)
            await connect(manager, second)
            await connected_and_negotiated(manager, 2)
            await manager.exchange_peers()

            # The ledger for a connection that goes away is dropped with it; the
            # other ledgers learn from their next snapshot.
            departing = manager.connection_for(second.address)
            assert departing is not None
            await departing.aclose(reason="test")
            await wait_until(lambda: len(manager.connections) == 1)

            assert manager._pex.get(second.address.address) is None
        finally:
            await manager.close_all()
            await first.stop()
            await second.stop()
