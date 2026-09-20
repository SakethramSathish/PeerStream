"""Integration tests for framed message I/O over real TCP sockets.

These tests exist because framing bugs hide from unit tests: a reader that
happens to receive whole messages in memory will happily work and then fail
against a peer whose TCP segments land differently. Everything here runs two
ends of a real connection.
"""

from __future__ import annotations

import pytest
from app.core.constants import HANDSHAKE_LENGTH, MAX_MESSAGE_LENGTH
from app.peer.errors import (
    HandshakeError,
    MessageError,
    PeerConnectionError,
    PeerDisconnected,
    PeerTimeoutError,
    ProtocolError,
)
from app.peer.handshake import Handshake, outgoing_handshake
from app.peer.messages import (
    Bitfield,
    Choke,
    Have,
    Interested,
    KeepAlive,
    Piece,
    Request,
    Unchoke,
    encode,
)
from app.peer.protocol import PeerStream

from tests.peer.conftest import (
    FAKE_PEER_ID,
    TEST_INFO_HASH,
    TEST_PEER_ID,
    FailingReader,
    FailingWriter,
    FakePeer,
    wait_until,
)


async def connected_stream(peer: FakePeer, **kwargs: object) -> PeerStream:
    """Open a stream to a running :class:`FakePeer`."""
    return await PeerStream.connect(peer.host, peer.port, **kwargs)  # type: ignore[arg-type]


class TestHandshake:
    async def test_exchange(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            peer = await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))

        assert peer.info_hash == TEST_INFO_HASH
        assert peer.peer_id == FAKE_PEER_ID
        assert stream.handshake_latency_ms is not None

    async def test_we_send_the_expected_bytes(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))

        assert fake_peer.received[:HANDSHAKE_LENGTH] == (
            outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID).encode()
        )

    async def test_wrong_info_hash_is_rejected(self) -> None:
        peer = FakePeer(info_hash=bytes(range(20, 40)))
        await peer.start()
        try:
            stream = await connected_stream(peer)
            async with stream:
                with pytest.raises(HandshakeError, match="info_hash mismatch"):
                    await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
        finally:
            await peer.stop()

    async def test_malformed_handshake_is_rejected(self) -> None:
        """A full 68 bytes that are not a handshake: the peer is not a peer."""
        peer = FakePeer(reply_to_handshake=False)
        await peer.start()
        try:
            stream = await connected_stream(peer)
            async with stream:
                await peer.write(b"X" * HANDSHAKE_LENGTH)
                with pytest.raises(HandshakeError, match="unsupported protocol"):
                    await stream.perform_handshake(
                        outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID), timeout=1.0
                    )
        finally:
            await peer.stop()

    async def test_truncated_handshake_is_a_disconnect(self) -> None:
        """Fewer than 68 bytes means the peer hung up, not that it spoke badly."""
        peer = FakePeer(reply_to_handshake=False)
        await peer.start()
        try:
            stream = await connected_stream(peer)
            async with stream:
                await peer.write(b"not a handshake")
                await peer.close_connection()
                with pytest.raises(PeerDisconnected):
                    await stream.perform_handshake(
                        outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID), timeout=1.0
                    )
        finally:
            await peer.stop()

    async def test_silent_peer_times_out(self) -> None:
        peer = FakePeer(reply_to_handshake=False)
        await peer.start()
        try:
            stream = await connected_stream(peer)
            async with stream:
                with pytest.raises(PeerTimeoutError, match="no data for"):
                    await stream.perform_handshake(
                        outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID), timeout=0.05
                    )
        finally:
            await peer.stop()

    async def test_peer_hanging_up_mid_handshake(self) -> None:
        peer = FakePeer(reply_to_handshake=False)
        await peer.start()
        try:
            stream = await connected_stream(peer)
            async with stream:
                await peer.close_connection()
                with pytest.raises(PeerDisconnected):
                    await stream.perform_handshake(
                        outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID), timeout=1.0
                    )
        finally:
            await peer.stop()

    async def test_connection_refused(self) -> None:
        # Port 1 is privileged: nothing is listening there.
        with pytest.raises(PeerConnectionError, match="cannot connect"):
            await PeerStream.connect("127.0.0.1", 1, timeout=1.0)


class TestFraming:
    async def test_a_single_message(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.send(Choke())
            assert isinstance(await stream.read_message(), Choke)

    async def test_partial_reads_are_reassembled(self, fake_peer: FakePeer) -> None:
        """A frame split across three TCP segments must still arrive whole."""
        frame = encode(Request(index=1, begin=2, length=16384))
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.write(frame[:2])
            await fake_peer.sleep(0.01)
            await fake_peer.write(frame[2:9])
            await fake_peer.sleep(0.01)
            await fake_peer.write(frame[9:])

            message = await stream.read_message(timeout=2.0)
        assert message == Request(index=1, begin=2, length=16384)

    async def test_several_messages_in_one_write(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.send_all([Choke(), Unchoke(), Have(index=7)])

            assert isinstance(await stream.read_message(), Choke)
            assert isinstance(await stream.read_message(), Unchoke)
            assert isinstance(await stream.read_message(), Have)

    async def test_keep_alive(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.write(b"\x00\x00\x00\x00")
            assert isinstance(await stream.read_message(), KeepAlive)

            await stream.send_keep_alive()
            await wait_until(lambda: len(fake_peer.received) > HANDSHAKE_LENGTH)
        assert fake_peer.received[HANDSHAKE_LENGTH:] == b"\x00\x00\x00\x00"

    async def test_oversized_length_is_refused_before_reading(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.write((MAX_MESSAGE_LENGTH + 1).to_bytes(4, "big"))
            with pytest.raises(ProtocolError, match="exceeds the"):
                await stream.read_message(timeout=2.0)

    async def test_custom_message_limit(self, fake_peer: FakePeer) -> None:
        stream = await PeerStream.connect(fake_peer.host, fake_peer.port, max_message_length=64)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.send(Bitfield(data=b"\xff" * 100))
            with pytest.raises(ProtocolError, match="exceeds the 64 byte limit"):
                await stream.read_message(timeout=2.0)

    async def test_unknown_message_id_from_the_wire(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.write(b"\x00\x00\x00\x01\x63")
            with pytest.raises(MessageError, match="unknown message id 99"):
                await stream.read_message(timeout=2.0)

    async def test_disconnect_mid_frame(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.write(b"\x00\x00\x00\x0d\x06\x00\x00")  # truncated request
            await fake_peer.close_connection()
            with pytest.raises(PeerDisconnected, match="closed after"):
                await stream.read_message(timeout=2.0)

    async def test_read_timeout(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            with pytest.raises(PeerTimeoutError, match="no data for"):
                await stream.read_message(timeout=0.05)


class TestSending:
    async def test_peer_receives_exactly_what_we_encoded(self, fake_peer: FakePeer) -> None:
        messages = [Interested(), Request(index=0, begin=0, length=16384)]
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            for message in messages:
                await stream.send(message)
            expected = b"".join(encode(message) for message in messages)
            await wait_until(lambda: fake_peer.received[HANDSHAKE_LENGTH:] == expected)

        assert fake_peer.received[HANDSHAKE_LENGTH:] == expected

    async def test_writes_after_close_are_refused(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
        await stream.aclose()
        with pytest.raises(PeerDisconnected, match="connection is closing"):
            await stream.send(Choke())


class TestCounters:
    async def test_bytes_are_counted_including_the_handshake(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await stream.send(Piece(index=0, begin=0, data=b"x" * 32))

        assert stream.bytes_sent == HANDSHAKE_LENGTH + 4 + 1 + 8 + 32
        assert stream.bytes_received == HANDSHAKE_LENGTH

    async def test_idle_time_updates(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.send(Choke())
            await stream.read_message()
            assert stream.idle_for < 1.0


class TestMessageIterator:
    async def test_iterates_until_the_peer_disconnects(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        seen: list[object] = []
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.send_all([Choke(), Unchoke()])
            await fake_peer.close_connection()
            async for message in stream.messages():
                seen.append(message)

        assert [type(message).__name__ for message in seen] == ["Choke", "Unchoke"]

    async def test_stops_when_we_close(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.send(Choke())
            await fake_peer.sleep(0.01)
            assert isinstance(await stream.read_message(), Choke)
            await stream.aclose()
            assert [message async for message in stream.messages()] == []


class TestLifecycle:
    async def test_aclose_is_idempotent(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
        await stream.aclose()
        await stream.aclose()
        assert stream.closed

    async def test_context_manager_closes(self, fake_peer: FakePeer) -> None:
        async with await connected_stream(fake_peer) as stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
        assert stream.closed

    async def test_label_and_string_form(self, fake_peer: FakePeer) -> None:
        stream = await connected_stream(fake_peer)
        async with stream:
            assert stream.label == f"{fake_peer.host}:{fake_peer.port}"
            assert str(stream) == f"<PeerStream {fake_peer.host}:{fake_peer.port}>"

    async def test_bytes_after_the_handshake_are_not_consumed(self, fake_peer: FakePeer) -> None:
        """The handshake helper must read exactly 68 bytes and leave the rest."""
        stream = await connected_stream(fake_peer)
        async with stream:
            await fake_peer.send(Choke())
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))

            # The peer received exactly our 68 bytes, and the message it queued
            # next is still waiting in the stream rather than swallowed.
            assert Handshake.decode(fake_peer.received[:HANDSHAKE_LENGTH]) == outgoing_handshake(
                TEST_INFO_HASH, TEST_PEER_ID
            )
            assert isinstance(await stream.read_message(timeout=2.0), Choke)


class TestErrorTranslation:
    """Socket failures must arrive as typed errors the connection layer can use."""

    async def test_read_error_becomes_a_disconnect(self) -> None:
        stream = PeerStream(FailingReader(ConnectionResetError()), FailingWriter(OSError()))
        with pytest.raises(PeerDisconnected, match="connection failed"):
            await stream.read_message(timeout=1.0)

    async def test_write_timeout_is_reported_as_a_timeout(self) -> None:
        stream = PeerStream(FailingReader(OSError()), FailingWriter(TimeoutError()))
        with pytest.raises(PeerTimeoutError, match="write timed out"):
            await stream.send(Choke())

    async def test_write_error_becomes_a_disconnect(self) -> None:
        stream = PeerStream(FailingReader(OSError()), FailingWriter(BrokenPipeError()))
        with pytest.raises(PeerDisconnected, match="write failed"):
            await stream.send(Choke())

    async def test_connect_timeout_is_reported_as_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def never_connects(*args: object, **kwargs: object) -> tuple[object, object]:
            raise TimeoutError

        monkeypatch.setattr("app.peer.protocol.asyncio.open_connection", never_connects)
        with pytest.raises(PeerTimeoutError, match="connect timed out"):
            await PeerStream.connect("10.0.0.1", 6881, timeout=0.01)

    async def test_a_zero_timeout_waits_for_data(self, fake_peer: FakePeer) -> None:
        """``timeout=0`` means no deadline at all, not an instant timeout."""
        stream = await connected_stream(fake_peer)
        async with stream:
            await stream.perform_handshake(outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID))
            await fake_peer.send(Choke())
            assert isinstance(await stream.read_message(timeout=0), Choke)
