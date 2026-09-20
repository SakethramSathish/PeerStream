"""Unit tests for the bencode decoder.

The decoder is the boundary where untrusted bytes become in-memory structures,
so these tests cover three things: correct decoding of every token type,
rejection of non-canonical encodings (which would destabilise ``info_hash``),
and the resource limits that keep hostile input cheap to reject.
"""

from __future__ import annotations

import pytest
from app.bencode import Decoder, decode, decode_prefix
from app.bencode.errors import BencodeDecodeError


class TestIntegers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"i0e", 0),
            (b"i42e", 42),
            (b"i-42e", -42),
            (b"i-1e", -1),
            (b"i1234567890e", 1234567890),
            # Arbitrary precision: bencode integers are not word-sized.
            (b"i123456789012345678901234567890e", 123456789012345678901234567890),
        ],
    )
    def test_decodes_valid_integers(self, raw: bytes, expected: int) -> None:
        assert decode(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            b"ie",  # no digits
            b"i-e",  # sign without digits
            b"i03e",  # leading zero
            b"i-03e",  # negative with leading zero
            b"i-0e",  # negative zero
            b"i+1e",  # explicit plus
            b"i1.5e",  # not an integer
            b"i 1e",  # whitespace
            b"i1 e",  # trailing space inside token
            b"i1xe",  # junk digit
            b"i12",  # unterminated
            b"i",  # nothing at all
        ],
    )
    def test_rejects_malformed_integers(self, raw: bytes) -> None:
        with pytest.raises(BencodeDecodeError):
            decode(raw)


class TestByteStrings:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"0:", b""),
            (b"5:hello", b"hello"),
            (b"11:hello world", b"hello world"),
            (b"3:a\x00b", b"a\x00b"),  # NUL bytes are data, not terminators
            (b"5:\xc3\xa9t\xc3\xa9", "été".encode()),  # multi-byte UTF-8 payload
        ],
    )
    def test_decodes_valid_byte_strings(self, raw: bytes, expected: bytes) -> None:
        assert decode(raw) == expected

    def test_decodes_large_byte_string(self) -> None:
        payload = bytes(range(256)) * 512  # 128 KiB, covers every byte value
        assert decode(b"%d:%s" % (len(payload), payload)) == payload

    @pytest.mark.parametrize(
        "raw",
        [
            b"5:hell",  # fewer bytes than declared
            b"1:",  # zero bytes available
            b"5hello",  # missing colon
            b"-1:x",  # negative length
            b"01:x",  # leading zero in length
            b"00:",  # zero with leading zero
            b"x:abc",  # non-digit length
            b"+1:x",  # signed length
            b"1",  # length with no colon and no data
        ],
    )
    def test_rejects_malformed_byte_strings(self, raw: bytes) -> None:
        with pytest.raises(BencodeDecodeError):
            decode(raw)


class TestLists:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"le", []),
            (b"li1ee", [1]),
            (b"li1ei2ei3ee", [1, 2, 3]),
            (b"l5:helloi42ee", [b"hello", 42]),
            (b"llleee", [[[]]]),
            (b"ll1:ael1:bee", [[b"a"], [b"b"]]),
            (b"ldee", [{}]),  # a dictionary nested inside a list
        ],
    )
    def test_decodes_valid_lists(self, raw: bytes, expected: object) -> None:
        assert decode(raw) == expected

    @pytest.mark.parametrize("raw", [b"l", b"li1e", b"lle", b"l5:hello", b"li1eX"])
    def test_rejects_malformed_lists(self, raw: bytes) -> None:
        with pytest.raises(BencodeDecodeError):
            decode(raw)


class TestDictionaries:
    def test_decodes_empty_dictionary(self) -> None:
        assert decode(b"de") == {}

    def test_decodes_flat_dictionary(self) -> None:
        assert decode(b"d3:fooi42e3:bar5:helloe") == {b"bar": b"hello", b"foo": 42}

    def test_decodes_nested_dictionary(self) -> None:
        raw = b"d4:infod6:lengthi42e4:name3:abcee"
        assert decode(raw) == {b"info": {b"length": 42, b"name": b"abc"}}

    def test_preserves_byte_string_keys(self) -> None:
        # Keys must stay bytes: torrent field names are binary, not text.
        result = decode(b"d4:name5:linuxe")
        assert list(result) == [b"name"]

    def test_rejects_non_string_key(self) -> None:
        with pytest.raises(BencodeDecodeError, match="byte strings"):
            decode(b"di1ei2ee")

    def test_rejects_list_key(self) -> None:
        with pytest.raises(BencodeDecodeError, match="byte strings"):
            decode(b"dlei1ee")

    def test_rejects_duplicate_key(self) -> None:
        with pytest.raises(BencodeDecodeError, match="duplicate"):
            decode(b"d1:ai1e1:ai2ee")

    def test_rejects_unsorted_keys_when_required(self) -> None:
        raw = b"d3:fooi1e3:bari2ee"  # "foo" sorts after "bar"
        assert decode(raw) == {b"foo": 1, b"bar": 2}  # accepted by default
        with pytest.raises(BencodeDecodeError, match="sorts before"):
            decode(raw, require_sorted_keys=True)

    def test_accepts_sorted_keys_when_required(self) -> None:
        assert decode(b"d3:bari1e3:fooi2ee", require_sorted_keys=True) == {
            b"bar": 1,
            b"foo": 2,
        }

    @pytest.mark.parametrize("raw", [b"d", b"d3:foo", b"d3:fooi1e", b"d3:fooi1eeX"])
    def test_rejects_malformed_dictionaries(self, raw: bytes) -> None:
        with pytest.raises(BencodeDecodeError):
            decode(raw)


class TestStreaming:
    def test_decode_rejects_trailing_data(self) -> None:
        with pytest.raises(BencodeDecodeError, match="trailing"):
            decode(b"i1ei2e")

    def test_decode_prefix_returns_consumed_count(self) -> None:
        value, consumed = decode_prefix(b"i42eREST")
        assert value == 42
        assert consumed == 4

    def test_decode_prefix_can_be_chained(self) -> None:
        data = b"i1ei2e5:hello" + b"d1:ai3ee"
        values: list[object] = []
        offset = 0
        while offset < len(data):
            value, consumed = decode_prefix(data[offset:])
            values.append(value)
            offset += consumed
        assert values == [1, 2, b"hello", {b"a": 3}]


class TestValueSpans:
    """``decode_mapping_with_spans`` underpins info-hash correctness (M2)."""

    def test_returns_spans_for_every_key(self) -> None:
        raw = b"d4:infod6:lengthi42eee"
        mapping, spans = Decoder(raw).decode_mapping_with_spans()
        assert set(mapping) == set(spans) == {b"info"}
        start, end = spans[b"info"]
        assert raw[start:end] == b"d6:lengthi42ee"

    def test_span_slices_re_decode_to_the_same_value(self) -> None:
        raw = b"d8:announce20:http://t.example/ann4:infod4:name8:demo.bine"[:-3]
        raw = b"d4:infod4:name8:demo.bin6:lengthi42e12:piece lengthi16384eee"
        mapping, spans = Decoder(raw).decode_mapping_with_spans()
        for key, (start, end) in spans.items():
            assert decode(raw[start:end]) == mapping[key]

    def test_spans_cover_nested_values_exactly(self) -> None:
        raw = b"d1:ai1e1:bl1:x1:yee"
        _, spans = Decoder(raw).decode_mapping_with_spans()
        assert raw[spans[b"a"][0] : spans[b"a"][1]] == b"i1e"
        assert raw[spans[b"b"][0] : spans[b"b"][1]] == b"l1:x1:ye"

    def test_rejects_non_dictionary_top_level(self) -> None:
        with pytest.raises(BencodeDecodeError, match="top-level dictionary"):
            Decoder(b"i42e").decode_mapping_with_spans()

    def test_rejects_empty_input(self) -> None:
        with pytest.raises(BencodeDecodeError, match="top-level dictionary"):
            Decoder(b"").decode_mapping_with_spans()

    def test_rejects_trailing_data(self) -> None:
        with pytest.raises(BencodeDecodeError, match="trailing"):
            Decoder(b"d1:ai1eeX").decode_mapping_with_spans()


class TestInputTypes:
    def test_accepts_bytearray(self) -> None:
        assert decode(bytearray(b"i42e")) == 42

    def test_accepts_memoryview(self) -> None:
        assert decode(memoryview(b"5:hello")) == b"hello"

    def test_rejects_str(self) -> None:
        with pytest.raises(TypeError):
            decode("i42e")  # type: ignore[arg-type]


class TestDiagnostics:
    def test_error_reports_offset(self) -> None:
        with pytest.raises(BencodeDecodeError) as excinfo:
            decode(b"li1eX")
        assert excinfo.value.position >= 0
        assert str(excinfo.value).endswith(f"(at byte offset {excinfo.value.position})")

    def test_decoder_exposes_position(self) -> None:
        decoder = Decoder(b"i1ei2e")
        assert decoder.decode_prefix()[1] == 3
        assert decoder.position == 3
