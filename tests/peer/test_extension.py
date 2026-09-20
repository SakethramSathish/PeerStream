"""BEP 10: the handshake that decides which id means what.

The interesting failures here are not parse failures — they are the two ways a
client can talk past its peer: numbering an extension with an id the peer did
not give it, and believing an id of 0 means "offered". Both are tested against
the bytes, because a bug in either direction is invisible until a real peer
drops the connection.
"""

from __future__ import annotations

import pytest
from app.bencode import encode
from app.peer.errors import MetadataError
from app.peer.extension import (
    HANDSHAKE_ID,
    MAX_EXTENSION_ID,
    MAX_HANDSHAKE_LENGTH,
    UT_METADATA,
    UT_PEX,
    DecodedHandshake,
    ExtensionError,
    ExtensionState,
    decode_handshake,
    encode_handshake,
)
from app.peer.metadata_exchange import OUR_UT_METADATA_ID, ExtensionHandshake


class TestEncoding:
    def test_a_round_trip_keeps_every_name_and_id(self) -> None:
        ours = {UT_METADATA: 1, UT_PEX: 2}

        decoded = decode_handshake(encode_handshake(ours))

        assert decoded.extensions == ours
        assert decoded.id_for(UT_PEX) == 2
        assert decoded.supports(UT_METADATA)
        assert not decoded.supports("lt_tex")

    def test_the_handshake_id_is_zero_and_extensions_start_at_one(self) -> None:
        # BEP 10: id 0 is the handshake itself, so an extension numbered 0 would
        # be indistinguishable from a second handshake.
        assert HANDSHAKE_ID == 0
        with pytest.raises(ExtensionError, match="outside 1-255"):
            encode_handshake({UT_PEX: 0})
        with pytest.raises(ExtensionError, match="outside 1-255"):
            encode_handshake({UT_PEX: MAX_EXTENSION_ID + 1})

    def test_the_largest_legal_id_is_accepted(self) -> None:
        decoded = decode_handshake(encode_handshake({UT_PEX: MAX_EXTENSION_ID}))
        assert decoded.id_for(UT_PEX) == MAX_EXTENSION_ID

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(ExtensionError, match="empty name"):
            encode_handshake({"": 1})

    def test_advertising_nothing_is_legal(self) -> None:
        # "We speak BEP 10 and nothing else" is a true statement, not an error.
        decoded = decode_handshake(encode_handshake({}))
        assert decoded.extensions == {}
        assert decoded.supports(UT_PEX) is False

    def test_the_optional_fields_travel_when_given(self) -> None:
        decoded = decode_handshake(
            encode_handshake({UT_PEX: 1}, version="bittorrent-client 0.1.0", port=6881)
        )
        assert decoded.version == "bittorrent-client 0.1.0"
        assert decoded.port == 6881

    def test_an_omitted_field_is_absent_not_empty(self) -> None:
        decoded = decode_handshake(encode_handshake({UT_PEX: 1}))
        assert decoded.version == ""
        assert decoded.port is None
        assert decoded.metadata_size is None
        assert decoded.reqq is None


class TestDecoding:
    def test_an_unreadable_body_is_refused(self) -> None:
        with pytest.raises(ExtensionError, match="unreadable"):
            decode_handshake(b"d3:bar")

    def test_a_body_that_is_not_a_dictionary_is_refused(self) -> None:
        with pytest.raises(ExtensionError, match="not a dictionary"):
            decode_handshake(encode([b"m"]))

    def test_an_absurdly_long_handshake_is_refused(self) -> None:
        with pytest.raises(ExtensionError, match="over"):
            decode_handshake(b"d" + b"x" * (MAX_HANDSHAKE_LENGTH + 1))

    def test_an_id_of_zero_means_not_offered(self) -> None:
        decoded = decode_handshake(encode({b"m": {b"ut_pex": 0, b"ut_metadata": 3}}))
        assert decoded.supports(UT_PEX) is False
        assert decoded.id_for(UT_METADATA) == 3

    def test_an_id_past_one_byte_is_dropped(self) -> None:
        decoded = decode_handshake(encode({b"m": {b"ut_pex": 256}}))
        assert decoded.extensions == {}

    def test_an_unknown_extension_is_kept_not_rejected(self) -> None:
        # A peer that advertises something we have never heard of is normal, and
        # refusing the whole handshake over it would cost us ut_pex too.
        decoded = decode_handshake(encode({b"m": {b"lt_donthave": 4, b"ut_pex": 1}}))
        assert decoded.extensions == {"lt_donthave": 4, UT_PEX: 1}

    def test_a_metadata_size_of_zero_is_no_metadata(self) -> None:
        decoded = decode_handshake(encode({b"m": {}, b"metadata_size": 0}))
        assert decoded.metadata_size is None

    def test_an_out_of_range_port_is_ignored(self) -> None:
        assert decode_handshake(encode({b"m": {}, b"p": 0})).port is None
        assert decode_handshake(encode({b"m": {}, b"p": 70000})).port is None
        assert decode_handshake(encode({b"m": {}, b"p": 51413})).port == 51413

    def test_a_version_that_is_not_utf8_still_decodes(self) -> None:
        decoded = decode_handshake(encode({b"m": {}, b"v": b"\xff\xfe client"}))
        assert "client" in decoded.version

    def test_unknown_fields_stay_reachable_in_the_raw_body(self) -> None:
        decoded = decode_handshake(encode({b"m": {}, b"yourip": b"\x01\x02\x03\x04"}))
        assert decoded.raw[b"yourip"] == b"\x01\x02\x03\x04"

    def test_a_non_integer_id_is_ignored(self) -> None:
        decoded = decode_handshake(encode({b"m": {b"ut_pex": b"1"}}))
        assert decoded.extensions == {}

    def test_an_empty_name_in_their_map_is_ignored(self) -> None:
        decoded = decode_handshake(encode({b"m": {b"": 1}}))
        assert decoded.extensions == {}


class TestState:
    def test_nothing_can_be_sent_before_their_handshake_arrives(self) -> None:
        state = ExtensionState(ours={UT_PEX: 1})

        assert state.negotiated is False
        assert state.can_send(UT_PEX) is False, "we do not know their id yet"
        assert state.their_id(UT_PEX) is None
        assert state.our_id(UT_PEX) == 1
        assert state.we_offer == (UT_PEX,)

    def test_a_shared_extension_is_the_only_one_worth_using(self) -> None:
        state = ExtensionState(ours={UT_PEX: 1, UT_METADATA: 2})
        state.note_theirs(decode_handshake(encode_handshake({UT_PEX: 9})))

        assert state.negotiated is True
        assert state.can_send(UT_PEX) is True
        assert state.can_send(UT_METADATA) is False
        assert state.shared() == (UT_PEX,)
        assert state.their_id(UT_PEX) == 9, "their id, not ours"

    def test_ids_are_per_direction(self) -> None:
        # The single most common BEP 10 bug: replying with our own id.
        state = ExtensionState(ours={UT_PEX: 1})
        state.note_theirs(decode_handshake(encode_handshake({UT_PEX: 7})))

        assert state.our_id(UT_PEX) == 1
        assert state.their_id(UT_PEX) == 7

    def test_a_repeated_handshake_replaces_the_first(self) -> None:
        state = ExtensionState(ours={UT_PEX: 1})
        state.note_theirs(decode_handshake(encode_handshake({UT_PEX: 7})))
        state.note_theirs(decode_handshake(encode_handshake({UT_METADATA: 3})))

        assert state.their_id(UT_PEX) is None, "they withdrew it"
        assert state.their_id(UT_METADATA) == 3
        assert state.can_send(UT_PEX) is False

    def test_their_version_and_port_are_recorded(self) -> None:
        state = ExtensionState(ours={UT_PEX: 1})
        state.note_theirs(
            decode_handshake(encode_handshake({UT_PEX: 2}, version="qBittorrent 5.1.0", port=6881))
        )

        assert state.version == "qBittorrent 5.1.0"
        assert state.port == 6881

    def test_the_body_we_send_names_our_own_ids(self) -> None:
        state = ExtensionState(ours={UT_PEX: 4})

        assert decode_handshake(state.encode_handshake(version="x")).extensions == {UT_PEX: 4}

    def test_an_empty_state_still_describes_itself(self) -> None:
        state = ExtensionState()
        assert state.we_offer == ()
        assert state.shared() == ()
        assert isinstance(DecodedHandshake(), DecodedHandshake)


class TestMetadataHandshakeStillDelegates:
    """BEP 9's reader moved onto the shared parser without changing its answers."""

    def test_the_metadata_fields_are_still_read(self) -> None:
        parsed = ExtensionHandshake.from_payload(
            encode({b"m": {b"ut_metadata": 6}, b"metadata_size": 321, b"reqq": 250, b"v": b"qB"})
        )

        assert parsed.ut_metadata_id == 6
        assert parsed.metadata_size == 321
        assert parsed.reqq == 250
        assert parsed.client == "qB"
        assert parsed.can_serve_metadata

    def test_its_error_type_is_still_metadata_error(self) -> None:
        with pytest.raises(MetadataError, match="unreadable"):
            ExtensionHandshake.from_payload(b"d3:bar")
        with pytest.raises(MetadataError, match="not a dictionary"):
            ExtensionHandshake.from_payload(encode([1]))

    def test_it_advertises_only_metadata(self) -> None:
        parsed = decode_handshake(ExtensionHandshake().encode())
        assert parsed.extensions == {UT_METADATA: OUR_UT_METADATA_ID}

    def test_an_oversized_handshake_is_refused_as_metadata_error(self) -> None:
        with pytest.raises(MetadataError, match="over"):
            ExtensionHandshake.from_payload(b"d" + b"x" * (MAX_HANDSHAKE_LENGTH + 1))
