"""``.torrent`` parsing.

Turns untrusted bytes into a :class:`~app.torrent.metadata.Torrent`.

The parser is intentionally strict — a torrent that is accepted here is a
torrent the rest of the client can trust. Anything ambiguous is rejected with a
specific error naming the offending field, because a vague "bad torrent" is
useless to a user who is trying to work out why their file will not open, and
because a lenient parser is how corrupt piece geometry reaches the downloader.

Validation performed here (PRD §13 FR-01, TRD §41):

* the document is a dictionary containing an ``info`` dictionary;
* ``info`` declares exactly one of ``length`` (single-file) or ``files``
  (multi-file);
* ``piece length`` is a positive integer;
* ``pieces`` is a non-empty byte string whose length is a multiple of 20;
* the number of piece hashes matches ``ceil(total_length / piece_length)``;
* every file path is safe to write (see :mod:`app.torrent.path_safety`);
* tracker URLs use a scheme we can actually speak (http/https/udp).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit

from app.bencode.decoder import BencodeValue, Decoder
from app.bencode.errors import BencodeDecodeError
from app.torrent.errors import InvalidTorrentError, TorrentParseError
from app.torrent.info_hash import compute_info_hash, info_hash_from_bencoded
from app.torrent.metadata import FileEntry, Torrent
from app.torrent.path_safety import sanitize_name, sanitize_path_components

logger = logging.getLogger(__name__)

_SUPPORTED_TRACKER_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "udp"})
_HASH_SIZE: Final[int] = 20
# Real-world torrents use 16 KiB-64 MiB pieces. Outside that range we accept the
# file but say so: it usually means a broken or hostile generator.
_MIN_TYPICAL_PIECE_LENGTH: Final[int] = 16 * 1024
_MAX_TYPICAL_PIECE_LENGTH: Final[int] = 64 * 1024 * 1024


def parse_torrent(data: bytes | bytearray | memoryview) -> Torrent:
    """Parse bencoded ``.torrent`` bytes into a validated :class:`Torrent`.

    The ``info_hash`` is computed over the *original* encoded ``info`` bytes,
    not a re-encoding, so it matches what every other peer computes even when
    the file's dictionary keys are not in canonical order.

    Args:
        data: Raw contents of a ``.torrent`` file.

    Returns:
        A validated torrent.

    Raises:
        TorrentParseError: The input is not decodable as a torrent document.
        InvalidTorrentError: The document decodes but is not a valid torrent.
        UnsafePathError: A file path in the torrent is not safe to write.
    """
    raw = bytes(data)
    try:
        decoder = Decoder(raw)
        document, spans = decoder.decode_mapping_with_spans()
    except BencodeDecodeError as exc:
        raise TorrentParseError(f"not a valid torrent file: {exc}") from exc

    raw_info = document.get(b"info")
    if raw_info is None:
        raise InvalidTorrentError("torrent has no 'info' dictionary")
    if not isinstance(raw_info, dict):
        raise InvalidTorrentError(f"'info' must be a dictionary, got {type(raw_info).__name__}")

    start, end = spans[b"info"]
    info_hash = info_hash_from_bencoded(raw[start:end])

    if list(raw_info) != sorted(raw_info):
        logger.warning(
            "torrent %s has non-canonically ordered info keys; using the raw "
            "on-the-wire bytes for the info-hash so it matches other clients",
            info_hash.hex(),
        )

    return _build_torrent(
        info=raw_info,
        info_hash=info_hash,
        announce=_optional_tracker(document.get(b"announce"), field="announce"),
        announce_list=_parse_announce_list(document.get(b"announce-list")),
        creation_date=_optional_int(document.get(b"creation date"), field="creation date"),
        comment=_optional_text(document.get(b"comment"), field="comment"),
        created_by=_optional_text(document.get(b"created by"), field="created by"),
    )


def parse_torrent_file(path: str | Path) -> Torrent:
    """Read and parse a ``.torrent`` file from disk.

    Args:
        path: Filesystem path to the torrent file.

    Returns:
        A validated torrent.

    Raises:
        TorrentParseError: The file could not be read or parsed.
    """
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise TorrentParseError(f"cannot read torrent file {file_path}: {exc}") from exc
    return parse_torrent(data)


def torrent_from_info(
    info: Mapping[bytes, BencodeValue],
    *,
    announce: str | None = None,
    announce_list: tuple[tuple[str, ...], ...] = (),
) -> Torrent:
    """Build a torrent from a parsed ``info`` dictionary.

    Used when the original bytes are unavailable — the metadata-exchange path
    for magnet links (BEP 9), where the info dictionary arrives from a peer and
    is required to be canonically encoded.

    Args:
        info: The ``info`` mapping.
        announce: Optional primary tracker URL.
        announce_list: Optional tracker tiers.

    Returns:
        A validated torrent whose info-hash is the SHA-1 of the canonical
        re-encoding of ``info``.
    """
    if not isinstance(info, Mapping):  # defensive: caller data is untrusted
        raise InvalidTorrentError(f"'info' must be a dictionary, got {type(info).__name__}")
    return _build_torrent(
        info=info,
        info_hash=compute_info_hash(info),
        announce=announce,
        announce_list=announce_list,
    )


# --------------------------------------------------------------------- build


def _build_torrent(
    *,
    info: Mapping[bytes, BencodeValue],
    info_hash: bytes,
    announce: str | None,
    announce_list: tuple[tuple[str, ...], ...],
    creation_date: int | None = None,
    comment: str | None = None,
    created_by: str | None = None,
) -> Torrent:
    """Assemble and validate a :class:`Torrent` from an info dictionary."""
    name = sanitize_name(_require_bytes(info, b"name", field="info.name"), field="info.name")
    piece_length = _require_positive_int(info, b"piece length", field="info.piece length")
    piece_hashes = _parse_piece_hashes(info)
    files = _parse_files(info, name)

    _warn_on_unusual_piece_length(piece_length)
    _validate_piece_geometry(files, piece_length, piece_hashes)

    return Torrent(
        name=name,
        info_hash=info_hash,
        piece_length=piece_length,
        piece_hashes=piece_hashes,
        files=files,
        info=info,
        announce=announce,
        announce_list=announce_list,
        creation_date=creation_date,
        comment=comment,
        created_by=created_by,
        private=_parse_private_flag(info),
    )


def _parse_files(info: Mapping[bytes, BencodeValue], name: str) -> tuple[FileEntry, ...]:
    """Parse either the single-file or the multi-file form of ``info``.

    BEP 3 requires exactly one of ``length`` or ``files``; a torrent that
    declares both is ambiguous and is rejected rather than guessed at.
    """
    has_length = b"length" in info
    has_files = b"files" in info

    if has_length and has_files:
        raise InvalidTorrentError("'info' declares both 'length' and 'files'")
    if not has_length and not has_files:
        raise InvalidTorrentError("'info' declares neither 'length' nor 'files'")

    if has_length:
        length = _require_non_negative_int(info, b"length", field="info.length")
        return (FileEntry(path=PurePosixPath(name), length=length, offset=0),)

    entries = info[b"files"]
    if not isinstance(entries, list):
        raise InvalidTorrentError(f"'info.files' must be a list, got {type(entries).__name__}")
    if not entries:
        raise InvalidTorrentError("'info.files' is empty")

    files: list[FileEntry] = []
    seen: set[PurePosixPath] = set()
    offset = 0
    root = PurePosixPath(name)

    for index, entry in enumerate(entries):
        field_name = f"info.files[{index}]"
        if not isinstance(entry, dict):
            raise InvalidTorrentError(
                f"{field_name} must be a dictionary, got {type(entry).__name__}"
            )

        length = _require_non_negative_int(entry, b"length", field=f"{field_name}.length")
        raw_path = entry.get(b"path")
        if not isinstance(raw_path, list):
            raise InvalidTorrentError(
                f"{field_name}.path must be a list, got {type(raw_path).__name__}"
            )

        components: list[bytes] = []
        for part_index, part in enumerate(raw_path):
            if not isinstance(part, (bytes, bytearray, memoryview)):
                raise InvalidTorrentError(
                    f"{field_name}.path[{part_index}] must be a byte string, "
                    f"got {type(part).__name__}"
                )
            components.append(bytes(part))

        # UnsafePathError is a subclass of InvalidTorrentError, so it propagates
        # with its original field/value context intact: the UI needs to say
        # exactly which path in which file entry was rejected.
        relative = sanitize_path_components(components, field=f"{field_name}.path")
        full_path = root / relative
        if full_path in seen:
            raise InvalidTorrentError(f"duplicate file path {str(full_path)!r} in {field_name}")
        seen.add(full_path)

        files.append(FileEntry(path=full_path, length=length, offset=offset))
        offset += length

    return tuple(files)


def _parse_piece_hashes(info: Mapping[bytes, BencodeValue]) -> tuple[bytes, ...]:
    """Split the ``pieces`` blob into 20-byte SHA-1 hashes."""
    raw = info.get(b"pieces")
    if raw is None:
        raise InvalidTorrentError("'info' has no 'pieces' field")
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise InvalidTorrentError(f"'info.pieces' must be a byte string, got {type(raw).__name__}")
    blob = bytes(raw)
    if not blob:
        raise InvalidTorrentError("'info.pieces' is empty")
    if len(blob) % _HASH_SIZE:
        raise InvalidTorrentError(
            f"'info.pieces' length {len(blob)} is not a multiple of {_HASH_SIZE}"
        )
    return tuple(blob[i : i + _HASH_SIZE] for i in range(0, len(blob), _HASH_SIZE))


def _validate_piece_geometry(
    files: Sequence[FileEntry], piece_length: int, piece_hashes: Sequence[bytes]
) -> None:
    """Check that the file sizes, piece length and hash count agree.

    A mismatch here would silently mis-index every piece, so it is fatal rather
    than a warning: the downloader, storage layer and UI all trust this.
    """
    total_length = sum(entry.length for entry in files)
    if total_length <= 0:
        raise InvalidTorrentError(f"torrent has a total length of {total_length} bytes")

    expected = math.ceil(total_length / piece_length)
    if expected != len(piece_hashes):
        raise InvalidTorrentError(
            f"torrent declares {len(piece_hashes)} piece hashes but "
            f"{total_length} bytes at {piece_length} bytes/piece requires {expected}"
        )


def _warn_on_unusual_piece_length(piece_length: int) -> None:
    if piece_length % 16384:
        logger.warning("piece length %d is not a multiple of 16 KiB", piece_length)
    if not _MIN_TYPICAL_PIECE_LENGTH <= piece_length <= _MAX_TYPICAL_PIECE_LENGTH:
        logger.warning(
            "piece length %d is outside the typical %d-%d byte range",
            piece_length,
            _MIN_TYPICAL_PIECE_LENGTH,
            _MAX_TYPICAL_PIECE_LENGTH,
        )


def _parse_private_flag(info: Mapping[bytes, BencodeValue]) -> bool:
    """Read the BEP 27 ``private`` flag.

    A private torrent forbids DHT and PEX; honouring it is a correctness issue
    (announcing a private torrent to the DHT leaks its existence).
    """
    value = info.get(b"private")
    if value is None:
        return False
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidTorrentError(f"'info.private' must be an integer, got {type(value).__name__}")
    return value == 1


def _parse_announce_list(value: BencodeValue | None) -> tuple[tuple[str, ...], ...]:
    """Parse ``announce-list`` into tiers of tracker URLs (BEP 12).

    Malformed tiers are skipped with a warning rather than failing the whole
    torrent: a broken tier is not a reason to refuse to open a file.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        logger.warning("ignoring 'announce-list': expected a list of tiers")
        return ()

    tiers: list[tuple[str, ...]] = []
    for index, tier in enumerate(value):
        urls: tuple[BencodeValue, ...]
        if isinstance(tier, (bytes, bytearray)):
            # Some encoders emit a flat list of URLs instead of tiers.
            urls = (tier,)
        elif isinstance(tier, list):
            urls = tuple(item for item in tier if isinstance(item, (bytes, bytearray)))
        else:
            logger.warning("ignoring announce-list tier %d: unsupported entry type", index)
            continue

        cleaned = tuple(
            url
            for url in (
                _optional_tracker(candidate, field=f"announce-list[{index}]") for candidate in urls
            )
            if url is not None
        )
        if cleaned:
            tiers.append(cleaned)
    return tuple(tiers)


def _optional_tracker(value: BencodeValue | None, *, field: str) -> str | None:
    """Validate a tracker URL, returning ``None`` if unusable.

    Only http/https/udp are accepted: those are the protocols this client can
    speak. A torrent whose only tracker is, say, ``wss://`` is accepted (it may
    still work over DHT later) but the URL is dropped so the tracker manager
    never tries to open an impossible connection.
    """
    if value is None:
        return None
    if not isinstance(value, (bytes, bytearray)):
        logger.warning("ignoring %s: expected a byte string", field)
        return None

    url = bytes(value).decode("utf-8", errors="replace").strip()
    if not url:
        return None

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in _SUPPORTED_TRACKER_SCHEMES:
        logger.warning("ignoring tracker %r: unsupported scheme %r", url, parsed.scheme)
        return None
    if not parsed.hostname:
        logger.warning("ignoring tracker %r: no host", url)
        return None
    return url


# ------------------------------------------------------- typed field access


def _require_bytes(mapping: Mapping[bytes, BencodeValue], key: bytes, *, field: str) -> bytes:
    value = mapping.get(key)
    if value is None:
        raise InvalidTorrentError(f"missing required field '{field}'")
    if not isinstance(value, (bytes, bytearray)):
        raise InvalidTorrentError(
            f"field '{field}' must be a byte string, got {type(value).__name__}"
        )
    return bytes(value)


def _require_positive_int(mapping: Mapping[bytes, BencodeValue], key: bytes, *, field: str) -> int:
    value = _require_int(mapping, key, field=field)
    if value <= 0:
        raise InvalidTorrentError(f"field '{field}' must be positive, got {value}")
    return value


def _require_non_negative_int(
    mapping: Mapping[bytes, BencodeValue], key: bytes, *, field: str
) -> int:
    value = _require_int(mapping, key, field=field)
    if value < 0:
        raise InvalidTorrentError(f"field '{field}' must not be negative, got {value}")
    return value


def _require_int(mapping: Mapping[bytes, BencodeValue], key: bytes, *, field: str) -> int:
    value = mapping.get(key)
    if value is None:
        raise InvalidTorrentError(f"missing required field '{field}'")
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidTorrentError(f"field '{field}' must be an integer, got {type(value).__name__}")
    return value


def _optional_int(value: BencodeValue | None, *, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        logger.warning("ignoring %s: expected an integer", field)
        return None
    return value


def _optional_text(value: BencodeValue | None, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (bytes, bytearray)):
        logger.warning("ignoring %s: expected a byte string", field)
        return None
    return bytes(value).decode("utf-8", errors="replace")
