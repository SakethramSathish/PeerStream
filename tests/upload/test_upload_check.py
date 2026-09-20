"""Tests for the upload check tool.

The tool is the upload engine's end-to-end proof, so these tests drive it the
way the CLI does: real payload on real disk, real leechers on real sockets,
real engine — and every block the leechers received is compared against the
payload afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.torrent import Torrent, parse_torrent
from tools.make_test_torrent import build_torrent_bytes, generate_payload
from tools.upload_check import (
    UploadCheck,
    build_parser,
    main,
    render,
    run_upload_check,
)

SIZE = 256 * 1024
PIECE_LENGTH = 32 * 1024


def make_torrent(payload: bytes) -> Torrent:
    return parse_torrent(
        build_torrent_bytes(payload, name="seed me", piece_length=PIECE_LENGTH, file_count=1)
    )


@pytest.fixture
def seed_payload() -> bytes:
    return generate_payload(SIZE, seed=7)


class TestRunUploadCheck:
    async def test_a_leecher_receives_the_real_bytes(
        self, seed_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(seed_payload)

        check = await run_upload_check(
            torrent,
            seed_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            leechers=1,
        )

        assert check.blocks_served == PIECE_LENGTH // (16 * 1024)
        assert check.blocks_checked == check.blocks_served
        assert check.blocks_correct == check.blocks_checked
        assert check.successful is True

    async def test_several_leechers_are_served(self, seed_payload: bytes, tmp_path: Path) -> None:
        torrent = make_torrent(seed_payload)

        check = await run_upload_check(
            torrent,
            seed_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            leechers=3,
        )

        assert check.leechers == 3
        assert check.blocks_served == 3 * (PIECE_LENGTH // (16 * 1024))
        assert check.successful is True

    async def test_one_slot_means_most_leechers_wait(
        self, seed_payload: bytes, tmp_path: Path
    ) -> None:
        """Capacity is capacity: with one slot, most of the swarm is choked."""
        torrent = make_torrent(seed_payload)

        check = await run_upload_check(
            torrent,
            seed_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            leechers=3,
            slots=1,
        )

        assert check.peers_unchoked <= 2  # one slot plus the optimistic one
        assert check.successful is True

    async def test_a_rate_limit_is_respected(self, seed_payload: bytes, tmp_path: Path) -> None:
        torrent = make_torrent(seed_payload)

        check = await run_upload_check(
            torrent,
            seed_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            leechers=1,
            rate=64 * 1024,
        )

        assert check.bytes_uploaded == PIECE_LENGTH
        assert check.bytes_per_second < 400 * 1024  # visibly slower than uncapped

    async def test_hostile_requests_are_refused_not_served(
        self, seed_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(seed_payload)

        check = await run_upload_check(
            torrent,
            seed_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            leechers=1,
            hostile=True,
        )

        assert check.hostile_refused == 2
        assert check.rejected.get("bad_index") == 1
        assert check.rejected.get("bad_length") == 1
        assert check.successful is True


class TestRender:
    def test_a_good_run_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        check = UploadCheck(
            name="seed me",
            hex_info_hash="b" * 40,
            total_length=4 * 1024 * 1024,
            piece_count=16,
            leechers=3,
            blocks_served=48,
            bytes_uploaded=768 * 1024,
            requests_received=48,
            rejected={},
            blocks_checked=48,
            blocks_correct=48,
            peers_unchoked=2,
            elapsed=0.5,
            hostile_refused=0,
        )

        render(check, directory=Path("/tmp/uc"))

        out = capsys.readouterr().out
        assert "4.0 MiB in 16 piece(s)" in out
        assert "48/48 block(s) match the payload" in out
        assert "3 leecher(s), 2 unchoked" in out
        assert "RESULT ok" in out

    def test_refusals_are_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        check = UploadCheck(
            name="seed me",
            hex_info_hash="b" * 40,
            total_length=1024,
            piece_count=1,
            leechers=1,
            blocks_served=1,
            bytes_uploaded=16 * 1024,
            requests_received=3,
            rejected={"bad_index": 1, "bad_length": 1},
            blocks_checked=1,
            blocks_correct=1,
            peers_unchoked=1,
            elapsed=0.1,
            hostile_refused=2,
        )

        render(check, directory=Path("/tmp/uc"))

        out = capsys.readouterr().out
        assert "refusals     bad_index x1, bad_length x1" in out
        assert "3 received, 2 refused" in out

    def test_a_run_that_served_nothing_is_a_failure(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        check = UploadCheck(
            name="seed me",
            hex_info_hash="b" * 40,
            total_length=1024,
            piece_count=1,
            leechers=1,
            blocks_served=0,
            bytes_uploaded=0,
            requests_received=0,
            rejected={},
            blocks_checked=0,
            blocks_correct=0,
            peers_unchoked=0,
            elapsed=0.1,
            hostile_refused=0,
        )

        render(check, directory=Path("/tmp/uc"))

        assert check.successful is False
        assert "RESULT failed" in capsys.readouterr().out


class TestCommandLine:
    def test_the_tool_seeds_and_reports(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            [
                "--size",
                "256KiB",
                "--piece-length",
                str(PIECE_LENGTH),
                "--leechers",
                "2",
                "--out",
                str(tmp_path / "dl"),
                "--state-dir",
                str(tmp_path / "state"),
                "--keep",
            ]
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Upload check" in out
        assert "RESULT ok" in out
        assert (tmp_path / "dl").is_dir()

    def test_the_parser_has_the_expected_defaults(self) -> None:
        args = build_parser().parse_args([])

        assert args.size == "4MiB"
        assert args.leechers == 3
        assert args.slots == 4
        assert args.rate == 0
        assert args.hostile is False

    def test_a_rate_can_be_written_the_way_people_write_it(self) -> None:
        args = build_parser().parse_args(["--rate", "100k"])

        assert args.rate == 100 * 1024

    def test_a_failure_is_reported_not_raised(self, tmp_path: Path, monkeypatch) -> None:
        async def broken(*args: object, **kwargs: object) -> UploadCheck:
            raise RuntimeError("cannot bind port")

        monkeypatch.setattr("tools.upload_check.run_upload_check", broken)

        exit_code = main(["--size", "64KiB", "--out", str(tmp_path / "dl")])

        assert exit_code == 1
