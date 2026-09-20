"""Unit tests for the Torrent and FileEntry domain objects.

The torrent's piece geometry (offsets, sizes, hashes) is the contract that the
downloader, storage layer and UI all rely on. If ``piece_size`` is wrong, the
client writes corrupt files and reports 100% success, so these properties are
pinned down exactly, including the last-piece edge case.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from app.torrent import FileEntry, Torrent
from app.torrent.errors import InvalidTorrentError

HASH_A = bytes([0xAA] * 20)
HASH_B = bytes([0xBB] * 20)
HASH_C = bytes([0xCC] * 20)


def make_torrent(**overrides: object) -> Torrent:
    """Build a small valid torrent: 10 bytes, 4-byte pieces, 3 pieces."""
    kwargs: dict[str, object] = {
        "name": "demo.bin",
        "info_hash": bytes(20),
        "piece_length": 4,
        "piece_hashes": (HASH_A, HASH_B, HASH_C),
        "files": (FileEntry(path=PurePosixPath("demo.bin"), length=10, offset=0),),
    }
    kwargs.update(overrides)
    return Torrent(**kwargs)  # type: ignore[arg-type]


class TestFileEntry:
    def test_end_offset(self) -> None:
        entry = FileEntry(path=PurePosixPath("a.bin"), length=10, offset=5)
        assert entry.end_offset == 15

    def test_contains_offset(self) -> None:
        entry = FileEntry(path=PurePosixPath("a.bin"), length=10, offset=5)
        assert entry.contains_offset(5)
        assert entry.contains_offset(14)
        assert not entry.contains_offset(4)
        assert not entry.contains_offset(15)

    def test_rejects_negative_length(self) -> None:
        with pytest.raises(InvalidTorrentError, match="negative length"):
            FileEntry(path=PurePosixPath("a.bin"), length=-1, offset=0)

    def test_rejects_negative_offset(self) -> None:
        with pytest.raises(InvalidTorrentError, match="negative offset"):
            FileEntry(path=PurePosixPath("a.bin"), length=1, offset=-1)

    def test_zero_length_file_is_allowed(self) -> None:
        entry = FileEntry(path=PurePosixPath("pad"), length=0, offset=0)
        assert entry.end_offset == 0
        assert not entry.contains_offset(0)


class TestGeometry:
    def test_piece_count_and_total_length(self) -> None:
        torrent = make_torrent()
        assert torrent.piece_count == 3
        assert torrent.total_length == 10

    def test_piece_offsets(self) -> None:
        torrent = make_torrent()
        assert [torrent.piece_offset(i) for i in range(3)] == [0, 4, 8]

    def test_piece_sizes(self) -> None:
        torrent = make_torrent()
        assert [torrent.piece_size(i) for i in range(3)] == [4, 4, 2]

    def test_last_piece_length(self) -> None:
        assert make_torrent().last_piece_length == 2

    def test_last_piece_is_full_when_length_divides_evenly(self) -> None:
        torrent = make_torrent(
            piece_length=5,
            piece_hashes=(HASH_A, HASH_B),
            files=(FileEntry(path=PurePosixPath("demo.bin"), length=10, offset=0),),
        )
        assert torrent.piece_count == 2
        assert torrent.last_piece_length == 5

    def test_piece_hashes_are_returned_by_index(self) -> None:
        torrent = make_torrent()
        assert torrent.piece_hash(0) == HASH_A
        assert torrent.piece_hash(2) == HASH_C

    def test_single_file_detection(self) -> None:
        assert make_torrent().is_single_file
        multi = make_torrent(
            name="bundle",
            files=(
                FileEntry(path=PurePosixPath("bundle/a.bin"), length=5, offset=0),
                FileEntry(path=PurePosixPath("bundle/b.bin"), length=5, offset=5),
            ),
        )
        assert not multi.is_single_file

    def test_out_of_range_index_raises(self) -> None:
        torrent = make_torrent()
        for method in (torrent.piece_offset, torrent.piece_size, torrent.piece_hash):
            with pytest.raises(IndexError, match="out of range"):
                method(3)
            with pytest.raises(IndexError):
                method(-1)

    def test_hex_info_hash(self) -> None:
        torrent = make_torrent(info_hash=bytes(range(20)))
        assert torrent.hex_info_hash == bytes(range(20)).hex()


class TestFilesInPiece:
    """Pieces frequently straddle two files; the UI and storage both need this."""

    def _two_file_torrent(self) -> Torrent:
        return make_torrent(
            name="bundle",
            piece_length=4,
            piece_hashes=(HASH_A, HASH_B, HASH_C),
            files=(
                FileEntry(path=PurePosixPath("bundle/a.bin"), length=6, offset=0),
                FileEntry(path=PurePosixPath("bundle/b.bin"), length=6, offset=6),
            ),
        )

    def test_piece_inside_a_single_file(self) -> None:
        torrent = self._two_file_torrent()
        assert [entry.path.name for entry in torrent.files_in_piece(0)] == ["a.bin"]

    def test_piece_straddling_two_files(self) -> None:
        torrent = self._two_file_torrent()
        # Piece 1 covers bytes 4..8, which spans a.bin (0..6) and b.bin (6..12).
        assert [entry.path.name for entry in torrent.files_in_piece(1)] == ["a.bin", "b.bin"]

    def test_final_piece(self) -> None:
        torrent = self._two_file_torrent()
        assert [entry.path.name for entry in torrent.files_in_piece(2)] == ["b.bin"]


class TestInvariants:
    def test_rejects_non_positive_piece_length(self) -> None:
        with pytest.raises(InvalidTorrentError, match="piece length must be positive"):
            make_torrent(piece_length=0)

    def test_rejects_wrong_info_hash_size(self) -> None:
        with pytest.raises(InvalidTorrentError, match="info-hash must be 20 bytes"):
            make_torrent(info_hash=b"\x00" * 19)

    def test_rejects_no_files(self) -> None:
        with pytest.raises(InvalidTorrentError, match="at least one file"):
            make_torrent(files=())

    def test_rejects_short_piece_hash(self) -> None:
        with pytest.raises(InvalidTorrentError, match="piece 1 hash"):
            make_torrent(piece_hashes=(HASH_A, b"\x00" * 19, HASH_C))

    def test_rejects_piece_count_mismatch(self) -> None:
        # 10 bytes at 4 bytes/piece needs 3 pieces; two hashes is corrupt.
        with pytest.raises(InvalidTorrentError, match="requires 3"):
            make_torrent(piece_hashes=(HASH_A, HASH_B))


class TestTrackers:
    def test_primary_tracker_comes_first(self) -> None:
        torrent = make_torrent(
            announce="http://primary/announce",
            announce_list=(("http://tier1-a/announce",), ("http://tier2/announce",)),
        )
        assert torrent.trackers == (
            "http://primary/announce",
            "http://tier1-a/announce",
            "http://tier2/announce",
        )

    def test_duplicate_urls_are_collapsed(self) -> None:
        torrent = make_torrent(
            announce="http://primary/announce",
            announce_list=(("http://primary/announce",), ("http://other/announce",)),
        )
        assert torrent.trackers == ("http://primary/announce", "http://other/announce")

    def test_no_trackers(self) -> None:
        assert make_torrent().trackers == ()
