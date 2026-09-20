"""Unit tests for info-hash computation and formatting.

These tests pin down the property that makes or breaks the client: the
info-hash must be byte-identical to what every other BitTorrent client
computes. The subtle case is a torrent whose ``info`` dictionary is *not*
canonically encoded — re-encoding it changes the bytes and therefore the hash,
which is why the parser hashes the original slice.
"""

from __future__ import annotations

import hashlib

import pytest
from app.bencode import decode, encode
from app.torrent import (
    compute_info_hash,
    format_info_hash,
    info_hash_from_bencoded,
    parse_info_hash,
    parse_torrent,
    validate_info_hash,
    verify_info_dict,
)
from app.torrent.errors import InvalidTorrentError

# Canonical order is byte-ascending: "length" < "name" < "piece length".
CANONICAL_INFO: bytes = b"d6:lengthi42e4:name8:demo.bin12:piece lengthi16384ee"
# The same dictionary with its keys emitted out of order, as some encoders do.
UNSORTED_INFO: bytes = b"d4:name8:demo.bin6:lengthi42e12:piece lengthi16384ee"

# Complete (parseable) info dictionaries: 100 bytes, one piece.
_PIECES: bytes = b"6:pieces20:" + bytes.fromhex("11" * 20)
# Canonical key order: "length" < "name" < "piece length" < "pieces".
CANONICAL_TORRENT_INFO: bytes = (
    b"d6:lengthi100e4:name8:demo.bin12:piece lengthi16384e" + _PIECES + b"e"
)
# The same dictionary with its keys emitted out of order.
UNSORTED_TORRENT_INFO: bytes = (
    b"d4:name8:demo.bin6:lengthi100e12:piece lengthi16384e" + _PIECES + b"e"
)


class TestComputation:
    def test_hash_is_20_bytes(self) -> None:
        assert len(compute_info_hash(decode(CANONICAL_INFO))) == 20

    def test_matches_independent_sha1(self) -> None:
        expected = hashlib.sha1(CANONICAL_INFO).digest()
        assert compute_info_hash(decode(CANONICAL_INFO)) == expected
        assert info_hash_from_bencoded(CANONICAL_INFO) == expected

    def test_raw_and_canonical_agree_for_canonical_input(self) -> None:
        """Both paths must agree when the input is already canonical."""
        info = decode(CANONICAL_INFO)
        assert info_hash_from_bencoded(CANONICAL_INFO) == compute_info_hash(info)

    def test_raw_differs_from_reencode_for_unsorted_input(self) -> None:
        """The reason the parser hashes raw bytes instead of re-encoding.

        If we re-encoded this info dict, we would announce and handshake with a
        hash no other peer recognises, and silently download nothing.
        """
        info = decode(UNSORTED_INFO)
        assert encode(info) != UNSORTED_INFO  # canonicalising changes the bytes
        assert compute_info_hash(info) != info_hash_from_bencoded(UNSORTED_INFO)

    def test_parser_uses_the_wire_hash(self) -> None:
        """A torrent with unsorted info keys must hash to the on-the-wire value.

        The document is assembled by hand so the bytes on the wire really are
        non-canonical — running them through our encoder would normalise them.
        """
        raw = b"d4:info" + UNSORTED_TORRENT_INFO + b"e"
        torrent = parse_torrent(raw)

        assert torrent.info_hash == hashlib.sha1(UNSORTED_TORRENT_INFO).digest()
        assert torrent.info_hash != compute_info_hash(decode(UNSORTED_TORRENT_INFO))

    def test_hash_is_order_insensitive_for_canonical_documents(self) -> None:
        """Two torrents differing only in source key order hash identically."""
        first = encode(
            {
                b"info": {
                    b"name": b"demo.bin",
                    b"length": 100,
                    b"piece length": 16384,
                    b"pieces": bytes(20),
                }
            }
        )
        second = encode(
            {
                b"info": {
                    b"pieces": bytes(20),
                    b"piece length": 16384,
                    b"length": 100,
                    b"name": b"demo.bin",
                }
            }
        )
        assert first == second  # the encoder canonicalises key order
        assert parse_torrent(first).info_hash == parse_torrent(second).info_hash

    def test_accepts_bytearray_and_memoryview(self) -> None:
        expected = info_hash_from_bencoded(CANONICAL_INFO)
        assert info_hash_from_bencoded(bytearray(CANONICAL_INFO)) == expected
        assert info_hash_from_bencoded(memoryview(CANONICAL_INFO)) == expected


class TestValidation:
    def test_accepts_valid_hash(self) -> None:
        digest = bytes(range(20))
        assert validate_info_hash(digest) == digest

    @pytest.mark.parametrize("length", [0, 19, 21, 32])
    def test_rejects_wrong_length(self, length: int) -> None:
        with pytest.raises(InvalidTorrentError, match="exactly 20 bytes"):
            validate_info_hash(b"\x00" * length)

    def test_verify_info_dict_accepts_matching_metadata(self) -> None:
        info = decode(CANONICAL_INFO)
        assert verify_info_dict(info, compute_info_hash(info)) is True

    def test_verify_info_dict_rejects_tampered_metadata(self) -> None:
        info = decode(CANONICAL_INFO)
        tampered = {**info, b"length": 43}
        assert verify_info_dict(tampered, compute_info_hash(info)) is False

    def test_verify_info_dict_rejects_malformed_expected_value(self) -> None:
        with pytest.raises(InvalidTorrentError):
            verify_info_dict(decode(CANONICAL_INFO), b"too short")


class TestFormatting:
    def test_hex_round_trip(self) -> None:
        digest = compute_info_hash(decode(CANONICAL_INFO))
        text = format_info_hash(digest)
        assert len(text) == 40
        assert parse_info_hash(text) == digest

    def test_parses_uppercase_hex(self) -> None:
        digest = compute_info_hash(decode(CANONICAL_INFO))
        assert parse_info_hash(format_info_hash(digest).upper()) == digest

    def test_parses_urn_btih_form(self) -> None:
        digest = compute_info_hash(decode(CANONICAL_INFO))
        assert parse_info_hash(f"urn:btih:{format_info_hash(digest)}") == digest

    def test_parses_with_surrounding_whitespace(self) -> None:
        digest = compute_info_hash(decode(CANONICAL_INFO))
        assert parse_info_hash(f"  {format_info_hash(digest)}\n") == digest

    @pytest.mark.parametrize(
        "text",
        ["", "not-hex", "abc", "z" * 40, "00" * 19, "00" * 21],
    )
    def test_rejects_malformed_text(self, text: str) -> None:
        with pytest.raises(InvalidTorrentError):
            parse_info_hash(text)
