"""Exception types raised by the bencode codec.

Bencode is the serialisation format used throughout BitTorrent: ``.torrent``
files, tracker responses and DHT (KRPC) packets are all bencoded.  Because all
of that data arrives from untrusted peers and remote servers, every malformed
input must surface as a specific, catchable error rather than an arbitrary
``ValueError``/``IndexError`` escaping from deep inside a parser.
"""

from __future__ import annotations


class BencodeError(Exception):
    """Base class for every error raised by this package."""


class BencodeDecodeError(BencodeError):
    """Raised when input is not well-formed bencode.

    The message always carries the byte offset at which parsing failed so that
    protocol logs can point at the exact malformed region.  The offending offset
    is also available programmatically via :attr:`position` (-1 when unknown).
    """

    def __init__(self, message: str, position: int = -1) -> None:
        self.position = position
        super().__init__(f"{message} (at byte offset {position})" if position >= 0 else message)


class BencodeEncodeError(BencodeError):
    """Raised when a Python object cannot be represented as bencode.

    Bencode has only four types (integers, byte strings, lists, dictionaries),
    so most Python objects — ``str``, ``float``, ``None``, ``bool`` — are not
    encodable and are rejected loudly instead of being coerced silently.
    """
