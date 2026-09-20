"""Canonical bencode encoder.

Encoding is as security-relevant as decoding: the SHA-1 ``info_hash`` is
computed over *re-encoded* info dictionary bytes, so the encoder must produce
byte-identical output for the same logical structure every time.  That means:

* dictionary keys are emitted in ascending byte order (BEP 3 requires sorted
  keys in ``info`` dictionaries);
* integers are emitted with no leading zeros, ``+`` or spaces;
* only the four bencode types are accepted — anything else is an error rather
  than a silent coercion, because a silent ``str`` → ``bytes`` coercion would
  produce an ``info_hash`` that depends on the platform's default encoding.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from app.bencode.decoder import BencodeValue
from app.bencode.errors import BencodeEncodeError

_ENCODABLE: Final[tuple[type, ...]] = (int, bytes, bytearray, memoryview, list, tuple, dict)


class Encoder:
    """Bencode encoder that writes into a reusable buffer.

    Encoding appends into a single ``bytearray`` instead of building
    intermediate ``bytes`` objects, which matters when re-encoding multi-megabyte
    dictionaries (the ``pieces`` string of a large torrent is a single byte
    string hundreds of kilobytes long).
    """

    __slots__ = ("_out",)

    def __init__(self) -> None:
        self._out = bytearray()

    def encode(self, value: BencodeValue) -> bytes:
        """Encode ``value`` and return the bencoded bytes.

        Raises:
            BencodeEncodeError: If ``value`` contains a type bencode cannot
                represent, or a dictionary key that is not a byte string.
        """
        self._out = bytearray()
        self._encode_value(value)
        return bytes(self._out)

    def _encode_value(self, value: object) -> None:
        # Fast path ordering matters: bool is a subclass of int and must be
        # rejected before the int branch, otherwise `True` would silently
        # serialise as the integer 1.
        if value is True or value is False:
            raise BencodeEncodeError(
                "booleans are not representable in bencode; use 1 or 0 explicitly"
            )

        if isinstance(value, int):
            self._out += b"i%de" % value
            return

        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            self._out += b"%d:" % len(raw)
            self._out += raw
            return

        if isinstance(value, (list, tuple)):
            self._out += b"l"
            for item in value:
                self._encode_value(item)
            self._out += b"e"
            return

        if isinstance(value, Mapping):
            # Validate before sorting: sorted() on mixed str/bytes keys raises a
            # bare TypeError that would leak past this module's error contract.
            keys = list(value)
            for key in keys:
                if not isinstance(key, (bytes, bytearray, memoryview)):
                    raise BencodeEncodeError(
                        f"dictionary keys must be byte strings, got {type(key).__name__} ({key!r})"
                    )
            self._out += b"d"
            for key in sorted(keys):  # canonical order (BEP 3)
                self._encode_value(bytes(key))
                self._encode_value(value[key])
            self._out += b"e"
            return

        raise BencodeEncodeError(
            f"{type(value).__name__} is not representable in bencode "
            f"(supported: int, bytes, list, dict)"
        )


def encode(value: BencodeValue) -> bytes:
    """Encode a Python object as bencode.

    Args:
        value: An ``int``, ``bytes``, ``list``/``tuple`` or ``dict`` tree.
            Dictionary keys must be ``bytes``; they are sorted automatically.

    Returns:
        The canonical bencoded representation.

    Raises:
        BencodeEncodeError: If the value is not bencode-representable.

    Examples:
        >>> encode({b"announce": b"http://t/announce", b"info": {b"length": 42}})
        b'd8:announce17:http://t/announce4:infod6:lengthi42eee'
    """
    if not isinstance(value, _ENCODABLE) or isinstance(value, bool):
        raise BencodeEncodeError(
            f"{type(value).__name__} is not representable in bencode "
            f"(supported: int, bytes, list, dict)"
        )
    return Encoder().encode(value)


def encoded_size(value: BencodeValue) -> int:
    """Return the number of bytes :func:`encode` would produce.

    Used by the storage and resume layers to size writes without materialising
    the encoded buffer twice.
    """
    return len(encode(value))
