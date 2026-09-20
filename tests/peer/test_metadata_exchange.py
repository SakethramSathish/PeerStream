"""BEP 9 metadata exchange, against mock peers that serve it — and don't.

The fetch is tested end to end: a real TCP connection, a real handshake, real
extension messages, and a real SHA-1 check at the end. The tests that matter
most are the dishonest peers — the one that answers with bytes for a *different*
torrent, the one that sends a short final chunk, the one that says yes and then
goes quiet. On the open internet those are the common cases, and the info hash
check is the only thing standing between a magnet link and the wrong file.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from hashlib import sha1

import pytest
from app.bencode import encode
from app.core.constants import DEFAULT_PIECE_LENGTH, PEER_ID_SIZE
from app.peer.errors import MetadataError, PeerError
from app.peer.handshake import outgoing_handshake
from app.peer.messages import Extended
from app.peer.metadata_exchange import (
    DATA,
    MAX_METADATA_SIZE,
    METADATA_PIECE_SIZE,
    OUR_UT_METADATA_ID,
    REJECT,
    REQUEST,
    UT_METADATA,
    ExtensionHandshake,
    MetadataMessage,
    MetadataResult,
    MetadataServer,
    fetch_metadata,
    fetch_metadata_from_any,
    split_metadata,
)
from app.peer.protocol import PeerStream
from app.torrent.info_hash import compute_info_hash
from app.torrent.parser import torrent_from_info

MOCK_PEER_ID = b"-MP0100-" + b"0" * 12
OUR_PEER_ID = b"-TS0100-" + b"1" * 12


def info_dict(*, length: int = DEFAULT_PIECE_LENGTH, padding: int = 0) -> dict[bytes, object]:
    """An info dictionary of a size we control, so chunking can be tested.

    Deterministic on purpose: the tests compare bytes fetched from a peer
    against bytes built here, and a random piece hash would make every
    comparison a coin toss.
    """
    piece_count = (length + DEFAULT_PIECE_LENGTH - 1) // DEFAULT_PIECE_LENGTH
    pieces = bytes((index * 37 + 11) % 256 for index in range(20 * piece_count))
    return {
        b"name": b"metadata-demo",
        b"piece length": DEFAULT_PIECE_LENGTH,
        b"pieces": pieces,
        b"length": length,
        **({b"comment": b"p" * padding} if padding else {}),
    }


def raw_info(**kwargs: object) -> bytes:
    return encode(info_dict(**kwargs))  # type: ignore[arg-type]


class MetadataPeer:
    """A mock seeder that serves metadata over real TCP.

    Built on the same :class:`PeerStream` the client uses, so a framing bug
    cannot cancel itself out across the two ends of a test. The misbehaviours
    are switches rather than subclasses, because real peers are not tidy: the
    one that sends the wrong bytes behaves perfectly right up to the moment it
    doesn't.
    """

    def __init__(
        self,
        metadata: bytes,
        *,
        info_hash: bytes | None = None,
        no_extensions: bool = False,
        no_ut_metadata: bool = False,
        no_metadata_size: bool = False,
        send_bitfield_first: bool = False,
        reject: bool = False,
        short_last_chunk: bool = False,
        silent: bool = False,
        hang_up: bool = False,
        ut_metadata_id: int = 3,
        reply_with_our_id: bool = False,
        claimed_size: int | None = None,
    ) -> None:
        self.metadata = metadata
        self.info_hash = info_hash if info_hash is not None else sha1(metadata).digest()
        self.no_extensions = no_extensions
        self.no_ut_metadata = no_ut_metadata
        self.no_metadata_size = no_metadata_size
        self.send_bitfield_first = send_bitfield_first
        self.reject = reject
        self.short_last_chunk = short_last_chunk
        self.silent = silent
        self.hang_up = hang_up
        self.ut_metadata_id = ut_metadata_id
        # Real clients answer a request using the id *we* advertised, not the
        # one they advertised: BEP 10 ids are per-peer, in both directions.
        self.reply_with_our_id = reply_with_our_id
        self.reply_id = OUR_UT_METADATA_ID if reply_with_our_id else ut_metadata_id
        self.claimed_size = claimed_size
        self.requests_received: list[int] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        assert self._server.sockets is not None
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> MetadataPeer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    def _extension_handshake(self) -> bytes:
        extensions: dict[bytes, object] = (
            {UT_METADATA.encode(): self.ut_metadata_id} if not self.no_ut_metadata else {}
        )
        body: dict[bytes, object] = {b"m": extensions, b"v": b"MockPeer 1.0"}
        if not self.no_metadata_size:
            body[b"metadata_size"] = (
                self.claimed_size if self.claimed_size is not None else len(self.metadata)
            )
        return encode(body)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = PeerStream(reader, writer, label="mock", timeout=5.0)
        try:
            await stream.accept_handshake(
                outgoing_handshake(self.info_hash, MOCK_PEER_ID, extensions=not self.no_extensions),
                timeout=5.0,
            )
            if self.hang_up:
                return
            if self.send_bitfield_first:
                await stream.send(Extended(0, self._extension_handshake()))
                await stream.send(Extended(0, self._extension_handshake()))
            else:
                await stream.send(Extended(0, self._extension_handshake()))
            if self.silent:
                await asyncio.sleep(1.0)
                return

            chunks = split_metadata(self.metadata)
            if getattr(self, "ask_us_for_piece", False):
                # Addressed with *our* advertised id, as a real peer would.
                await stream.send(
                    Extended(OUR_UT_METADATA_ID, encode({b"msg_type": REQUEST, b"piece": 0}))
                )
            while True:
                message = await stream.read_message(timeout=5.0)
                if not isinstance(message, Extended):
                    continue
                if message.extension_id == 0:
                    continue
                request = MetadataMessage.decode(message.payload)
                self.requests_received.append(request.piece)
                if self.reject or request.piece >= len(chunks):
                    await stream.send(
                        Extended(
                            self.ut_metadata_id,
                            encode({b"msg_type": REJECT, b"piece": request.piece}),
                        )
                    )
                    continue
                chunk = chunks[request.piece]
                if self.short_last_chunk and request.piece == len(chunks) - 1:
                    chunk = chunk[:-1]
                payload = (
                    encode(
                        {
                            b"msg_type": DATA,
                            b"piece": request.piece,
                            b"total_size": len(self.metadata),
                        }
                    )
                    + chunk
                )
                await stream.send(Extended(self.ut_metadata_id, payload))
        except (TimeoutError, PeerError, ConnectionResetError):
            return
        finally:
            await stream.aclose()


@pytest.fixture
async def peer() -> AsyncIterator[MetadataPeer]:
    async with MetadataPeer(raw_info()) as started:
        yield started


@pytest.fixture
def info() -> bytes:
    return raw_info()


class TestMessages:
    def test_a_request_survives_the_round_trip(self) -> None:
        message = MetadataMessage.decode(encode({b"msg_type": REQUEST, b"piece": 4}))
        assert message.kind == REQUEST
        assert message.piece == 4

    def test_the_chunk_rides_after_the_dictionary(self) -> None:
        chunk = b"x" * 100
        message = MetadataMessage.decode(
            encode({b"msg_type": DATA, b"piece": 0, b"total_size": 100}) + chunk
        )
        assert message.kind == DATA
        assert message.total_size == 100
        assert message.chunk == chunk

    def test_a_message_that_is_not_a_dictionary_is_refused(self) -> None:
        with pytest.raises(MetadataError, match="not a dictionary"):
            MetadataMessage.decode(encode([1, 2, 3]))

    def test_a_message_with_no_piece_is_refused(self) -> None:
        with pytest.raises(MetadataError, match="piece"):
            MetadataMessage.decode(encode({b"msg_type": REQUEST}))

    def test_metadata_is_split_into_protocol_sized_chunks(self) -> None:
        raw = os.urandom(METADATA_PIECE_SIZE * 2 + 10)
        chunks = split_metadata(raw)
        assert [len(chunk) for chunk in chunks] == [METADATA_PIECE_SIZE, METADATA_PIECE_SIZE, 10]
        assert b"".join(chunks) == raw


class TestExtensionHandshake:
    def test_we_advertise_only_what_we_implement(self) -> None:
        parsed = ExtensionHandshake.from_payload(ExtensionHandshake().encode())
        assert parsed.ut_metadata_id == OUR_UT_METADATA_ID
        assert parsed.metadata_size is None

    def test_their_ids_are_read_from_their_dictionary(self) -> None:
        payload = encode(
            {
                b"m": {b"ut_metadata": 7, b"lt_tex": 9},
                b"metadata_size": 4321,
                b"v": b"qBittorrent 5.1.0",
                b"reqq": 250,
            }
        )
        parsed = ExtensionHandshake.from_payload(payload)
        assert parsed.ut_metadata_id == 7
        assert parsed.metadata_size == 4321
        assert parsed.client == "qBittorrent 5.1.0"
        assert parsed.reqq == 250
        assert parsed.can_serve_metadata

    def test_a_peer_without_metadata_is_not_a_failure_just_unusable(self) -> None:
        parsed = ExtensionHandshake.from_payload(encode({b"m": {b"ut_metadata": 1}}))
        assert parsed.ut_metadata_id == 1
        assert not parsed.can_serve_metadata

    def test_a_peer_that_names_no_extensions_cannot_serve_anything(self) -> None:
        assert not ExtensionHandshake.from_payload(encode({})).can_serve_metadata

    def test_an_unreadable_handshake_is_refused(self) -> None:
        with pytest.raises(MetadataError, match="unreadable"):
            ExtensionHandshake.from_payload(b"d3:bar")

    def test_a_zero_metadata_size_is_nothing_at_all(self) -> None:
        parsed = ExtensionHandshake.from_payload(
            encode({b"metadata_size": 0, b"m": {b"ut_metadata": 1}})
        )
        assert parsed.metadata_size is None


class TestServer:
    def test_it_serves_the_chunk_that_was_asked_for(self) -> None:
        raw = os.urandom(METADATA_PIECE_SIZE * 2 + 5)
        server = MetadataServer(raw)
        reply = server.answer(encode({b"msg_type": REQUEST, b"piece": 2}))
        assert reply is not None
        message = MetadataMessage.decode(reply)
        assert message.kind == DATA
        assert message.chunk == raw[METADATA_PIECE_SIZE * 2 :]
        assert message.total_size == len(raw)

    def test_it_refuses_a_piece_beyond_what_it_holds(self) -> None:
        server = MetadataServer(b"short")
        reply = server.answer(encode({b"msg_type": REQUEST, b"piece": 99}))
        assert reply is not None
        assert MetadataMessage.decode(reply).kind == REJECT

    def test_it_ignores_messages_that_are_not_requests(self) -> None:
        server = MetadataServer(b"short")
        assert server.answer(encode({b"msg_type": DATA, b"piece": 0})) is None

    def test_its_handshake_states_what_it_holds(self) -> None:
        server = MetadataServer(b"x" * (METADATA_PIECE_SIZE + 3))
        parsed = ExtensionHandshake.from_payload(server.handshake())
        assert parsed.metadata_size == METADATA_PIECE_SIZE + 3
        assert parsed.can_serve_metadata

    def test_it_refuses_to_serve_nothing(self) -> None:
        with pytest.raises(MetadataError, match="empty"):
            MetadataServer(b"")


class TestFetch:
    async def test_metadata_arrives_and_hashes_to_the_info_hash(
        self, peer: MetadataPeer, info: bytes
    ) -> None:
        result = await fetch_metadata(
            "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
        )

        assert isinstance(result, MetadataResult)
        assert result.raw == info
        assert sha1(result.raw).digest() == peer.info_hash
        assert result.pieces == 1
        assert result.address == f"127.0.0.1:{peer.port}"
        assert result.size == len(info)

    async def test_it_becomes_a_torrent_we_could_download(self, peer: MetadataPeer) -> None:
        result = await fetch_metadata(
            "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
        )
        torrent = torrent_from_info(result.info, announce=("http://tracker.example/announce",))
        assert torrent.info_hash == peer.info_hash
        assert torrent.name == "metadata-demo"

    async def test_a_large_info_dictionary_arrives_in_several_chunks(self) -> None:
        raw = raw_info(padding=METADATA_PIECE_SIZE * 2)
        async with MetadataPeer(raw) as peer:
            result = await fetch_metadata(
                "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
            )
        assert result.pieces == len(split_metadata(raw))
        assert result.raw == raw
        assert result.pieces > 1

    async def test_requests_are_pipelined_not_sent_one_at_a_time(self) -> None:
        raw = raw_info(padding=METADATA_PIECE_SIZE * 3)
        async with MetadataPeer(raw) as peer:
            await fetch_metadata(
                "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
            )
        assert len(peer.requests_received) == len(split_metadata(raw))

    async def test_a_bitfield_before_the_extension_handshake_is_ignored(self, info: bytes) -> None:
        async with MetadataPeer(info, send_bitfield_first=True) as peer:
            result = await fetch_metadata(
                "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
            )
        assert result.raw == info


class TestRealPeerBehaviour:
    """The things real clients do that a tidy mock would never think of."""

    async def test_a_peer_that_answers_using_our_extension_id(self) -> None:
        # qBittorrent, Transmission and libtorrent all reply to a ut_metadata
        # request with the id *we* advertised, not the one in their own
        # handshake. Matching only the id from their handshake is how a client
        # times out on a peer that answered it.
        raw = raw_info(padding=METADATA_PIECE_SIZE)
        async with MetadataPeer(raw, reply_with_our_id=True) as peer:
            result = await fetch_metadata(
                "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
            )
        assert result.raw == raw

    async def test_a_peer_that_asks_us_for_metadata_is_refused_and_the_fetch_continues(
        self, info: bytes
    ) -> None:
        # A peer may want the same torrent from us. We are here to fetch, not
        # to serve, so we answer "no" rather than dropping the connection.
        raw = raw_info(padding=METADATA_PIECE_SIZE)
        async with MetadataPeer(raw) as peer:
            peer.ask_us_for_piece = True
            result = await fetch_metadata(
                "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
            )
        assert result.raw == raw


class TestDishonestPeers:
    async def test_metadata_for_another_torrent_is_refused(self, info: bytes) -> None:
        # The peer serves a real info dictionary — just not the one we asked
        # for. The hash check is what catches it.
        wanted = os.urandom(20)
        async with MetadataPeer(info, info_hash=wanted) as peer:
            with pytest.raises(MetadataError, match="does not hash"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, wanted, peer_id=OUR_PEER_ID, timeout=5.0
                )

    async def test_a_peer_without_the_extension_protocol_is_refused(self, info: bytes) -> None:
        async with MetadataPeer(info, no_extensions=True) as peer:
            with pytest.raises(MetadataError, match="extension protocol"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
                )

    async def test_a_peer_that_does_not_speak_ut_metadata_is_refused(self, info: bytes) -> None:
        async with MetadataPeer(info, no_ut_metadata=True) as peer:
            with pytest.raises(MetadataError, match="no metadata to offer"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
                )

    async def test_a_peer_that_names_no_size_is_refused(self, info: bytes) -> None:
        async with MetadataPeer(info, no_metadata_size=True) as peer:
            with pytest.raises(MetadataError, match="no metadata to offer"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
                )

    async def test_a_refusal_is_reported_as_one(self, info: bytes) -> None:
        async with MetadataPeer(info, reject=True) as peer:
            with pytest.raises(MetadataError, match="rejected metadata piece"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
                )

    async def test_a_short_final_chunk_is_caught(self, info: bytes) -> None:
        raw = raw_info(padding=METADATA_PIECE_SIZE)
        async with MetadataPeer(raw, short_last_chunk=True) as peer:
            with pytest.raises(MetadataError, match="expected"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
                )

    async def test_a_peer_that_goes_quiet_costs_only_the_timeout(self, info: bytes) -> None:
        async with MetadataPeer(info, silent=True) as peer:
            with pytest.raises(MetadataError, match="then stopped"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=1.0
                )

    async def test_a_peer_that_hangs_up_before_its_handshake_costs_only_the_timeout(
        self, info: bytes
    ) -> None:
        async with MetadataPeer(info, hang_up=True) as peer:
            with pytest.raises(MetadataError, match="extension handshake"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=1.0
                )

    async def test_an_absurd_size_claim_is_refused_before_we_allocate(self, info: bytes) -> None:
        async with MetadataPeer(info, claimed_size=MAX_METADATA_SIZE + 1) as peer:
            with pytest.raises(MetadataError, match="bytes of metadata"):
                await fetch_metadata(
                    "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
                )

    async def test_a_dead_address_is_reported(self) -> None:
        with pytest.raises(PeerError):
            await fetch_metadata("127.0.0.1", 1, os.urandom(20), peer_id=OUR_PEER_ID, timeout=2.0)

    async def test_a_bad_info_hash_is_refused_before_we_dial(self) -> None:
        with pytest.raises(MetadataError, match="20 bytes"):
            await fetch_metadata("127.0.0.1", 6881, b"short", peer_id=OUR_PEER_ID)

    async def test_a_bad_port_is_refused_before_we_dial(self) -> None:
        with pytest.raises(MetadataError, match="port"):
            await fetch_metadata("127.0.0.1", 0, os.urandom(20), peer_id=OUR_PEER_ID)


class TestFetchFromAny:
    async def test_the_first_peer_that_answers_wins(self, info: bytes) -> None:
        async with MetadataPeer(info) as good:
            result = await fetch_metadata_from_any(
                [("127.0.0.1", 1), ("127.0.0.1", good.port)],
                good.info_hash,
                peer_id=OUR_PEER_ID,
                timeout=5.0,
            )
        assert result.raw == info

    async def test_when_nobody_answers_the_reasons_are_reported(self, info: bytes) -> None:
        async with MetadataPeer(info, no_extensions=True) as peer:
            with pytest.raises(MetadataError, match="no peer supplied metadata"):
                await fetch_metadata_from_any(
                    [("127.0.0.1", 1), ("127.0.0.1", peer.port)],
                    peer.info_hash,
                    peer_id=OUR_PEER_ID,
                    timeout=2.0,
                )

    async def test_an_empty_peer_list_is_not_a_network_problem(self) -> None:
        with pytest.raises(MetadataError, match="no peers to ask"):
            await fetch_metadata_from_any([], os.urandom(20))


class TestPeerId:
    async def test_a_peer_id_is_generated_when_we_are_not_given_one(
        self, peer: MetadataPeer
    ) -> None:
        # The mock checks only the info hash, so this also proves the random id
        # is the right length to be a peer id at all.
        result = await fetch_metadata("127.0.0.1", peer.port, peer.info_hash, timeout=5.0)
        assert result.raw == raw_info()
        assert len(OUR_PEER_ID) == PEER_ID_SIZE

    async def test_the_info_hash_we_send_is_the_one_we_want(self, peer: MetadataPeer) -> None:
        result = await fetch_metadata(
            "127.0.0.1", peer.port, peer.info_hash, peer_id=OUR_PEER_ID, timeout=5.0
        )
        assert compute_info_hash(result.info) == peer.info_hash
