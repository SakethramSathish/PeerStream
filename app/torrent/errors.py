"""Exceptions raised while parsing or validating torrent metadata.

The distinction between the two main types matters for error recovery
(TRD §43): a :class:`TorrentParseError` means "this file is not a torrent",
which is terminal — there is nothing to retry.  An :class:`InvalidTorrentError`
means "this is bencode, but the metadata is wrong", which the UI reports
precisely (bad piece count, unsafe path) instead of a generic failure.
"""

from __future__ import annotations

from app.bencode.errors import BencodeError


class TorrentError(Exception):
    """Base class for torrent metadata errors."""


class TorrentParseError(TorrentError):
    """The input could not be read as a torrent at all (not bencode, no ``info``).

    This is terminal: the file cannot be recovered by retrying.
    """


class InvalidTorrentError(TorrentError):
    """The input is readable bencode but is not a valid torrent.

    Examples: ``pieces`` length is not a multiple of 20, ``info`` declares both
    ``length`` and ``files``, the declared piece count does not match the total
    size, or a file path is unsafe.
    """


class MagnetError(TorrentError):
    """A magnet link could not be read as one.

    The link is malformed or names no torrent — no ``xt``, or an ``xt`` that is
    neither 40 hex nor 32 base32 characters. Terminal: retrying the same text
    will not change the answer.
    """


class UnsupportedMagnetError(MagnetError):
    """The magnet is well-formed but names something this client cannot fetch.

    Raised for BitTorrent v2 magnets (``xt=urn:btmh:``, BEP 52). Saying so here
    is the difference between "this client does not do v2" and a confusing
    failure an hour later, after a DHT search that was never going to work.
    """


class UnsafePathError(InvalidTorrentError, ValueError):
    """A path derived from torrent metadata is not safe to write to disk.

    Subclasses both :class:`InvalidTorrentError` (so callers that only handle
    torrent-level failures still behave correctly) and :class:`ValueError` (so
    generic filesystem code can catch it too).

    Attributes:
        field: The metadata field the offending path came from, e.g.
            ``"info.files[2].path[1]"``.
        value: The offending value, for error reporting and logs.
    """

    def __init__(self, message: str, *, field: str = "path", value: str = "") -> None:
        self.field = field
        self.value = value
        detail = f" ({field}={value!r})" if value else f" (field: {field})"
        super().__init__(f"{message}{detail}")


__all__ = [
    "BencodeError",
    "InvalidTorrentError",
    "MagnetError",
    "TorrentError",
    "TorrentParseError",
    "UnsafePathError",
    "UnsupportedMagnetError",
]
