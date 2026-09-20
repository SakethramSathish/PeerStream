"""Security tests for the storage layer (TRD §41, FR-10).

Torrent metadata is attacker-supplied data, and a file list is a set of
instructions about where to write on the local disk. These tests are the ones
that must never be "optimised away": each one is a way a hostile torrent could
turn "add a torrent" into "overwrite a file I care about", plus the ways our own
resume data could be used against us after a restart.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest
from app.core.config import StorageConfig
from app.storage import StorageManager
from app.storage.errors import ResumeError, StoragePathError
from app.storage.files import FileLayout, MappedFile
from app.storage.resume import ResumeState, ResumeStore
from app.torrent import Torrent
from app.torrent.metadata import FileEntry

HOSTILE_HASH = bytes(range(20))


def torrent_with(*paths: str, length: int = 16) -> Torrent:
    """A torrent whose files are the given (hostile) paths."""
    files = tuple(
        FileEntry(PurePosixPath(path), length, index * length) for index, path in enumerate(paths)
    )
    return Torrent(
        name="hostile",
        info_hash=HOSTILE_HASH,
        piece_length=16,
        piece_hashes=(bytes(20),) * len(files),
        files=files,
    )


class TestPathContainment:
    def test_a_relative_escape_is_refused(self, tmp_path: Path) -> None:
        torrent = torrent_with("../../escaped.bin")

        with pytest.raises(StoragePathError, match="outside"):
            FileLayout.from_torrent(torrent, tmp_path / "dl")

    def test_an_absolute_path_is_refused(self, tmp_path: Path) -> None:
        torrent = torrent_with("/etc/passwd")

        with pytest.raises(StoragePathError, match="outside"):
            FileLayout.from_torrent(torrent, tmp_path / "dl")

    def test_a_symlink_out_of_the_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "dl"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "link").symlink_to(outside)

        torrent = torrent_with("link/escape.bin")

        with pytest.raises(StoragePathError, match="outside"):
            FileLayout.from_torrent(torrent, root)

        assert not (outside / "escape.bin").exists()

    def test_two_entries_may_not_alias_one_file(self, tmp_path: Path) -> None:
        torrent = torrent_with("a.bin", "./a.bin")

        with pytest.raises(StoragePathError, match="same file"):
            FileLayout.from_torrent(torrent, tmp_path / "dl")

    def test_a_dot_dot_component_is_refused_even_without_a_real_file(self, tmp_path: Path) -> None:
        files = (
            MappedFile(FileEntry(PurePosixPath("ok.bin"), 5, 0), tmp_path / "dl" / "ok.bin"),
            MappedFile(FileEntry(PurePosixPath("../bad.bin"), 5, 5), tmp_path / "bad.bin"),
        )

        with pytest.raises(StoragePathError):
            FileLayout(files=files, root=tmp_path / "dl", total_length=10)

    def test_a_hostile_torrent_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure happens at construction, before any file is created."""
        root = tmp_path / "dl"

        with pytest.raises(StoragePathError):
            StorageManager(torrent_with("../../pwned.bin"), root)

        assert not (tmp_path / "pwned.bin").exists()
        assert not root.exists()


class TestWriteBounds:
    async def test_a_piece_cannot_be_written_past_the_end_of_the_torrent(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = StorageManager(sample_torrent, tmp_path / "dl")
        await storage.prepare()

        # Valid data, valid piece index, but one byte too many: the last piece
        # is exactly as long as the metainfo says, no more.
        with pytest.raises(ValueError, match="must be"):
            await storage.write_piece(
                sample_torrent.piece_count - 1,
                b"x" * (sample_torrent.last_piece_length + 1),
            )
        await storage.aclose()

    async def test_a_write_cannot_overflow_into_the_next_piece(
        self, sample_torrent: Torrent, tmp_path: Path, payload: bytes
    ) -> None:
        storage = StorageManager(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        piece_length = sample_torrent.piece_length
        data = payload[:piece_length]

        await storage.write_piece(0, data)

        on_disk = (storage.root / "payload.bin").read_bytes()
        assert on_disk[piece_length : piece_length + 16] == bytes(16)
        await storage.aclose()

    def test_a_range_outside_the_stream_is_refused(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        layout = FileLayout.from_torrent(sample_torrent, tmp_path)

        with pytest.raises(StoragePathError, match="past the end"):
            layout.plan(sample_torrent.total_length - 1, b"x" * 100)

    async def test_blocks_may_not_leave_their_piece(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = StorageManager(sample_torrent, tmp_path / "dl")
        await storage.prepare()

        with pytest.raises(ValueError, match="outside piece"):
            await storage.read_block(0, -1, 16)
        with pytest.raises(ValueError, match="outside piece"):
            await storage.read_block(0, 0, sample_torrent.piece_length + 1)
        await storage.aclose()


class TestResumeHardening:
    def test_a_piece_index_outside_the_torrent_is_refused(self) -> None:
        document = {
            "info_hash": HOSTILE_HASH.hex(),
            "name": "x",
            "piece_length": 16,
            "piece_count": 4,
            "total_length": 64,
            "completed_pieces": [0, 3, 99],
        }

        with pytest.raises(ResumeError, match="outside"):
            ResumeState.from_dict(document)

    def test_a_piece_list_longer_than_the_torrent_is_normalised(self) -> None:
        # Duplicates collapse, but every index still has to exist.
        document = {
            "info_hash": HOSTILE_HASH.hex(),
            "piece_length": 16,
            "piece_count": 4,
            "total_length": 64,
            "completed_pieces": [0, 0, 0, 1],
        }

        assert ResumeState.from_dict(document).completed_pieces == (0, 1)

    def test_a_huge_piece_list_is_still_checked(self) -> None:
        document = {
            "info_hash": HOSTILE_HASH.hex(),
            "piece_length": 16,
            "piece_count": 4,
            "total_length": 64,
            "completed_pieces": list(range(10_000)),
        }

        with pytest.raises(ResumeError, match="outside"):
            ResumeState.from_dict(document)

    def test_a_resume_file_name_cannot_escape_the_state_directory(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path / "state")

        path = store.path_for(b"\x2e\x2e" * 10)

        assert path.parent == store.directory
        assert ".." not in path.name

    async def test_resume_state_cannot_redirect_where_data_is_written(
        self, sample_torrent: Torrent, tmp_path: Path, payload: bytes
    ) -> None:
        """A state file claiming another directory must not move the download."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        state_directory = tmp_path / "state"
        state = ResumeState.from_torrent(sample_torrent, directory=elsewhere)
        state_directory.mkdir()
        (state_directory / f"{sample_torrent.hex_info_hash}.resume.json").write_text(
            json.dumps(state.to_dict())
        )

        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=state_directory),
        )
        await storage.prepare()
        await storage.load_resume()
        await storage.write_piece(0, payload[: sample_torrent.piece_length])

        assert (storage.root / "payload.bin").exists()
        assert not (elsewhere / "payload.bin").exists()
        assert list(elsewhere.iterdir()) == []
        await storage.aclose()

    async def test_corrupt_resume_data_is_quarantined_not_trusted(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        state_directory = tmp_path / "state"
        state_directory.mkdir()
        path = state_directory / f"{sample_torrent.hex_info_hash}.resume.json"
        path.write_text('{"info_hash": "zz", "completed_pieces": [0, 1]}')

        storage = StorageManager(
            sample_torrent,
            tmp_path / "dl",
            config=StorageConfig(state_directory=state_directory),
        )
        assert await storage.load_resume() is None
        assert path.with_suffix(".json.corrupt").exists()
        await storage.aclose()
