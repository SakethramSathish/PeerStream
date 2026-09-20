"""Torrent domain model.

This module holds the immutable value objects that the rest of the client is
built around: :class:`FileEntry` (one file inside a torrent) and
:class:`Torrent` (a fully validated, ready-to-use torrent).

Everything here is derived from info in the PRD/TRD §20-§21: a torrent is a
name, an identity (``info_hash``), a piece geometry, and a list of files mapped
onto one continuous byte stream. The torrent is *the* byte stream; individual
files are just windows onto it. That mental model is what makes multi-file
support fall out for free later, when the storage layer (M6) writes a piece
that happens to straddle two files.

Invariants enforced at construction time (fail fast, never halfway):

* ``piece_length`` is positive;
* ``info_hash`` is exactly 20 bytes;
* every piece hash is exactly 20 bytes;
* there is at least one file and the total length is positive;
* the declared piece count matches ``ceil(total_length / piece_length)``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.bencode.decoder import BencodeValue
from app.torrent.errors import InvalidTorrentError
from app.torrent.info_hash import INFO_HASH_SIZE, format_info_hash


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One file within a torrent.

    Attributes:
        path: Path relative to the download directory. For multi-file torrents
            this includes the torrent root name (``root/sub/file.bin``); for
            single-file torrents it is just the torrent name.
        length: Size of the file in bytes. May be zero (padding files exist in
            the wild, and BEP 47 allows explicit padding).
        offset: Where the file starts within the torrent's byte stream.
    """

    path: PurePosixPath
    length: int
    offset: int

    def __post_init__(self) -> None:
        if self.length < 0:
            raise InvalidTorrentError(f"file {self.path} has negative length {self.length}")
        if self.offset < 0:
            raise InvalidTorrentError(f"file {self.path} has negative offset {self.offset}")

    @property
    def end_offset(self) -> int:
        """Offset one past the last byte of this file in the torrent stream."""
        return self.offset + self.length

    def contains_offset(self, offset: int) -> bool:
        """True when a torrent-stream offset lies inside this file."""
        return self.offset <= offset < self.end_offset


@dataclass(frozen=True, slots=True)
class Torrent:
    """A validated torrent.

    Constructed by :func:`app.torrent.parser.parse_torrent`, never by hand —
    the invariants below are checked in ``__post_init__`` so that no other part
    of the codebase ever has to re-validate a torrent.

    Attributes:
        name: Torrent name; the download directory for multi-file torrents.
        info_hash: 20-byte SHA-1 identity of the torrent.
        piece_length: Nominal piece size in bytes (last piece may be shorter).
        piece_hashes: Expected SHA-1 of each piece, in piece order.
        files: Files in stream order, with offsets covering the whole stream.
        info: The raw ``info`` dictionary, retained for metadata exchange
            (BEP 9) and for the UI's raw-metadata view.
        announce: Primary tracker URL, if any.
        announce_list: Tracker tiers; each tier is tried before the next.
        creation_date: POSIX timestamp from the metadata, if present.
        comment: Free-text comment from the metadata, if present.
        created_by: Program that created the torrent, if recorded.
        private: True when the torrent forbids DHT/PEX (BEP 27).
    """

    name: str
    info_hash: bytes
    piece_length: int
    piece_hashes: tuple[bytes, ...]
    files: tuple[FileEntry, ...]
    info: Mapping[bytes, BencodeValue] = field(default_factory=dict)
    announce: str | None = None
    announce_list: tuple[tuple[str, ...], ...] = ()
    creation_date: int | None = None
    comment: str | None = None
    created_by: str | None = None
    private: bool = False

    def __post_init__(self) -> None:
        if self.piece_length <= 0:
            raise InvalidTorrentError(f"piece length must be positive, got {self.piece_length}")
        if len(self.info_hash) != INFO_HASH_SIZE:
            raise InvalidTorrentError(
                f"info-hash must be {INFO_HASH_SIZE} bytes, got {len(self.info_hash)}"
            )
        if not self.files:
            raise InvalidTorrentError("torrent must contain at least one file")

        for index, piece_hash in enumerate(self.piece_hashes):
            if len(piece_hash) != INFO_HASH_SIZE:
                raise InvalidTorrentError(
                    f"piece {index} hash must be {INFO_HASH_SIZE} bytes, got {len(piece_hash)}"
                )

        expected_pieces = (self.total_length + self.piece_length - 1) // self.piece_length
        if len(self.piece_hashes) != expected_pieces:
            raise InvalidTorrentError(
                f"torrent declares {len(self.piece_hashes)} pieces but "
                f"{self.total_length} bytes at {self.piece_length} bytes/piece "
                f"requires {expected_pieces}"
            )

    # ------------------------------------------------------------- geometry

    @property
    def piece_count(self) -> int:
        """Number of pieces in the torrent."""
        return len(self.piece_hashes)

    @property
    def total_length(self) -> int:
        """Total size of all files, in bytes (the length of the byte stream)."""
        return sum(entry.length for entry in self.files)

    @property
    def is_single_file(self) -> bool:
        """True when the whole torrent is one file at the root of its directory."""
        return len(self.files) == 1 and self.files[0].path == PurePosixPath(self.name)

    @property
    def last_piece_length(self) -> int:
        """Size of the final piece; it is usually shorter than ``piece_length``."""
        return self.piece_size(self.piece_count - 1)

    @property
    def hex_info_hash(self) -> str:
        """The info-hash as lowercase hex, for display and logging."""
        return format_info_hash(self.info_hash)

    @property
    def trackers(self) -> tuple[str, ...]:
        """Every distinct tracker URL, primary first, preserving tier order."""
        ordered: list[str] = []
        if self.announce:
            ordered.append(self.announce)
        for tier in self.announce_list:
            for url in tier:
                if url not in ordered:
                    ordered.append(url)
        return tuple(ordered)

    # -------------------------------------------------------------- lookups

    def piece_offset(self, index: int) -> int:
        """Byte offset of a piece within the torrent stream."""
        self._check_index(index)
        return index * self.piece_length

    def piece_size(self, index: int) -> int:
        """Size of a piece in bytes; the last piece may be shorter."""
        self._check_index(index)
        if index == self.piece_count - 1:
            return self.total_length - (index * self.piece_length)
        return self.piece_length

    def piece_hash(self, index: int) -> bytes:
        """Expected SHA-1 hash of a piece."""
        self._check_index(index)
        return self.piece_hashes[index]

    def files_in_piece(self, index: int) -> Iterator[FileEntry]:
        """Yield every file that a piece touches (usually one, sometimes several)."""
        start = self.piece_offset(index)
        end = start + self.piece_size(index)
        for entry in self.files:
            if entry.offset < end and entry.end_offset > start:
                yield entry

    def _check_index(self, index: int) -> None:
        if not 0 <= index < self.piece_count:
            raise IndexError(f"piece index {index} out of range (0..{self.piece_count - 1})")
