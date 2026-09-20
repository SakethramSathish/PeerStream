"""Tests for the swarm download tool.

The tool is the download engine's end-to-end proof, so these tests drive it
the way the CLI does: real payload, real seeders on real sockets, real peer
manager, real engine — the only thing standing in for the internet is the
swarm itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.config import DownloadConfig, PieceStrategy
from app.torrent import Torrent, parse_torrent
from tools.make_test_torrent import build_torrent_bytes, generate_payload
from tools.swarm_download import (
    SwarmReport,
    _human_bytes,
    _read_piece,
    build_parser,
    main,
    render,
    run_swarm,
)

SIZE = 256 * 1024
PIECE_LENGTH = 32 * 1024


@pytest.fixture
def swarm_payload() -> bytes:
    return generate_payload(SIZE, seed=7)


@pytest.fixture
def swarm_torrent(swarm_payload: bytes) -> Torrent:
    return parse_torrent(
        build_torrent_bytes(swarm_payload, name="swarm", piece_length=PIECE_LENGTH, file_count=1)
    )


class TestRunSwarm:
    async def test_the_engine_pulls_every_piece_from_the_swarm(
        self, swarm_torrent: Torrent, swarm_payload: bytes, tmp_path: Path
    ) -> None:
        report = await run_swarm(
            swarm_torrent,
            swarm_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            seeds=2,
        )

        assert report.pieces_verified == swarm_torrent.piece_count
        assert report.pieces_failed == 0
        assert report.verified is True
        assert report.successful is True
        assert report.blocks_received == (SIZE + 16 * 1024 - 1) // (16 * 1024)

    async def test_the_bytes_on_disk_are_the_payload(
        self, swarm_torrent: Torrent, swarm_payload: bytes, tmp_path: Path
    ) -> None:
        directory = tmp_path / "dl"
        await run_swarm(
            swarm_torrent,
            swarm_payload,
            directory=directory,
            state_directory=tmp_path / "state",
            seeds=2,
        )

        on_disk = (
            (directory / "swarm").read_bytes()
            if (directory / "swarm").exists()
            else b"".join((directory / entry.path).read_bytes() for entry in swarm_torrent.files)
        )
        assert on_disk == swarm_payload

    async def test_killing_a_seeder_costs_nothing_but_time(
        self, swarm_torrent: Torrent, swarm_payload: bytes, tmp_path: Path
    ) -> None:
        report = await run_swarm(
            swarm_torrent,
            swarm_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            seeds=2,
            kill_after=1,
        )

        assert report.seeds_killed == 1
        assert report.successful is True

    async def test_endgame_outruns_a_slow_seeder(
        self, swarm_torrent: Torrent, swarm_payload: bytes, tmp_path: Path
    ) -> None:
        """A seeder that answers late is raced, and the download still finishes."""
        report = await run_swarm(
            swarm_torrent,
            swarm_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            seeds=2,
            config=DownloadConfig(endgame_delay=0.0),
            slow_seeds=1,
            slow_delay=0.05,
        )

        assert report.slow_seeds == 1
        # Endgame asked for more blocks than the torrent contains: the slow
        # peer's share was requested from somebody faster as well.
        assert report.requests_sent > report.blocks_received
        assert report.successful is True

    async def test_a_single_seeder_suffices(
        self, swarm_torrent: Torrent, swarm_payload: bytes, tmp_path: Path
    ) -> None:
        report = await run_swarm(
            swarm_torrent,
            swarm_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            seeds=1,
            config=DownloadConfig(max_outstanding_requests=4, endgame_enabled=False),
        )

        assert report.seeds == 1
        assert report.successful is True

    async def test_sequential_selection_also_completes(
        self, swarm_torrent: Torrent, swarm_payload: bytes, tmp_path: Path
    ) -> None:
        report = await run_swarm(
            swarm_torrent,
            swarm_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            seeds=1,
            config=DownloadConfig(piece_strategy=PieceStrategy.SEQUENTIAL, endgame_enabled=False),
        )

        assert report.strategy == "sequential"
        assert report.successful is True

    async def test_a_multi_file_torrent_reassembles(
        self, swarm_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = parse_torrent(
            build_torrent_bytes(
                swarm_payload, name="bundle", piece_length=PIECE_LENGTH, file_count=4
            )
        )

        report = await run_swarm(
            torrent,
            swarm_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            seeds=2,
            config=DownloadConfig(endgame_enabled=False),
        )

        assert report.successful is True
        for index in range(torrent.piece_count):
            assert _read_piece(torrent, tmp_path / "dl", index) is not None


class TestReadPiece:
    def test_a_missing_file_yields_nothing(self, swarm_torrent: Torrent, tmp_path: Path) -> None:
        assert _read_piece(swarm_torrent, tmp_path, 0) is None


class TestHumanBytes:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (1024 * 1024, "1.0 MiB"),
            (4 * 1024**3, "4.0 GiB"),
        ],
    )
    def test_sizes_read_like_a_human_writes_them(self, value: int, expected: str) -> None:
        assert _human_bytes(value) == expected


class TestRender:
    def test_a_good_run_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = SwarmReport(
            name="swarm",
            hex_info_hash="a" * 40,
            total_length=8 * 1024 * 1024,
            piece_count=32,
            seeds=3,
            seeds_killed=0,
            slow_seeds=0,
            pieces_verified=32,
            pieces_failed=0,
            blocks_received=512,
            duplicate_blocks=1,
            wasted_bytes=16384,
            requests_sent=513,
            elapsed=2.0,
            verified=True,
            strategy="rarest_first",
        )

        render(report, directory=Path("/tmp/swarm"))

        out = capsys.readouterr().out
        assert "8.0 MiB in 32 pieces" in out
        assert "32/32 verified" in out
        assert "4.0 MiB/s" in out
        assert "RESULT ok" in out
        assert "3 seeder(s)" in out

    def test_a_bad_run_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = SwarmReport(
            name="swarm",
            hex_info_hash="a" * 40,
            total_length=1024,
            piece_count=1,
            seeds=1,
            seeds_killed=0,
            slow_seeds=0,
            pieces_verified=0,
            pieces_failed=0,
            blocks_received=0,
            duplicate_blocks=0,
            wasted_bytes=0,
            requests_sent=0,
            elapsed=1.0,
            verified=False,
            strategy="rarest_first",
        )

        render(report, directory=Path("/tmp/swarm"))

        assert "RESULT failed" in capsys.readouterr().out

    def test_killed_seeders_are_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = SwarmReport(
            name="swarm",
            hex_info_hash="a" * 40,
            total_length=1024,
            piece_count=1,
            seeds=3,
            seeds_killed=2,
            slow_seeds=0,
            pieces_verified=1,
            pieces_failed=0,
            blocks_received=1,
            duplicate_blocks=0,
            wasted_bytes=0,
            requests_sent=1,
            elapsed=1.0,
            verified=True,
            strategy="rarest_first",
        )

        render(report, directory=Path("/tmp/swarm"))

        assert "2 killed" in capsys.readouterr().out


class TestCommandLine:
    def test_the_tool_downloads_and_reports(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            [
                "--size",
                "256KiB",
                "--piece-length",
                str(PIECE_LENGTH),
                "--seeds",
                "2",
                "--no-endgame",
                "--files",
                "3",
                "--out",
                str(tmp_path / "dl"),
                "--state-dir",
                str(tmp_path / "state"),
                "--keep",
            ]
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Swarm download" in out
        assert "RESULT ok" in out
        assert (tmp_path / "dl").exists()

    def test_the_parser_has_the_expected_defaults(self) -> None:
        args = build_parser().parse_args([])

        assert args.size == "8MiB"
        assert args.seeds == 3
        assert args.files == 1
        assert args.strategy is PieceStrategy.RAREST_FIRST
        assert args.keep is False

    def test_a_failure_is_reported_not_raised(self, tmp_path: Path, monkeypatch) -> None:
        async def broken(*args: object, **kwargs: object) -> SwarmReport:
            raise RuntimeError("no route to host")

        monkeypatch.setattr("tools.swarm_download.run_swarm", broken)

        exit_code = main(["--size", "1KiB", "--out", str(tmp_path / "dl")])

        assert exit_code == 1
