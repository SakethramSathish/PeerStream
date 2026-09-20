"""Tests for the byte-stream → file mapping (TRD §29).

Geometry is pure arithmetic, so most of what matters here is testable without
touching a disk. The IO helpers at the end are the only part that does.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest
from app.storage.errors import AllocationError, StoragePathError
from app.storage.files import (
    AllocationResult,
    FileLayout,
    MappedFile,
    create_files,
    file_sizes,
    preallocate_file,
    read_chunk,
    write_chunk,
)
from app.torrent import Torrent
from app.torrent.metadata import FileEntry


def make_layout(root: Path, sizes: list[int]) -> FileLayout:
    """Build a layout of consecutive files with the given sizes."""
    files: list[MappedFile] = []
    offset = 0
    for index, size in enumerate(sizes):
        files.append(
            MappedFile(
                entry=FileEntry(PurePosixPath(f"file{index}.bin"), size, offset),
                path=root / f"file{index}.bin",
            )
        )
        offset += size
    return FileLayout(files=tuple(files), root=root, total_length=offset)


class TestMapping:
    def test_a_single_file_maps_straight_to_the_root(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        layout = FileLayout.from_torrent(sample_torrent, tmp_path)

        assert layout.file_count == 1
        assert layout.files[0].path == tmp_path / "payload.bin"
        assert layout.total_length == sample_torrent.total_length

    def test_multi_file_paths_keep_their_nesting(
        self, multi_file_torrent: Torrent, tmp_path: Path
    ) -> None:
        layout = FileLayout.from_torrent(multi_file_torrent, tmp_path / "dl")

        relative = [str(mapped.relative_path) for mapped in layout.files]
        assert relative == [
            "bundle/part000.bin",
            "bundle/part001.bin",
            "bundle/data/part002.bin",
            "bundle/part003.bin",
        ]
        assert layout.files[2].path == tmp_path / "dl" / "bundle" / "data" / "part002.bin"
        assert layout.root == (tmp_path / "dl").resolve()

    def test_offsets_and_lengths_describe_the_whole_stream(
        self, crossing_torrent: Torrent, tmp_path: Path
    ) -> None:
        layout = FileLayout.from_torrent(crossing_torrent, tmp_path)

        ends = [mapped.end_offset for mapped in layout.files]
        assert ends[-1] == layout.total_length == crossing_torrent.total_length


class TestSpans:
    def test_a_range_inside_one_file_stays_there(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10, 10])

        spans = list(layout.spans(2, 3))

        assert [(mapped.path.name, start, end) for mapped, start, end in spans] == [
            ("file0.bin", 2, 5)
        ]

    def test_a_range_across_two_files_is_split(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10, 10])

        spans = list(layout.spans(5, 10))

        assert [(mapped.path.name, start, end) for mapped, start, end in spans] == [
            ("file0.bin", 5, 10),
            ("file1.bin", 0, 5),
        ]

    def test_zero_length_files_are_skipped(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10, 0, 10])

        spans = list(layout.spans(0, 20))

        assert [mapped.path.name for mapped, _, _ in spans] == ["file0.bin", "file2.bin"]

    def test_pieces_of_a_real_torrent_cross_boundaries(
        self, crossing_torrent: Torrent, tmp_path: Path
    ) -> None:
        layout = FileLayout.from_torrent(crossing_torrent, tmp_path)
        piece_length = crossing_torrent.piece_length  # 100 KiB; files are 128 KiB

        spans = list(layout.spans(crossing_torrent.piece_offset(1), piece_length))

        assert [mapped.entry.path.name for mapped, _, _ in spans] == [
            "part000.bin",
            "part001.bin",
        ]
        # 28 KiB at the tail of the first file, 72 KiB at the head of the next.
        assert spans[0][1] == 100 * 1024
        assert spans[0][2] == 128 * 1024
        assert spans[1][1] == 0
        assert spans[1][2] == 72 * 1024

    def test_an_empty_range_yields_nothing(self, tmp_path: Path) -> None:
        assert list(make_layout(tmp_path, [10]).spans(0, 0)) == []

    def test_clip_returns_none_when_the_file_is_untouched(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10, 10])

        assert layout.files[0].clip(15, 20) is None

    def test_the_last_byte_of_the_torrent_is_addressable(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10])

        assert [(m.path.name, s, e) for m, s, e in layout.spans(9, 1)] == [("file0.bin", 9, 10)]


class TestPlan:
    def test_chunks_reassemble_into_the_original_data(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10, 10, 10])

        plan = layout.plan(5, b"abcdefghijklmnopqrst")

        assert bytes(b"".join(chunk for _, _, chunk in plan)) == b"abcdefghijklmnopqrst"

    def test_chunk_offsets_match_their_file(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [4, 4])

        plan = layout.plan(2, b"ABCDEF")

        assert [(mapped.path.name, offset, bytes(chunk)) for mapped, offset, chunk in plan] == [
            ("file0.bin", 2, b"AB"),
            ("file1.bin", 0, b"CDEF"),
        ]

    def test_a_range_past_the_end_is_refused(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10])

        with pytest.raises(StoragePathError, match="past the end"):
            layout.plan(5, b"abcdef")

    def test_negative_offsets_are_refused(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10])

        with pytest.raises(StoragePathError, match="negative"):
            layout.plan(-1, b"a")

    def test_metadata_claiming_more_than_the_files_hold_is_refused(self, tmp_path: Path) -> None:
        # A torrent whose declared length outruns its file list would silently
        # lose the tail of every write; the plan refuses instead.
        layout = FileLayout(
            files=(
                MappedFile(FileEntry(PurePosixPath("file0.bin"), 4, 0), tmp_path / "file0.bin"),
            ),
            root=tmp_path,
            total_length=100,
        )

        with pytest.raises(StoragePathError, match="only 4 bytes"):
            layout.plan(0, b"x" * 8)


class TestValidation:
    def test_an_empty_layout_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(StoragePathError, match="at least one file"):
            FileLayout(files=(), root=tmp_path, total_length=0)

    def test_two_entries_may_not_share_a_path(self, tmp_path: Path) -> None:
        files = (
            MappedFile(FileEntry(PurePosixPath("a.bin"), 5, 0), tmp_path / "a.bin"),
            MappedFile(FileEntry(PurePosixPath("sub/a.bin"), 5, 5), tmp_path / "a.bin"),
        )

        with pytest.raises(StoragePathError, match="same file"):
            FileLayout(files=files, root=tmp_path, total_length=10)

    def test_a_torrent_path_escaping_the_root_is_refused(self, tmp_path: Path) -> None:
        torrent = Torrent(
            name="evil",
            info_hash=bytes(20),
            piece_length=16,
            piece_hashes=(bytes(20),),
            files=(FileEntry(PurePosixPath("../escaped.bin"), 10, 0),),
        )

        with pytest.raises(StoragePathError, match="outside"):
            FileLayout.from_torrent(torrent, tmp_path / "dl")

    def test_directories_are_parents_first(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path, [10])
        nested = MappedFile(
            FileEntry(PurePosixPath("sub/deep/f.bin"), 5, 10), tmp_path / "sub" / "deep" / "f.bin"
        )
        layout = FileLayout(files=(*layout.files, nested), root=tmp_path, total_length=15)

        assert layout.directories[0] == tmp_path.resolve()
        assert tmp_path / "sub" / "deep" in layout.directories


class TestAllocation:
    def test_files_are_created_empty_when_preallocation_is_off(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path / "dl", [10, 0, 5])

        result = create_files(layout, preallocate=False)

        assert result == AllocationResult(
            file_count=3, total_bytes=15, bytes_allocated=0, preallocated=False
        )
        assert [path.exists() for path in (tmp_path / "dl" / f"file{i}.bin" for i in range(3))]
        assert list(file_sizes(layout)) == [0, 0, 0]

    def test_preallocation_reserves_the_declared_size(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path / "dl", [10, 0, 5])

        result = create_files(layout, preallocate=True)

        assert result.bytes_allocated == 15
        assert list(file_sizes(layout)) == [10, 0, 5]

    def test_preallocating_twice_allocates_nothing(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path / "dl", [10, 5])

        create_files(layout, preallocate=True)
        again = create_files(layout, preallocate=True)

        assert again.bytes_allocated == 0
        assert list(file_sizes(layout)) == [10, 5]

    def test_preallocation_never_shrinks_a_file(self, tmp_path: Path) -> None:
        path = tmp_path / "grown.bin"
        path.write_bytes(b"x" * 100)

        assert preallocate_file(path, 10) == 0
        assert path.stat().st_size == 100

    def test_a_file_is_grown_to_the_declared_length(self, tmp_path: Path) -> None:
        path = tmp_path / "partial.bin"
        path.write_bytes(b"x" * 4)

        assert preallocate_file(path, 10) == 6
        assert path.stat().st_size == 10
        # Existing bytes are preserved: resume must not throw away good data.
        assert read_chunk(path, 0, 4) == b"x" * 4

    def test_missing_parent_directories_are_created(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "c.bin"

        preallocate_file(path, 8)

        assert path.stat().st_size == 8

    def test_an_unwritable_directory_is_reported_as_allocation_failure(
        self, tmp_path: Path
    ) -> None:
        if os.geteuid() == 0:  # root ignores the write bit
            pytest.skip("running as root: permissions are not enforced")
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)

        with pytest.raises(AllocationError, match="cannot create"):
            create_files(make_layout(locked / "dl", [4]), preallocate=True)


class TestChunkIO:
    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "data.bin"
        path.write_bytes(b"\x00" * 10)

        written = write_chunk(path, 3, b"hello")
        read = read_chunk(path, 3, 5)

        assert (written, read) == (5, b"hello")
        assert path.read_bytes() == b"\x00\x00\x00hello\x00\x00"

    def test_writes_create_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "new.bin"

        assert write_chunk(path, 0, b"abc") == 3
        assert path.read_bytes() == b"abc"

    def test_reading_past_the_end_returns_what_exists(self, tmp_path: Path) -> None:
        path = tmp_path / "short.bin"
        path.write_bytes(b"abc")

        assert read_chunk(path, 0, 10) == b"abc"
        assert read_chunk(path, 10, 5) == b""

    def test_reading_a_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_chunk(tmp_path / "nope.bin", 0, 10) == b""

    def test_zero_length_reads_are_empty(self, tmp_path: Path) -> None:
        assert read_chunk(tmp_path / "whatever.bin", 0, 0) == b""

    def test_a_piece_is_scattered_and_gathered_across_files(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path / "dl", [4, 0, 4])
        create_files(layout, preallocate=True)
        data = b"ABCDEFGH"

        for mapped, offset, chunk in layout.plan(0, data):
            write_chunk(mapped.path, offset, chunk)

        gathered = b"".join(
            read_chunk(mapped.path, start, end - start) for mapped, start, end in layout.spans(0, 8)
        )
        assert gathered == data
        # The padding file in the middle stayed empty.
        assert (tmp_path / "dl" / "file1.bin").stat().st_size == 0


class TestFailurePaths:
    """Every way the filesystem can say no, said in our own words."""

    def test_a_negative_total_length_is_refused(self, tmp_path: Path) -> None:
        files = (MappedFile(FileEntry(PurePosixPath("a.bin"), 5, 0), tmp_path / "a.bin"),)

        with pytest.raises(StoragePathError, match="cannot be negative"):
            FileLayout(files=files, root=tmp_path, total_length=-1)

    def test_a_negative_length_range_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(StoragePathError, match="length cannot be negative"):
            list(make_layout(tmp_path, [10]).spans(0, -1))

    def test_a_file_that_cannot_be_stat_ed_is_allocation_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "weird.bin"
        target.write_bytes(b"data")  # exists, so its size will be checked
        real_stat = Path.stat

        def failing_stat(self: Path, **kwargs: object) -> object:
            if self == target:
                raise OSError(5, "I/O error")
            return real_stat(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", failing_stat)

        with pytest.raises(AllocationError, match="cannot stat"):
            preallocate_file(target, 10)

    def test_a_path_that_is_a_directory_cannot_be_grown(self, tmp_path: Path) -> None:
        # A directory already "exists", so only a request larger than it can
        # reach the open() that fails.
        with pytest.raises(AllocationError, match="cannot create"):
            preallocate_file(tmp_path, 1 << 20)

    def test_a_full_disk_is_reported_as_allocation_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno as errno_module

        def denied(fd: int, offset: int, length: int) -> None:
            raise OSError(errno_module.ENOSPC, "No space left on device")

        monkeypatch.setattr(os, "posix_fallocate", denied)

        with pytest.raises(AllocationError, match="cannot allocate"):
            preallocate_file(tmp_path / "big.bin", 1024)

    def test_a_filesystem_without_fallocate_falls_back_to_truncate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno as errno_module

        def unsupported(fd: int, offset: int, length: int) -> None:
            raise OSError(errno_module.EOPNOTSUPP, "Operation not supported")

        monkeypatch.setattr(os, "posix_fallocate", unsupported)
        path = tmp_path / "sparse.bin"

        preallocate_file(path, 4096)

        assert path.stat().st_size == 4096
        assert path.read_bytes() == bytes(4096)

    def test_a_file_that_cannot_be_created_fails_the_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing_touch(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError(5, "I/O error")

        monkeypatch.setattr(Path, "touch", failing_touch)

        with pytest.raises(AllocationError, match="cannot create"):
            create_files(make_layout(tmp_path, [10]), preallocate=False)

    def test_opening_a_directory_for_writing_fails(self, tmp_path: Path) -> None:
        with pytest.raises(AllocationError, match="cannot open"):
            write_chunk(tmp_path, 0, b"data")

    def test_a_failed_write_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(fd: int, buffer: object, offset: int) -> int:
            raise OSError(5, "I/O error")

        monkeypatch.setattr(os, "pwrite", boom)

        with pytest.raises(AllocationError, match="write to"):
            write_chunk(tmp_path / "data.bin", 0, b"data")

    def test_a_file_that_cannot_be_opened_reads_as_empty(self, tmp_path: Path) -> None:
        loop = tmp_path / "loop"
        os.symlink("loop", loop)

        assert read_chunk(loop, 0, 10) == b""

    def test_a_failed_read_returns_what_was_already_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "data.bin"
        path.write_bytes(b"abcdef")

        def flaky(fd: int, length: int, offset: int) -> bytes:
            if offset == 0:
                return b"abc"
            raise OSError(5, "I/O error")

        monkeypatch.setattr(os, "pread", flaky)

        assert read_chunk(path, 0, 6) == b"abc"

    def test_sizes_of_files_that_do_not_exist_are_zero(self, tmp_path: Path) -> None:
        assert file_sizes(make_layout(tmp_path / "missing", [4, 6])) == (0, 0)
