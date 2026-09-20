"""Torrent metadata: parsing, validation, identity and file layout.

The torrent is the root of the domain model — every other subsystem (tracker,
peer connections, download, storage, UI) is parameterised by one.

Quick start::

    from app.torrent import parse_torrent_file

    torrent = parse_torrent_file("ubuntu.torrent")
    print(torrent.name, torrent.total_length, torrent.hex_info_hash)

Exports:
    parse_torrent, parse_torrent_file, torrent_from_info — entry points
    Torrent, FileEntry                                   — domain model
    compute_info_hash, info_hash_from_bencoded,
    format_info_hash, parse_info_hash                    — identity helpers
    TorrentError and subclasses                          — error types
"""

from __future__ import annotations

from app.torrent.errors import (
    InvalidTorrentError,
    MagnetError,
    TorrentError,
    TorrentParseError,
    UnsafePathError,
    UnsupportedMagnetError,
)
from app.torrent.info_hash import (
    INFO_HASH_SIZE,
    compute_info_hash,
    format_info_hash,
    info_hash_from_bencoded,
    parse_info_hash,
    validate_info_hash,
    verify_info_dict,
)
from app.torrent.magnet import MagnetUri, is_magnet, magnet_for, parse_magnet
from app.torrent.metadata import FileEntry, Torrent
from app.torrent.parser import parse_torrent, parse_torrent_file, torrent_from_info

__all__ = [
    "INFO_HASH_SIZE",
    "FileEntry",
    "InvalidTorrentError",
    "MagnetError",
    "MagnetUri",
    "Torrent",
    "TorrentError",
    "TorrentParseError",
    "UnsafePathError",
    "UnsupportedMagnetError",
    "compute_info_hash",
    "format_info_hash",
    "info_hash_from_bencoded",
    "is_magnet",
    "magnet_for",
    "parse_info_hash",
    "parse_magnet",
    "parse_torrent",
    "parse_torrent_file",
    "torrent_from_info",
    "validate_info_hash",
    "verify_info_dict",
]
