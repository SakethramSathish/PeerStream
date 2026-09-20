"""Strict bencode decoder.

Why this exists instead of ``bencodepy``: bencode parsing is one of the two
places (the other being the peer wire protocol) where remote, attacker-
controlled bytes are turned into in-memory structures.  Owning the parser lets
us enforce canonical-form rules and hard resource limits at the exact boundary
where it matters, and it is a core piece of protocol understanding for the
project.

Supported grammar (BEP 3, with BEP 52-compatible integers)::

    integer   ::= "i" <sign? digits> "e"
    string    ::= <digits> ":" <bytes>
    list      ::= "l" <value>* "e"
    dict      ::= "d" (<string> <value>)* "e"

Canonical-form rules enforced (important for ``info_hash`` stability):

* integers have no leading zeros, and negative zero is rejected;
* byte-string lengths have no leading zeros;
* dictionary keys are byte strings, unique, and — optionally — sorted.

Resource limits protect against hostile input: total input size, nesting depth
and the digit count of length/int tokens are all bounded, so a malicious
``.torrent`` cannot exhaust memory or CPU.
"""

from __future__ import annotations

from typing import Final

from app.bencode.errors import BencodeDecodeError

BencodeValue = int | bytes | list['BencodeValue'] | dict[bytes, 'BencodeValue']
"""Any value representable in bencode.  Dictionary keys are always bytes."""

DEFAULT_MAX_DEPTH: Final[int] = 100
DEFAULT_MAX_LENGTH: Final[int] = 64 * 1024 * 1024  # 64 MiB of bencoded input

# Digits allowed in a length prefix. 20 digits is far beyond any real torrent
# (2^64 fits in 20 digits) while keeping int() conversions trivially cheap.
_MAX_LENGTH_DIGITS: Final[int] = 20
# Generous, but bounded: prevents a 1 MiB integer token from being scanned.
_MAX_INT_DIGITS: Final[int] = 1024

_DIGIT_BYTES: Final[frozenset[int]] = frozenset(b"0123456789")

_TOKEN_INT: Final[int] = 0x69  # 'i'
_TOKEN_LIST: Final[int] = 0x6C  # 'l'
_TOKEN_DICT: Final[int] = 0x64  # 'd'
_TOKEN_END: Final[int] = 0x65  # 'e'
_TOKEN_COLON: Final[int] = 0x3A  # ':'
_TOKEN_MINUS: Final[int] = 0x2D  # '-'
_TOKEN_ZERO: Final[int] = 0x30  # '0'
_TOKEN_NINE: Final[int] = 0x39  # '9'


class Decoder:
    """Single-use, cursor-based bencode decoder.

    A decoder instance carries the buffer plus its parse limits, which keeps the
    token helpers free of long parameter lists and lets error messages report an
    exact byte offset.

    Args:
        data: Bytes to decode. ``bytes``, ``bytearray`` and ``memoryview`` are
            all accepted; the buffer is copied so the caller may mutate it after
            the call.
        max_depth: Maximum container nesting depth.
        max_length: Maximum accepted input size, in bytes.
        require_sorted_keys: When true, reject dictionaries whose keys are not in
            ascending byte order. Torrent ``info`` dictionaries are required by
            BEP 3 to be sorted, so the metadata parser enables this to detect
            non-conforming files that would otherwise produce an ``info_hash``
            no other peer in the swarm can match.
    """

    __slots__ = ("_data", "_max_depth", "_pos", "_require_sorted_keys")

    def __init__(
        self,
        data: bytes | bytearray | memoryview,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_length: int = DEFAULT_MAX_LENGTH,
        require_sorted_keys: bool = False,
    ) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"expected a bytes-like object, got {type(data).__name__}")
        if len(data) > max_length:
            raise BencodeDecodeError(
                f"input of {len(data)} bytes exceeds the {max_length} byte limit"
            )
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")

        self._data: bytes = bytes(data)
        self._max_depth: int = max_depth
        self._require_sorted_keys: bool = require_sorted_keys
        self._pos: int = 0

    # ------------------------------------------------------------------ API

    def decode(self) -> BencodeValue:
        """Decode exactly one value and reject any trailing bytes.

        Raises:
            BencodeDecodeError: if the data is malformed, exceeds a limit, or is
                followed by extra bytes.
        """
        value = self._decode_value(depth=0)
        if self._pos != len(self._data):
            raise BencodeDecodeError("unexpected trailing data", self._pos)
        return value

    def decode_prefix(self) -> tuple[BencodeValue, int]:
        """Decode one value from the start of the buffer.

        Unlike :meth:`decode`, trailing bytes are allowed — this is what a
        stream parser needs when several bencoded values share one buffer.

        Returns:
            A ``(value, consumed)`` pair where ``consumed`` is the number of
            bytes taken from the front of the buffer.
        """
        value = self._decode_value(depth=0)
        return value, self._pos

    def decode_mapping_with_spans(
        self,
    ) -> tuple[dict[bytes, BencodeValue], dict[bytes, tuple[int, int]]]:
        """Decode a top-level dictionary and report where each value lives.

        Returns:
            ``(mapping, spans)`` where ``spans[key]`` is the ``(start, end)``
            byte range of the *encoded* value, ready to slice out of the
            original buffer.

        Why this exists: the torrent ``info_hash`` is the SHA-1 of the info
        dictionary **exactly as it appeared on the wire**. Some encoders emit
        dictionary keys in non-canonical order, so re-encoding a parsed info
        dict can produce a different hash — one that no other peer in the swarm
        recognises. Hashing the original byte slice avoids that class of bug
        entirely, and is what :func:`app.torrent.parser.parse_torrent` uses.
        """
        if not self._data or self._data[0] != _TOKEN_DICT:
            raise BencodeDecodeError("expected a top-level dictionary", self._pos)
        spans: dict[bytes, tuple[int, int]] = {}
        mapping = self._decode_dict(depth=0, spans=spans)
        if self._pos != len(self._data):
            raise BencodeDecodeError("unexpected trailing data", self._pos)
        return mapping, spans

    @property
    def position(self) -> int:
        """Current cursor position, exposed for diagnostics."""
        return self._pos

    # -------------------------------------------------------------- tokens

    def _decode_value(self, depth: int) -> BencodeValue:
        if depth > self._max_depth:
            raise BencodeDecodeError(f"nesting deeper than {self._max_depth} levels", self._pos)

        pos = self._pos
        if pos >= len(self._data):
            raise BencodeDecodeError("unexpected end of data while expecting a value", pos)

        token = self._data[pos]
        if _TOKEN_ZERO <= token <= _TOKEN_NINE:
            return self._decode_bytes()
        if token == _TOKEN_INT:
            return self._decode_int()
        if token == _TOKEN_LIST:
            return self._decode_list(depth)
        if token == _TOKEN_DICT:
            return self._decode_dict(depth)

        raise BencodeDecodeError(f"invalid token {bytes((token,))!r}", pos)

    def _decode_int(self) -> int:
        start = self._pos
        end = self._data.find(b"e", start + 1)
        if end == -1:
            raise BencodeDecodeError("unterminated integer (missing 'e')", start)

        raw = self._data[start + 1 : end]
        if not raw:
            raise BencodeDecodeError("empty integer", start)
        if len(raw) > _MAX_INT_DIGITS:
            raise BencodeDecodeError(f"integer token longer than {_MAX_INT_DIGITS} digits", start)

        negative = raw[0] == _TOKEN_MINUS
        digits = raw[1:] if negative else raw
        if not digits or not _all_digits(digits):
            raise BencodeDecodeError(f"malformed integer {raw!r}", start)
        if len(digits) > 1 and digits[0] == _TOKEN_ZERO:
            raise BencodeDecodeError(f"integer with leading zero {raw!r}", start)
        if negative and digits == b"0":
            raise BencodeDecodeError("negative zero is not valid bencode", start)

        self._pos = end + 1
        return int(raw)

    def _decode_bytes(self) -> bytes:
        start = self._pos
        colon = self._data.find(b":", start)
        if colon == -1:
            raise BencodeDecodeError("unterminated byte string (missing ':')", start)

        digits = self._data[start:colon]
        if not digits or not _all_digits(digits):
            raise BencodeDecodeError(f"malformed byte-string length {digits!r}", start)
        if len(digits) > _MAX_LENGTH_DIGITS:
            raise BencodeDecodeError("byte-string length exceeds addressable size", start)
        if len(digits) > 1 and digits[0] == _TOKEN_ZERO:
            raise BencodeDecodeError(f"byte-string length with leading zero {digits!r}", start)

        length = int(digits)
        begin = colon + 1
        end = begin + length
        if end > len(self._data):
            raise BencodeDecodeError(
                f"byte string declares {length} bytes but only "
                f"{max(0, len(self._data) - begin)} remain",
                start,
            )

        self._pos = end
        return self._data[begin:end]

    def _decode_list(self, depth: int) -> list[BencodeValue]:
        self._pos += 1  # consume 'l'
        items: list[BencodeValue] = []
        while True:
            pos = self._pos
            if pos >= len(self._data):
                raise BencodeDecodeError("unterminated list (missing 'e')", pos)
            if self._data[pos] == _TOKEN_END:
                self._pos += 1
                return items
            items.append(self._decode_value(depth + 1))

    def _decode_dict(
        self,
        depth: int,
        spans: dict[bytes, tuple[int, int]] | None = None,
    ) -> dict[bytes, BencodeValue]:
        self._pos += 1  # consume 'd'
        mapping: dict[bytes, BencodeValue] = {}
        previous_key: bytes | None = None
        while True:
            pos = self._pos
            if pos >= len(self._data):
                raise BencodeDecodeError("unterminated dictionary (missing 'e')", pos)
            if self._data[pos] == _TOKEN_END:
                self._pos += 1
                return mapping

            key = self._decode_value(depth + 1)
            if not isinstance(key, bytes):
                raise BencodeDecodeError(
                    f"dictionary keys must be byte strings, got {type(key).__name__}",
                    pos,
                )
            if key in mapping:
                raise BencodeDecodeError(f"duplicate dictionary key {key!r}", pos)
            if self._require_sorted_keys and previous_key is not None and key < previous_key:
                raise BencodeDecodeError(
                    f"dictionary key {key!r} sorts before the preceding key {previous_key!r}",
                    pos,
                )

            previous_key = key
            value_start = self._pos
            mapping[key] = self._decode_value(depth + 1)
            if spans is not None:
                spans[key] = (value_start, self._pos)


def _all_digits(raw: bytes) -> bool:
    """True when every byte is an ASCII digit."""
    return all(byte in _DIGIT_BYTES for byte in raw)


def decode(
    data: bytes | bytearray | memoryview,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_length: int = DEFAULT_MAX_LENGTH,
    require_sorted_keys: bool = False,
) -> BencodeValue:
    """Decode a complete bencoded document.

    Args:
        data: Bencoded bytes.
        max_depth: Maximum container nesting depth.
        max_length: Maximum accepted input size in bytes.
        require_sorted_keys: Reject dictionaries whose keys are unsorted.

    Returns:
        The decoded value: an ``int``, ``bytes``, ``list`` or ``dict`` whose
        keys are ``bytes``.

    Raises:
        BencodeDecodeError: On malformed, non-canonical or oversized input.
        TypeError: If ``data`` is not bytes-like.
    """
    return Decoder(
        data,
        max_depth=max_depth,
        max_length=max_length,
        require_sorted_keys=require_sorted_keys,
    ).decode()


def decode_prefix(
    data: bytes | bytearray | memoryview,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_length: int = DEFAULT_MAX_LENGTH,
    require_sorted_keys: bool = False,
) -> tuple[BencodeValue, int]:
    """Decode the first bencoded value in ``data``.

    Returns:
        ``(value, consumed_byte_count)``; trailing bytes are left untouched.
    """
    return Decoder(
        data,
        max_depth=max_depth,
        max_length=max_length,
        require_sorted_keys=require_sorted_keys,
    ).decode_prefix()
