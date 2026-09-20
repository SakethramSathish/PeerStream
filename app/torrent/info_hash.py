"""info-hash computation and formatting.

The ``info_hash`` is the identity of a torrent in the entire BitTorrent
ecosystem: trackers key announces by it, handshakes are validated against it,
and DHT lookups are addressed by it.  It is the SHA-1 of the bencoded ``info``
dictionary — and getting it wrong means the client is silently invisible to
every peer in the swarm.

There are two ways to compute it, and the difference matters:

``info_hash_from_bencoded(raw_bytes)``
    SHA-1 of the info dictionary **as it appeared on the wire**. This is the
    correct choice when the original bytes are available, because some encoders
    emit dictionary keys in non-canonical order; re-encoding would normalise
    that ordering and produce a hash no other peer recognises.

``compute_info_hash(info_mapping)``
    SHA-1 of the *canonical re-encoding* of a parsed info dictionary. Used when
    the original bytes are unavailable — i.e. metadata fetched from peers during
    magnet-link resolution (BEP 9), where the metadata is required to be
    canonical.

:func:`app.torrent.parser.parse_torrent` always uses the first form.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from hmac import compare_digest
from typing import Final

from app.bencode import encode
from app.bencode.decoder import BencodeValue
from app.torrent.errors import InvalidTorrentError

INFO_HASH_SIZE: Final[int] = 20  # SHA-1 digest length, in bytes
_URN_PREFIX: Final[str] = "urn:btih:"


def compute_info_hash(info: Mapping[bytes, BencodeValue]) -> bytes:
    """Compute the info-hash from a parsed info dictionary.

    Args:
        info: The ``info`` mapping, already validated as a dictionary.

    Returns:
        The 20-byte SHA-1 digest.

    Note:
        The hash is taken over the *canonical* encoding (dictionary keys sorted),
        so this differs from the on-the-wire hash for any torrent whose info
        dictionary was not canonically encoded. Prefer
        :func:`info_hash_from_bencoded` whenever the original bytes exist.
    """
    return hashlib.sha1(encode(dict(info))).digest()


def info_hash_from_bencoded(raw: bytes | bytearray | memoryview) -> bytes:
    """Compute the info-hash from raw bencoded info-dictionary bytes.

    Args:
        raw: The exact encoded bytes of the ``info`` dictionary.

    Returns:
        The 20-byte SHA-1 digest — byte-for-byte identical to what every other
        client in the swarm computes.
    """
    return hashlib.sha1(bytes(raw)).digest()


def verify_info_dict(info: Mapping[bytes, BencodeValue], expected: bytes) -> bool:
    """Check that a metadata dictionary matches an expected info-hash.

    Used when metadata arrives from an untrusted source — a peer during magnet
    resolution — before it is trusted for downloading.

    Args:
        info: Candidate info dictionary.
        expected: The 20-byte info-hash from the magnet link.

    Returns:
        True when the canonical encoding of ``info`` hashes to ``expected``.
    """
    validate_info_hash(expected)
    return compare_digest(compute_info_hash(info), expected)


def validate_info_hash(candidate: bytes | bytearray | memoryview) -> bytes:
    """Validate and normalise an info-hash value.

    Args:
        candidate: A purported 20-byte info-hash.

    Returns:
        The hash as ``bytes``.

    Raises:
        InvalidTorrentError: If the length is not exactly 20 bytes.
    """
    value = bytes(candidate)
    if len(value) != INFO_HASH_SIZE:
        raise InvalidTorrentError(
            f"info-hash must be exactly {INFO_HASH_SIZE} bytes, got {len(value)}"
        )
    return value


def format_info_hash(info_hash: bytes | bytearray | memoryview) -> str:
    """Render an info-hash as lowercase hex (the conventional display form)."""
    return bytes(info_hash).hex()


def parse_info_hash(text: str) -> bytes:
    """Parse an info-hash from hex, a ``urn:btih:`` URN, or raw 20 bytes.

    Accepts the forms that appear in magnet links and user input: 40-character
    hex, case-insensitive, with or without the ``urn:btih:`` prefix.

    Args:
        text: The textual representation.

    Returns:
        The 20-byte digest.

    Raises:
        InvalidTorrentError: If the text is not a well-formed 20-byte hash.
    """
    candidate = text.strip()
    if candidate.lower().startswith(_URN_PREFIX):
        candidate = candidate[len(_URN_PREFIX) :]
    try:
        digest = bytes.fromhex(candidate)
    except ValueError as exc:
        raise InvalidTorrentError(f"not a valid hex info-hash: {text!r}") from exc
    return validate_info_hash(digest)
