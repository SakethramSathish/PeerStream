"""Tests for the storage check tool.

The tool is the storage layer's end-to-end proof, so these tests drive it the
same way the CLI does: real payload, real files, real hashes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.torrent import Torrent, parse_torrent
from tools.make_test_torrent import build_torrent_bytes
from tools.storage_check import (
    StorageCheck,
    build_parser,
    corrupt_piece,
    main,
    parse_size,
    render,
    run_check,
)

SIZE = 256 * 1024
PIECE_LENGTH = 64 * 1024


def make_torrent(payload: bytes, *, file_count: int = 3) -> Torrent:
    return parse_torrent(
        build_torrent_bytes(payload, name="check", piece_length=PIECE_LENGTH, file_count=file_count)
    )


@pytest.fixture
def small_payload() -> bytes:
    from tools.make_test_torrent import generate_payload

    return generate_payload(SIZE, seed=7)


class TestRunCheck:
    async def test_every_piece_lands_on_disk(self, small_payload: bytes, tmp_path: Path) -> None:
        torrent = make_torrent(small_payload)

        check = await run_check(
            torrent,
            small_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
        )

        assert check.pieces_written == torrent.piece_count
        assert check.valid_pieces == torrent.piece_count
        assert check.pieces_rejected == ()
        assert check.invalid_pieces == ()
        assert check.files_ok is True
        assert check.successful is True
        assert check.bytes_on_disk == torrent.total_length
        assert check.resumed_pieces == torrent.piece_count

    async def test_files_match_the_torrent_layout(
        self, small_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(small_payload)

        check = await run_check(
            torrent,
            small_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
        )

        assert [file.declared for file in check.files] == [entry.length for entry in torrent.files]
        assert [file.on_disk for file in check.files] == [entry.length for entry in torrent.files]
        assert check.files[2].path == "check/data/part002.bin"

    async def test_a_corrupt_piece_never_reaches_disk(
        self, small_payload: bytes, tmp_path: Path
    ) -> None:
        # Single file, so the rejected piece's bytes are easy to point at.
        torrent = make_torrent(small_payload, file_count=1)

        check = await run_check(
            torrent,
            small_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            corrupt=[1],
        )

        assert check.pieces_rejected == (1,)
        assert check.pieces_written == torrent.piece_count - 1
        assert check.successful is False
        # The rejected piece's range is still zeroed, not half-written.
        path = tmp_path / "dl" / "check"
        assert path.read_bytes()[PIECE_LENGTH : PIECE_LENGTH * 2] == bytes(PIECE_LENGTH)

    async def test_preallocation_can_be_switched_off(
        self, small_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(small_payload)

        check = await run_check(
            torrent,
            small_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            preallocate=False,
        )

        assert check.preallocated is False
        assert check.bytes_allocated == 0
        assert check.successful is True

    async def test_a_payload_that_is_not_the_torrent_is_refused(
        self, small_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(small_payload)

        with pytest.raises(ValueError, match="payload is"):
            await run_check(
                torrent,
                small_payload[:-1],
                directory=tmp_path / "dl",
                state_directory=tmp_path / "state",
            )

    async def test_resume_state_is_written_where_it_was_asked_to_go(
        self, small_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(small_payload)

        check = await run_check(
            torrent,
            small_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
        )

        assert check.resume_path == str(tmp_path / "state" / f"{torrent.hex_info_hash}.resume.json")
        assert Path(check.resume_path).exists()


class TestCorruptPiece:
    def test_the_length_is_preserved(self) -> None:
        data = b"abcdef"

        assert len(corrupt_piece(data)) == len(data)
        assert corrupt_piece(data) != data

    def test_empty_data_is_left_alone(self) -> None:
        assert corrupt_piece(b"") == b""


class TestRendering:
    def test_a_successful_run_reads_as_successful(self, capsys: pytest.CaptureFixture[str]) -> None:
        check = StorageCheck(
            name="check",
            hex_info_hash="ab" * 20,
            total_length=SIZE,
            piece_count=4,
            piece_length=PIECE_LENGTH,
            files=(
                __import__("tools.storage_check", fromlist=["FileCheck"]).FileCheck(
                    "check/a.bin", 100, 100
                ),
            ),
            pieces_written=4,
            pieces_rejected=(),
            valid_pieces=4,
            invalid_pieces=(),
            resumed_pieces=4,
            resume_path="/tmp/state/ab.resume.json",
            directory="/tmp/dl",
            bytes_allocated=SIZE,
            preallocated=True,
            elapsed=0.5,
        )

        render(check, title="Storage check: check")

        out = capsys.readouterr().out
        assert "Storage check: check" in out
        assert "256.0 KiB" in out
        assert "wrote 4/4 pieces" in out
        assert "RESULT ok" in out
        assert "/tmp/dl" in out

    def test_failures_are_spelled_out(self, capsys: pytest.CaptureFixture[str]) -> None:
        from tools.storage_check import FileCheck

        check = StorageCheck(
            name="check",
            hex_info_hash="ab" * 20,
            total_length=SIZE,
            piece_count=4,
            piece_length=PIECE_LENGTH,
            files=(FileCheck("check/a.bin", 100, 99),),
            pieces_written=3,
            pieces_rejected=(2,),
            valid_pieces=3,
            invalid_pieces=(4,),
            resumed_pieces=3,
            resume_path="/tmp/state/ab.resume.json",
            directory="/tmp/dl",
            bytes_allocated=0,
            preallocated=False,
            elapsed=0.25,
        )

        render(check, title="Storage check: check")

        out = capsys.readouterr().out
        assert "BAD " in out
        assert "rejected     1 piece(s) before writing: [2]" in out
        assert "preallocation disabled" in out
        assert "RESULT failed" in out


class TestCommandLine:
    def test_a_generated_torrent_is_stored_and_verified(self, tmp_path: Path) -> None:
        exit_code = main(
            [
                "--size",
                "512KiB",
                "--piece-length",
                str(PIECE_LENGTH),
                "--files",
                "3",
                "--out",
                str(tmp_path / "dl"),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )

        assert exit_code == 0
        assert (tmp_path / "dl" / "storage-check" / "part000.bin").exists()

    def test_a_corrupted_piece_is_reported_but_not_fatal(self, tmp_path: Path) -> None:
        exit_code = main(
            [
                "--size",
                "256KiB",
                "--piece-length",
                str(PIECE_LENGTH),
                "--out",
                str(tmp_path / "dl"),
                "--state-dir",
                str(tmp_path / "state"),
                "--corrupt",
                "1",
            ]
        )

        assert exit_code == 0

    def test_an_unreadable_torrent_is_reported(self, tmp_path: Path) -> None:
        exit_code = main(["--torrent", str(tmp_path / "missing.torrent")])

        assert exit_code == 1

    def test_a_torrent_without_a_payload_is_rejected(self, tmp_path: Path) -> None:
        torrent_path = tmp_path / "a.torrent"
        torrent_path.write_bytes(build_torrent_bytes(b"x" * 1024, name="a", piece_length=1024))

        assert main(["--torrent", str(torrent_path), "--out", str(tmp_path / "dl")]) == 1

    def test_a_real_torrent_and_payload_round_trip(self, tmp_path: Path) -> None:
        payload = b"payload data" * 100
        torrent_bytes = build_torrent_bytes(payload, name="real", piece_length=512, file_count=2)
        torrent_path = tmp_path / "real.torrent"
        payload_path = tmp_path / "real.bin"
        torrent_path.write_bytes(torrent_bytes)
        payload_path.write_bytes(payload)

        exit_code = main(
            [
                "--torrent",
                str(torrent_path),
                "--payload",
                str(payload_path),
                "--out",
                str(tmp_path / "dl"),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )

        assert exit_code == 0
        assert (tmp_path / "dl" / "real" / "part000.bin").read_bytes() == payload[:600]

    def test_the_parser_accepts_human_sizes(self) -> None:
        args = build_parser().parse_args(["--size", "4MiB", "--files", "2"])

        assert parse_size(args.size) == 4 * 1024 * 1024
        assert args.files == 2
