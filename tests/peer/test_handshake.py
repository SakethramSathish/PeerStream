"""Unit tests for the 68-byte handshake."""

from __future__ import annotations

import pytest
from app.core.constants import HANDSHAKE_LENGTH, PROTOCOL_STRING
from app.peer.errors import HandshakeError
from app.peer.handshake import Handshake, outgoing_handshake

from tests.peer.conftest import FAKE_PEER_ID, TEST_INFO_HASH, TEST_PEER_ID

OTHER_INFO_HASH = bytes(range(20, 40))


def handshake_bytes(
    *, pstrlen: int = 19, pstr: bytes = PROTOCOL_STRING, info_hash: bytes = TEST_INFO_HASH
) -> bytes:
    """Assemble raw handshake bytes with any field broken on purpose."""
    return bytes((pstrlen,)) + pstr + bytes(8) + info_hash + TEST_PEER_ID


class TestEncoding:
    def test_wire_form_is_68_bytes(self) -> None:
        data = Handshake(TEST_INFO_HASH, TEST_PEER_ID).encode()
        assert len(data) == HANDSHAKE_LENGTH

    def test_layout(self) -> None:
        data = Handshake(TEST_INFO_HASH, TEST_PEER_ID).encode()
        assert data[0] == 19
        assert data[1:20] == PROTOCOL_STRING
        assert data[20:28] == bytes(8)
        assert data[28:48] == TEST_INFO_HASH
        assert data[48:68] == TEST_PEER_ID

    def test_round_trip(self) -> None:
        original = Handshake(TEST_INFO_HASH, TEST_PEER_ID, reserved=b"\x01" + bytes(7))
        decoded = Handshake.decode(original.encode())
        assert decoded == original

    def test_round_trip_with_expected_info_hash(self) -> None:
        handshake = Handshake(TEST_INFO_HASH, TEST_PEER_ID)
        assert Handshake.decode(handshake.encode(), expected_info_hash=TEST_INFO_HASH) == handshake


class TestValidation:
    def test_rejects_wrong_total_length(self) -> None:
        with pytest.raises(HandshakeError, match="must be 68 bytes"):
            Handshake.decode(handshake_bytes()[:-1])

    def test_rejects_empty_data(self) -> None:
        with pytest.raises(HandshakeError, match="must be 68 bytes"):
            Handshake.decode(b"")

    @pytest.mark.parametrize("length", [0, 18, 20, 255])
    def test_rejects_wrong_protocol_length(self, length: int) -> None:
        with pytest.raises(HandshakeError, match="protocol string length"):
            Handshake.decode(handshake_bytes(pstrlen=length))

    def test_rejects_wrong_protocol_string(self) -> None:
        with pytest.raises(HandshakeError, match="unsupported protocol"):
            Handshake.decode(handshake_bytes(pstr=b"BitTorrent protocoX"))

    def test_rejects_info_hash_mismatch(self) -> None:
        data = handshake_bytes(info_hash=OTHER_INFO_HASH)
        with pytest.raises(HandshakeError, match="info_hash mismatch"):
            Handshake.decode(data, expected_info_hash=TEST_INFO_HASH)

    def test_accepts_any_info_hash_when_none_expected(self) -> None:
        decoded = Handshake.decode(handshake_bytes(info_hash=OTHER_INFO_HASH))
        assert decoded.info_hash == OTHER_INFO_HASH

    @pytest.mark.parametrize("size", [0, 19, 21])
    def test_rejects_bad_info_hash_size(self, size: int) -> None:
        with pytest.raises(HandshakeError, match="info_hash must be 20 bytes"):
            Handshake(b"x" * size, TEST_PEER_ID)

    @pytest.mark.parametrize("size", [0, 19, 21])
    def test_rejects_bad_peer_id_size(self, size: int) -> None:
        with pytest.raises(HandshakeError, match="peer_id must be 20 bytes"):
            Handshake(TEST_INFO_HASH, b"x" * size)

    def test_rejects_bad_reserved_size(self) -> None:
        with pytest.raises(HandshakeError, match="reserved must be 8 bytes"):
            Handshake(TEST_INFO_HASH, TEST_PEER_ID, reserved=b"\x00" * 4)


class TestExtensionFlags:
    def test_dht_flag(self) -> None:
        reserved = bytearray(8)
        reserved[7] = 0x01
        assert Handshake(TEST_INFO_HASH, TEST_PEER_ID, bytes(reserved)).supports_dht
        assert not Handshake(TEST_INFO_HASH, TEST_PEER_ID).supports_dht

    def test_extension_flag(self) -> None:
        reserved = bytearray(8)
        reserved[5] = 0x10
        assert Handshake(TEST_INFO_HASH, TEST_PEER_ID, bytes(reserved)).supports_extensions

    def test_fast_flag(self) -> None:
        reserved = bytearray(8)
        reserved[7] = 0x04
        assert Handshake(TEST_INFO_HASH, TEST_PEER_ID, bytes(reserved)).supports_fast

    def test_outgoing_advertises_nothing_by_default(self) -> None:
        handshake = outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID)
        assert not handshake.supports_dht
        assert not handshake.supports_extensions
        assert not handshake.supports_fast

    def test_outgoing_can_advertise_dht(self) -> None:
        handshake = outgoing_handshake(TEST_INFO_HASH, TEST_PEER_ID, dht=True)
        assert handshake.supports_dht
        assert len(handshake.encode()) == HANDSHAKE_LENGTH
        assert Handshake.decode(handshake.encode()).supports_dht


class TestIdentification:
    def test_client_name_comes_from_the_peer_id(self) -> None:
        handshake = Handshake(TEST_INFO_HASH, FAKE_PEER_ID)
        assert "qBittorrent" in handshake.client

    def test_unknown_client(self) -> None:
        handshake = Handshake(TEST_INFO_HASH, bytes(20))
        assert handshake.client == "Unknown"

    def test_string_form_describes_the_peer(self) -> None:
        handshake = outgoing_handshake(TEST_INFO_HASH, FAKE_PEER_ID, dht=True)
        text = str(handshake)
        assert "qBittorrent" in text
        assert "dht" in text
        assert TEST_INFO_HASH.hex()[:12] in text
