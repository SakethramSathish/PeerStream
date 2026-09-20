"""Round-trip and canonical-form tests.

Two properties matter to the rest of the client:

1. ``decode(encode(value)) == value`` for every bencode-representable value.
2. ``encode(decode(raw)) == raw`` for every *canonical* encoding — this is what
   makes ``info_hash`` stable across a parse/re-serialise cycle, and it is the
   property a naive implementation breaks by preserving dictionary insertion
   order.
"""

from __future__ import annotations

import random

import pytest
from app.bencode import decode, encode
from app.bencode.errors import BencodeDecodeError

CANONICAL_VECTORS: list[bytes] = [
    b"i0e",
    b"i42e",
    b"i-42e",
    b"0:",
    b"5:hello",
    b"le",
    b"de",
    b"li1ei2ei3ee",
    b"l5:helloi42ee",
    b"d1:ai1ee",
    b"d1:ai1e1:bi2ee",
    b"d4:infod6:lengthi42e4:name8:demo.binee",
    b"ll1:ael1:bee",
]


class TestCanonicalForm:
    @pytest.mark.parametrize("raw", CANONICAL_VECTORS)
    def test_reencoding_reproduces_input(self, raw: bytes) -> None:
        assert encode(decode(raw)) == raw

    def test_key_order_is_normalised(self) -> None:
        # A non-canonical (unsorted) document re-encodes into canonical form.
        assert encode(decode(b"d3:fooi1e3:bari2ee")) == b"d3:bari2e3:fooi1ee"

    def test_leading_zeros_are_normalised(self) -> None:
        # Our decoder rejects these outright; document the guarantee.
        with pytest.raises(BencodeDecodeError):
            decode(b"i042e")


class TestValueRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            0,
            1,
            -1,
            2**80,
            b"",
            b"\x00\xff\x10binary",
            [],
            {},
            [1, b"two", [3, [4]]],
            {b"key": [1, 2, {b"nested": b"value"}]},
        ],
    )
    def test_round_trips(self, value: object) -> None:
        assert decode(encode(value)) == value  # type: ignore[arg-type]

    def test_round_trips_torrent_shaped_document(self) -> None:
        pieces = bytes(random.Random(1234).randrange(256) for _ in range(20 * 20))
        document = {
            b"announce": b"http://tracker.example:6969/announce",
            b"announce-list": [[b"http://tracker.example:6969/announce"]],
            b"info": {
                b"length": 1024,
                b"name": b"payload.bin",
                b"piece length": 16384,
                b"pieces": pieces,
            },
        }
        restored = decode(encode(document))
        assert restored == document
        assert restored[b"info"][b"pieces"] == pieces  # type: ignore[index]


class TestFuzzRoundTrip:
    """Randomised round-trips with a fixed seed, so failures are reproducible."""

    def _random_value(self, rng: random.Random, depth: int) -> object:
        choice = rng.randrange(4)
        if depth <= 0 or choice == 0:
            return rng.randrange(-(2**40), 2**40)
        if choice == 1:
            return bytes(rng.randrange(256) for _ in range(rng.randrange(0, 32)))
        if choice == 2:
            return [self._random_value(rng, depth - 1) for _ in range(rng.randrange(0, 5))]
        keys = [bytes([rng.randrange(97, 123)]) for _ in range(rng.randrange(0, 6))]
        return {key: self._random_value(rng, depth - 1) for key in set(keys)}

    def test_random_structures_survive_round_trip(self) -> None:
        rng = random.Random(20240917)
        for _ in range(300):
            value = self._random_value(rng, depth=4)
            assert decode(encode(value)) == value

    def test_random_structures_are_canonical_after_one_pass(self) -> None:
        """Encoding twice must be idempotent."""
        rng = random.Random(99)
        for _ in range(200):
            value = self._random_value(rng, depth=3)
            once = encode(value)  # type: ignore[arg-type]
            assert encode(decode(once)) == once
