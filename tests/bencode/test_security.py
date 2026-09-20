"""Security regression tests for the bencode decoder.

Every byte sequence in a ``.torrent`` file or tracker response is attacker
controlled.  These tests assert that malformed or hostile input fails fast with
a typed error, never with an unhandled exception, an unbounded allocation or a
hang.  Regression guard for TRD §41 (security requirements).
"""

from __future__ import annotations

import random
import time

import pytest
from app.bencode import decode
from app.bencode.errors import BencodeDecodeError


class TestResourceLimits:
    def test_absurd_length_prefix_is_rejected_immediately(self) -> None:
        # A 23-digit length would be ~10^22 bytes; we must not try to slice it.
        with pytest.raises(BencodeDecodeError, match="addressable size"):
            decode(b"99999999999999999999999:x")

    def test_declared_length_beyond_buffer_is_rejected(self) -> None:
        with pytest.raises(BencodeDecodeError, match="declares"):
            decode(b"100:short")

    def test_huge_but_legal_prefix_does_not_allocate(self) -> None:
        started = time.perf_counter()
        with pytest.raises(BencodeDecodeError):
            decode(b"1000000000:" + b"x" * 10)
        # Must fail on the length check, not while materialising a gigabyte.
        assert time.perf_counter() - started < 1.0

    def test_oversized_integer_token_is_rejected(self) -> None:
        with pytest.raises(BencodeDecodeError, match="digits"):
            decode(b"i" + b"9" * 5000 + b"e")

    def test_nesting_depth_is_bounded(self) -> None:
        depth = 500
        payload = b"l" * depth + b"e" * depth
        with pytest.raises(BencodeDecodeError, match="nesting deeper"):
            decode(payload)

    def test_nesting_depth_is_configurable(self) -> None:
        payload = b"l" * 10 + b"e" * 10
        assert decode(payload, max_depth=10) is not None
        with pytest.raises(BencodeDecodeError):
            decode(payload, max_depth=5)

    def test_input_size_limit_is_enforced(self) -> None:
        with pytest.raises(BencodeDecodeError, match="exceeds"):
            decode(b"i123456e", max_length=4)


class HostileInput:
    """A corpus of known-bad inputs that must never crash the parser."""

    CASES: tuple[bytes, ...] = (
        b"",
        b"i",
        b"e",
        b"l",
        b"d",
        b":",
        b"0",
        b"-",
        b"i-e",
        b"i-0e",
        b"i00e",
        b"0:x",  # length 0 followed by stray data
        b"1:",
        b"2:a",
        b"dexe",
        b"di1ei2ee",
        b"d1:ai1e1:ae",  # second key has no value
        b"d1:ai1e1:b",  # dictionary never closed
        b"lle",
        b"lee",
        b"l" * 200,
        b"d" * 200,
        b"i" + b"1" * 3000 + b"e",
        b"9" * 30 + b":x",
        b"1000000:abc",
        bytes(range(256)),
    )

    @pytest.mark.parametrize("raw", CASES)
    def test_malformed_input_raises_typed_error(self, raw: bytes) -> None:
        with pytest.raises(BencodeDecodeError):
            decode(raw)


def test_random_bytes_never_raise_untyped_errors() -> None:
    """Fuzz: 5 000 random buffers, every failure must be a BencodeDecodeError."""
    rng = random.Random(7)
    alphabet = b"0123456789ield:-x"
    decoded = 0
    for index in range(5000):
        # Alternate between pure noise and a bencode-ish alphabet: the noise
        # half probes the rejection paths, the alphabet half produces enough
        # valid documents to exercise the success path as well.
        if index % 2:
            raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 24)))
        else:
            raw = bytes(alphabet[rng.randrange(len(alphabet))] for _ in range(rng.randrange(1, 12)))
        try:
            decode(raw)
        except BencodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            pytest.fail(f"untyped {type(exc).__name__} for input {raw!r}: {exc}")
        else:
            decoded += 1
    # Sanity: the fuzz corpus should contain at least a few valid documents,
    # otherwise the test proves nothing about the success path.
    assert decoded > 0


def test_structured_fuzz_stays_typed() -> None:
    """Fuzz built from bencode-ish tokens rather than pure noise."""
    rng = random.Random(31337)
    tokens = [b"i", b"e", b"l", b"d", b":", b"0", b"1", b"9", b"-", b"x", b"42", b"abc"]
    for _ in range(5000):
        raw = b"".join(rng.choice(tokens) for _ in range(rng.randrange(1, 16)))
        try:
            decode(raw)
        except BencodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"untyped {type(exc).__name__} for input {raw!r}: {exc}")


def _decode_seconds(items: int) -> float:
    """Seconds to decode a flat list of ``items`` integers."""
    raw = b"l" + b"i1e" * items + b"e"
    started = time.perf_counter()
    assert len(decode(raw)) == items
    return time.perf_counter() - started


def test_decode_is_linear_on_large_input() -> None:
    """A multi-megabyte flat list must decode in linear time.

    The check is comparative, not a fixed budget: what matters is that four
    times the input costs about four times the work. An absolute number of
    seconds is a test that a slow machine — or a coverage tracer — fails for
    no good reason.
    """
    _decode_seconds(10_000)  # warm up
    small = _decode_seconds(250_000)
    large = _decode_seconds(1_000_000)

    ratio = large / max(small, 1e-9)
    assert ratio < 8.0, f"4x the input cost {ratio:.1f}x the time: decoding is not linear"
