"""Extension negotiation on a live connection (BEP 10), over real TCP.

The parsing has its own tests; these are about the conversation, and the two
things that only a real socket can prove: that we set the reserved bit only when
we have something to offer, and that a message we send uses the id the *peer*
chose rather than the one we chose. The second is the bug that makes an
extension silently do nothing, because both ends encode and decode perfectly
and never understand each other.
"""

from __future__ import annotations

import asyncio

import pytest
from app.discovery.pex import UT_PEX, PexContact, decode_pex, encode_pex
from app.peer.connection import PeerConnection, SwarmContext
from app.peer.extension import HANDSHAKE_ID, UT_METADATA
from app.peer.state import ConnectionState
from app.tracker.base import PeerAddress

from tests.mocks.mock_peer import MockPeer
from tests.peer.conftest import TEST_INFO_HASH, TEST_PEER_ID, wait_until

PIECE_LENGTH = 16 * 1024
OUR_PEX_ID = 1
THEIR_PEX_ID = 5


@pytest.fixture
def payload() -> bytes:
    return bytes(range(64)) * 512


@pytest.fixture
def context(payload: bytes) -> SwarmContext:
    return SwarmContext(
        info_hash=TEST_INFO_HASH,
        piece_count=(len(payload) + PIECE_LENGTH - 1) // PIECE_LENGTH,
        piece_length=PIECE_LENGTH,
        name="test.bin",
    )


def private_context(context: SwarmContext) -> SwarmContext:
    return SwarmContext(
        info_hash=context.info_hash,
        piece_count=context.piece_count,
        piece_length=context.piece_length,
        name=context.name,
        private=True,
    )


async def build_connection(
    peer: MockPeer,
    context: SwarmContext,
    *,
    extensions: dict[str, int] | None = None,
    on_extension: object = None,
) -> PeerConnection:
    """A connected, running client connection to ``peer``."""
    connection = PeerConnection(
        peer.address,
        context,
        peer_id=TEST_PEER_ID,
        extensions=extensions,  # type: ignore[arg-type]
        on_extension=on_extension,  # type: ignore[arg-type]
    )
    await connection.connect()
    connection.start()
    return connection


class TestAdvertising:
    async def test_the_extension_bit_is_set_when_we_have_an_extension(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
        ) as peer:
            connection = await build_connection(peer, context, extensions={UT_PEX: OUR_PEX_ID})
            try:
                await wait_until(lambda: peer.client_supports_extensions)
                assert peer.client_supports_extensions is True
            finally:
                await connection.aclose()

    async def test_no_extensions_means_no_extension_bit(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        # A client with nothing to offer must not invite a handshake it will not
        # answer. Advertising and then ignoring is how peers end up dropping us.
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
        ) as peer:
            connection = await build_connection(peer, context)
            try:
                await wait_until(lambda: connection.session.state is ConnectionState.CONNECTED)
                assert peer.client_supports_extensions is False
                assert peer.their_extensions == {}
            finally:
                await connection.aclose()

    def test_the_private_flag_travels_with_the_context(self, context: SwarmContext) -> None:
        # A connection does what it is told; refusing PEX for a private torrent
        # is the peer manager's decision, and it needs this flag to make it.
        # BEP 27: a closed swarm stays closed.
        assert context.private is False
        assert private_context(context).private is True

    async def test_our_handshake_names_what_we_answer_and_who_we_are(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
        ) as peer:
            connection = await build_connection(
                peer, context, extensions={UT_PEX: OUR_PEX_ID, UT_METADATA: 2}
            )
            try:
                await wait_until(lambda: bool(peer.their_extensions))
                assert peer.their_extensions == {UT_PEX: OUR_PEX_ID, UT_METADATA: 2}
                assert "bittorrent-client" in peer.client_version
            finally:
                await connection.aclose()

    async def test_a_peer_that_does_not_speak_extensions_is_still_usable(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        async with MockPeer(payload, info_hash=TEST_INFO_HASH, piece_length=PIECE_LENGTH) as peer:
            connection = await build_connection(peer, context, extensions={UT_PEX: OUR_PEX_ID})
            try:
                await wait_until(lambda: connection.session.bitfield.count > 0)
                assert connection.extensions.negotiated is False
                assert connection.session.bitfield.complete, "it is still a seeder"
                assert await connection.send_extension(UT_PEX, b"de") is False
            finally:
                await connection.aclose()


class TestReceiving:
    async def test_their_handshake_is_recorded(self, payload: bytes, context: SwarmContext) -> None:
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
        ) as peer:
            connection = await build_connection(peer, context, extensions={UT_PEX: OUR_PEX_ID})
            try:
                await wait_until(lambda: connection.extensions.negotiated)
                assert connection.extensions.their_id(UT_PEX) == THEIR_PEX_ID
                assert connection.extensions.our_id(UT_PEX) == OUR_PEX_ID
                assert connection.extensions.shared() == (UT_PEX,)
                assert connection.extensions.version == "mock-peer 1.0"
            finally:
                await connection.aclose()

    async def test_an_extension_message_arrives_as_a_name(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        # The id on the wire is ours; the callback gets the name, so no caller
        # ever has to remember which number meant what.
        heard: list[tuple[str, bytes]] = []
        offered = PeerAddress(host="203.0.113.9", port=6881)

        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
            pex_peers=[offered],
        ) as peer:
            connection = await build_connection(
                peer,
                context,
                extensions={UT_PEX: OUR_PEX_ID},
                on_extension=lambda conn, name, body: heard.append((name, body)),
            )
            try:
                await wait_until(lambda: bool(heard))
                name, body = heard[0]
                assert name == UT_PEX
                assert [peer_address.port for peer_address in decode_pex(body).added] == [6881]
            finally:
                await connection.aclose()

    async def test_an_id_we_never_advertised_is_ignored(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        heard: list[tuple[str, bytes]] = []

        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
        ) as peer:
            connection = await build_connection(
                peer,
                context,
                extensions={UT_PEX: OUR_PEX_ID},
                on_extension=lambda conn, name, body: heard.append((name, body)),
            )
            try:
                await wait_until(lambda: connection.extensions.negotiated)
                # A peer guessing our numbering: id 9 was never offered.
                from app.peer.messages import Extended

                await connection.stream.send(Extended(9, b"de"))  # type: ignore[union-attr]
                await asyncio.sleep(0.2)

                assert heard == []
                assert connection.closed is False, "confusion is not a protocol violation"
            finally:
                await connection.aclose()

    async def test_a_garbled_handshake_costs_the_extensions_not_the_connection(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
            malformed_extension=True,
        ) as peer:
            connection = await build_connection(peer, context, extensions={UT_PEX: OUR_PEX_ID})
            try:
                await wait_until(lambda: connection.session.bitfield.count > 0)
                assert connection.extensions.negotiated is False
                assert connection.closed is False
                assert connection.session.is_seed, "the pieces still arrive"
            finally:
                await connection.aclose()


class TestSending:
    async def test_a_message_goes_out_with_their_id(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        # The single most common BEP 10 bug, checked end to end: our ut_pex is
        # id 1, theirs is 5, and the bytes on the wire must say 5.
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
        ) as peer:
            connection = await build_connection(peer, context, extensions={UT_PEX: OUR_PEX_ID})
            try:
                await wait_until(lambda: connection.extensions.negotiated)
                body = encode_pex(added=[PexContact(address=peer.address, reachable=True)])

                assert await connection.send_extension(UT_PEX, body) is True
                # The handshake is id 0 and arrives first; wait for the message
                # that is not the negotiation.
                await wait_until(
                    lambda: any(
                        identifier != HANDSHAKE_ID for identifier, _ in peer.extension_messages
                    )
                )

                identifier, sent = next(
                    pair for pair in peer.extension_messages if pair[0] != HANDSHAKE_ID
                )
                assert identifier == THEIR_PEX_ID, "their id, not ours"
                assert decode_pex(sent).added[0].port == peer.address.port
            finally:
                await connection.aclose()

    async def test_an_extension_they_did_not_advertise_cannot_be_sent(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_METADATA: 3},
        ) as peer:
            connection = await build_connection(
                peer, context, extensions={UT_PEX: OUR_PEX_ID, UT_METADATA: 2}
            )
            try:
                await wait_until(lambda: connection.extensions.negotiated)
                assert await connection.send_extension(UT_PEX, b"de") is False
                assert connection.closed is False
            finally:
                await connection.aclose()

    async def test_sending_before_their_handshake_arrives_is_refused(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
        ) as peer:
            connection = PeerConnection(
                peer.address, context, peer_id=TEST_PEER_ID, extensions={UT_PEX: OUR_PEX_ID}
            )
            await connection.connect()
            try:
                # Not started, so nothing has read their handshake yet.
                assert connection.extensions.can_send(UT_PEX) is False
                assert await connection.send_extension(UT_PEX, b"de") is False
            finally:
                await connection.aclose()

    async def test_the_handshake_id_is_never_reused_for_an_extension(
        self, payload: bytes, context: SwarmContext
    ) -> None:
        # Id 0 is the negotiation itself, so an extension numbered 0 would be
        # indistinguishable from a second handshake.
        assert HANDSHAKE_ID == 0
        async with MockPeer(
            payload,
            info_hash=TEST_INFO_HASH,
            piece_length=PIECE_LENGTH,
            extensions={UT_PEX: THEIR_PEX_ID},
        ) as peer:
            connection = await build_connection(peer, context, extensions={UT_PEX: OUR_PEX_ID})
            try:
                await wait_until(lambda: connection.extensions.negotiated)
                assert HANDSHAKE_ID not in connection.extensions.theirs.values()
                assert HANDSHAKE_ID not in connection.extensions.ours.values()
                assert [identifier for identifier, _ in peer.extension_messages] == [
                    HANDSHAKE_ID
                ], "the only extension message we sent was the negotiation"
            finally:
                await connection.aclose()
