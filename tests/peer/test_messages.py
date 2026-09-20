"""Unit tests for peer message encoding, decoding and validation."""

from __future__ import annotations

import pytest
from app.core.constants import (
    MAX_BLOCK_SIZE,
    MSG_BITFIELD,
    MSG_CANCEL,
    MSG_CHOKE,
    MSG_EXTENDED,
    MSG_HAVE,
    MSG_PIECE,
    MSG_PORT,
    MSG_REQUEST,
    MSG_UNCHOKE,
)
from app.peer.errors import MessageError
from app.peer.messages import (
    Bitfield,
    Cancel,
    Choke,
    Extended,
    Have,
    Interested,
    KeepAlive,
    Message,
    NotInterested,
    Piece,
    Port,
    Request,
    Unchoke,
    decode,
    encode,
    message_name,
)

SAMPLE_MESSAGES: tuple[Message, ...] = (
    KeepAlive(),
    Choke(),
    Unchoke(),
    Interested(),
    NotInterested(),
    Have(index=42),
    Bitfield(data=b"\xf0\x0f"),
    Request(index=1, begin=2, length=16384),
    Piece(index=1, begin=2, data=b"\x00\xff\x10"),
    Cancel(index=1, begin=2, length=16384),
    Port(port=6881),
)


class TestRoundTrips:
    @pytest.mark.parametrize("message", SAMPLE_MESSAGES, ids=message_name)
    def test_encode_then_decode(self, message: Message) -> None:
        frame = encode(message)
        length = int.from_bytes(frame[:4], "big")
        assert length == len(frame) - 4
        assert decode(frame[4:]) == message

    def test_keep_alive_is_four_zero_bytes(self) -> None:
        assert encode(KeepAlive()) == b"\x00\x00\x00\x00"

    def test_empty_body_decodes_to_keep_alive(self) -> None:
        assert decode(b"") == KeepAlive()

    def test_fixed_size_messages_are_one_byte(self) -> None:
        assert encode(Choke()) == b"\x00\x00\x00\x01\x00"
        assert encode(Unchoke()) == b"\x00\x00\x00\x01\x01"
        assert encode(Interested()) == b"\x00\x00\x00\x01\x02"
        assert encode(NotInterested()) == b"\x00\x00\x00\x01\x03"

    def test_have_is_big_endian(self) -> None:
        assert encode(Have(index=0x01020304)) == b"\x00\x00\x00\x05\x04\x01\x02\x03\x04"

    def test_request_is_big_endian(self) -> None:
        frame = encode(Request(index=1, begin=2, length=3))
        assert frame == b"\x00\x00\x00\r\x06\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x03"

    def test_piece_carries_arbitrary_binary(self) -> None:
        data = bytes(range(256))
        decoded = decode(encode(Piece(index=5, begin=0, data=data))[4:])
        assert isinstance(decoded, Piece)
        assert decoded.data == data

    def test_bitfield_payload_is_preserved(self) -> None:
        decoded = decode(encode(Bitfield(data=b"\x80\x40\x20"))[4:])
        assert isinstance(decoded, Bitfield)
        assert decoded.data == b"\x80\x40\x20"

    def test_port_message(self) -> None:
        decoded = decode(encode(Port(port=51413))[4:])
        assert isinstance(decoded, Port)
        assert decoded.port == 51413


class TestDecodingErrors:
    def test_unknown_id(self) -> None:
        with pytest.raises(MessageError, match="unknown message id 99"):
            decode(b"\x63\x00")

    @pytest.mark.parametrize(
        ("message_id", "name"),
        [
            (MSG_CHOKE, "choke"),
            (MSG_UNCHOKE, "unchoke"),
            (2, "interested"),
            (3, "not_interested"),
        ],
    )
    def test_rejects_payload_on_empty_messages(self, message_id: int, name: str) -> None:
        with pytest.raises(MessageError, match=f"{name} message must carry 0 payload bytes"):
            decode(bytes((message_id,)) + b"\x00")

    def test_rejects_short_have(self) -> None:
        with pytest.raises(MessageError, match="have message must carry 4 payload bytes"):
            decode(b"\x04\x00\x00\x00")

    def test_rejects_empty_bitfield(self) -> None:
        with pytest.raises(MessageError, match="bitfield payload must not be empty"):
            decode(bytes((MSG_BITFIELD,)))

    def test_rejects_short_request(self) -> None:
        with pytest.raises(MessageError, match="request message must carry 12 payload bytes"):
            decode(bytes((MSG_REQUEST,)) + b"\x00" * 11)

    def test_rejects_short_cancel(self) -> None:
        with pytest.raises(MessageError, match="cancel message must carry 12 payload bytes"):
            decode(bytes((MSG_CANCEL,)) + b"\x00" * 11)

    def test_rejects_piece_without_data(self) -> None:
        with pytest.raises(MessageError, match="piece message carries no block data"):
            decode(bytes((MSG_PIECE,)) + b"\x00" * 8)

    def test_rejects_short_port(self) -> None:
        with pytest.raises(MessageError, match="port message must carry 2 payload bytes"):
            decode(bytes((MSG_PORT,)) + b"\x00")


class TestConstructionValidation:
    def test_rejects_negative_piece_index(self) -> None:
        with pytest.raises(MessageError, match="must not be negative"):
            Have(index=-1)

    @pytest.mark.parametrize("length", [0, -1, MAX_BLOCK_SIZE + 1])
    def test_rejects_invalid_request_lengths(self, length: int) -> None:
        with pytest.raises(MessageError, match="request length"):
            Request(index=0, begin=0, length=length)

    @pytest.mark.parametrize("length", [0, MAX_BLOCK_SIZE + 1])
    def test_rejects_invalid_cancel_lengths(self, length: int) -> None:
        with pytest.raises(MessageError, match="cancel length"):
            Cancel(index=0, begin=0, length=length)

    def test_rejects_negative_request_offsets(self) -> None:
        with pytest.raises(MessageError, match="block offset must not be negative"):
            Request(index=0, begin=-1, length=16)

    def test_rejects_empty_piece_data(self) -> None:
        with pytest.raises(MessageError, match="piece payload must not be empty"):
            Piece(index=0, begin=0, data=b"")

    def test_rejects_oversized_piece_data(self) -> None:
        with pytest.raises(MessageError, match="exceeds the"):
            Piece(index=0, begin=0, data=b"x" * (MAX_BLOCK_SIZE + 1))

    def test_rejects_negative_piece_offsets(self) -> None:
        with pytest.raises(MessageError, match="block offset must not be negative"):
            Piece(index=0, begin=-1, data=b"x")

    @pytest.mark.parametrize("port", [0, 65536])
    def test_rejects_invalid_dht_ports(self, port: int) -> None:
        with pytest.raises(MessageError, match="is outside 1-65535"):
            Port(port=port)

    def test_rejects_empty_bitfield_payload(self) -> None:
        with pytest.raises(MessageError, match="bitfield payload must not be empty"):
            Bitfield(data=b"")


class TestMessageNames:
    def test_names_a_message_object(self) -> None:
        assert message_name(Choke()) == "choke"
        assert message_name(KeepAlive()) == "keepalive"

    def test_names_a_message_id(self) -> None:
        assert message_name(MSG_HAVE) == "have"

    def test_names_an_unknown_id(self) -> None:
        assert message_name(200) == "unknown(200)"

    def test_string_forms_are_readable(self) -> None:
        assert str(Choke()) == "Choke"
        assert str(Have(index=3)) == "Have(index=3)"
        assert str(Piece(index=1, begin=2, data=b"abc")) == "Piece(index=1, begin=2, length=3)"
        assert str(Request(index=1, begin=2, length=3)) == "Request(index=1, begin=2, length=3)"
        assert str(Cancel(index=1, begin=2, length=3)) == "Cancel(index=1, begin=2, length=3)"
        assert str(Interested()) == "Interested"
        assert str(NotInterested()) == "NotInterested"
        assert str(Unchoke()) == "Unchoke"
        assert str(KeepAlive()) == "KeepAlive"
        assert str(Port(port=6881)) == "Port(port=6881)"
        assert str(Bitfield(data=b"\xff")) == "Bitfield(1 bytes)"


class TestPeerSuppliedValues:
    """A peer can send anything; none of it may escape validation."""

    def test_huge_request_length_is_rejected(self) -> None:
        payload = (0).to_bytes(4, "big") + (0).to_bytes(4, "big") + (0xFFFFFFFF).to_bytes(4, "big")
        with pytest.raises(MessageError, match="request length"):
            decode(bytes((MSG_REQUEST,)) + payload)

    def test_index_range_is_checked_by_the_session_not_the_message(self) -> None:
        """A message cannot know the torrent's piece count.

        0xFFFFFFFF decodes fine here; rejecting it is the session's job, which
        does know how many pieces exist (see ``tests/peer/test_state.py``).
        """
        decoded = decode(bytes((MSG_HAVE,)) + b"\xff\xff\xff\xff")
        assert isinstance(decoded, Have)
        assert decoded.index == 0xFFFFFFFF


class TestMoreValidation:
    def test_rejects_negative_piece_index_on_a_piece_message(self) -> None:
        with pytest.raises(MessageError, match="piece index must not be negative"):
            Piece(index=-1, begin=0, data=b"x")

    def test_rejects_negative_index_on_a_request(self) -> None:
        with pytest.raises(MessageError, match="piece index must not be negative"):
            Request(index=-1, begin=0, length=16)

    def test_rejects_negative_index_on_a_cancel(self) -> None:
        with pytest.raises(MessageError, match="piece index must not be negative"):
            Cancel(index=-1, begin=0, length=16)


class TestExtended:
    """The BEP 10 message that carries every other extension.

    Its id byte is chosen by whoever sends it, so the message is a container:
    one byte naming the extension, then whatever that extension means. The only
    thing we can validate is that the byte exists.
    """

    def test_round_trip_keeps_the_id_and_the_body(self) -> None:
        payload = b"d1:md11:ut_metadatai1eee"
        decoded = decode(bytes((MSG_EXTENDED, 0)) + payload)
        assert isinstance(decoded, Extended)
        assert decoded.extension_id == 0
        assert decoded.payload == payload

    def test_an_extension_id_is_arbitrary(self) -> None:
        decoded = decode(bytes((MSG_EXTENDED, 200, 1, 2, 3)))
        assert isinstance(decoded, Extended)
        assert decoded.extension_id == 200
        assert decoded.payload == b"\x01\x02\x03"

    def test_encoding_is_a_framed_id_byte_then_the_body(self) -> None:
        assert (
            encode(Extended(3, b"abc")) == b"\x00\x00\x00\x05" + bytes((MSG_EXTENDED, 3)) + b"abc"
        )

    def test_a_message_naming_no_extension_is_refused(self) -> None:
        with pytest.raises(MessageError, match="names no extension"):
            decode(bytes((MSG_EXTENDED,)))

    def test_an_id_outside_a_byte_is_refused(self) -> None:
        with pytest.raises(MessageError, match="outside 0-255"):
            Extended(256)

    def test_the_message_names_itself_for_the_log(self) -> None:
        assert message_name(MSG_EXTENDED) == "extended"
        assert str(Extended(1, b"1234")) == "Extended(id=1, 4 bytes)"
