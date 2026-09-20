"""Unit tests for the bencode encoder.

Correctness here is not cosmetic: the torrent ``info_hash`` is the SHA-1 of
*re-encoded* info-dictionary bytes, so a non-canonical or non-deterministic
encoder would make the client invisible to every other peer in the swarm.
These tests pin down the canonical form and the set of accepted types.
"""

from __future__ import annotations

import pytest
from app.bencode import encode, encoded_size
from app.bencode.errors import BencodeEncodeError


class TestScalars:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, b"i0e"),
            (42, b"i42e"),
            (-42, b"i-42e"),
            (b"", b"0:"),
            (b"hello", b"5:hello"),
            (b"a\x00b", b"3:a\x00b"),
            ("été".encode(), b"5:\xc3\xa9t\xc3\xa9"),
        ],
    )
    def test_encodes_scalars(self, value: object, expected: bytes) -> None:
        assert encode(value) == expected  # type: ignore[arg-type]

    def test_encodes_large_byte_string(self) -> None:
        payload = bytes(range(256)) * 256
        assert encode(payload) == b"%d:%s" % (len(payload), payload)

    def test_accepts_bytearray_and_memoryview(self) -> None:
        assert encode(bytearray(b"hi")) == b"2:hi"
        assert encode(memoryview(b"hi")) == b"2:hi"


class TestContainers:
    def test_encodes_empty_containers(self) -> None:
        assert encode([]) == b"le"
        assert encode({}) == b"de"

    def test_encodes_nested_structures(self) -> None:
        assert encode([1, b"a", [2]]) == b"li1e1:ali2eee"

    def test_tuples_encode_as_lists(self) -> None:
        assert encode((1, 2)) == b"li1ei2ee"

    def test_dictionary_keys_are_sorted(self) -> None:
        # Insertion order must not leak into the output.
        assert encode({b"b": 1, b"a": 2, b"c": 3}) == b"d1:ai2e1:bi1e1:ci3ee"

    def test_sorts_by_byte_value_not_locale(self) -> None:
        # 'Z' (0x5A) sorts before 'a' (0x61) in byte order.
        assert encode({b"a": 1, b"Z": 2}) == b"d1:Zi2e1:ai1ee"

    def test_encodes_torrent_like_document(self) -> None:
        document = {
            b"announce": b"http://tracker.example/announce",
            b"info": {b"length": 42, b"name": b"demo.bin", b"piece length": 16384},
        }
        assert encode(document) == (
            b"d8:announce31:http://tracker.example/announce"
            b"4:infod6:lengthi42e4:name8:demo.bin12:piece lengthi16384eee"
        )


class TestRejections:
    @pytest.mark.parametrize(
        "value",
        [
            True,  # bool is an int subclass; silently encoding it as 1 would be a bug
            False,
            None,
            "string",  # ambiguous encoding — must be explicit bytes
            1.5,
            {1, 2},  # sets are unordered, so they cannot be canonical
            object(),
        ],
    )
    def test_rejects_unrepresentable_values(self, value: object) -> None:
        with pytest.raises(BencodeEncodeError):
            encode(value)  # type: ignore[arg-type]

    def test_rejects_non_bytes_dictionary_key(self) -> None:
        with pytest.raises(BencodeEncodeError, match="byte strings"):
            encode({b"ok": 1, "text": 2})  # type: ignore[dict-item]

    def test_rejects_nested_unrepresentable_value(self) -> None:
        with pytest.raises(BencodeEncodeError):
            encode({b"a": [1, "nope"]})  # type: ignore[list-item]

    def test_rejects_boolean_nested_in_dictionary(self) -> None:
        with pytest.raises(BencodeEncodeError):
            encode({b"flag": True})  # type: ignore[dict-item]


class TestHelpers:
    def test_encoded_size_matches_encode(self) -> None:
        value = {b"a": [1, 2, 3], b"b": b"x" * 100}
        assert encoded_size(value) == len(encode(value))

    def test_encoder_is_reusable_and_stateless(self) -> None:
        from app.bencode import Encoder

        encoder = Encoder()
        first = encoder.encode({b"a": 1})
        second = encoder.encode({b"a": 1})
        assert first == second == b"d1:ai1ee"
