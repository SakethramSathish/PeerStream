"""Tests for the ``download`` command.

Most of this is small: argument defaults, error paths, and the shape of the
summary. The last test is not small — it runs the real command against a real
loopback swarm (mock tracker, mock seeders, real sockets) and checks that the
bytes on disk are the bytes the torrent was built from, then runs it a second
time to prove the resume message comes from disk and not from optimism.

The command is exercised through :func:`cli.main._run_download` rather than
:func:`cli.main.command_download` only because the latter wraps it in
``asyncio.run``, which cannot be called from inside a running loop.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

import pytest
from app.services import Engine, build_engine
from app.torrent import Torrent, parse_torrent
from cli.main import (
    DEFAULT_DOWNLOAD_TIMEOUT,
    INTERRUPTED_EXIT_CODE,
    _download_summary,
    _extra_tiers,
    _progress_line,
    _run_download,
    _seed,
    build_parser,
    command_download,
    main,
    quiet,
)
from tools.make_test_torrent import build_torrent_bytes
from tools.mock_tracker import MockTracker

from tests.mocks.mock_peer import MockPeer

PIECE_LENGTH = 32 * 1024
SEEDER_COUNT = 2


TorrentPathFactory = Callable[[Path, bytes, str], Path]


@pytest.fixture
def torrent_path_factory() -> TorrentPathFactory:
    """Write ``payload`` out as a trackerless ``.torrent`` file."""

    def _write(directory: Path, payload: bytes, name: str) -> Path:
        path = directory / f"{name}.torrent"
        path.write_bytes(build_torrent_bytes(payload, name=name, announce=None))
        return path

    return _write


def download_args(torrent_path: Path, directory: Path, *extra: str) -> argparse.Namespace:
    """Arguments for ``download``, exactly as the shell would produce them."""
    return build_parser().parse_args(
        [
            "download",
            str(torrent_path),
            "--download-dir",
            str(directory),
            "--no-seed",
            *extra,
        ]
    )


async def build_and_get(sample_torrent: Torrent, tmp_path: Path) -> Engine:
    """A real engine that never transfers anything."""
    return await build_engine(sample_torrent, download_directory=tmp_path, listen=False)


@pytest.fixture
def torrent(payload: bytes) -> Torrent:
    """A torrent with no trackers, so a download attempt has nowhere to go."""
    return parse_torrent(
        build_torrent_bytes(payload, name="quiet.bin", piece_length=PIECE_LENGTH, announce=None)
    )


class TestDownloadParser:
    def test_the_command_exists_with_sane_defaults(self, tmp_path: Path) -> None:
        args = build_parser().parse_args(["download", "x.torrent"])

        assert args.command == "download"
        assert args.timeout == DEFAULT_DOWNLOAD_TIMEOUT
        assert args.seed_minutes == 0.0
        assert not args.no_listen
        assert not args.no_seed
        assert not args.no_resume
        assert not args.quiet
        assert args.download_dir is None

    def test_the_flags_are_understood(self, tmp_path: Path) -> None:
        args = build_parser().parse_args(
            [
                "download",
                "x.torrent",
                "--download-dir",
                str(tmp_path),
                "--tracker",
                "http://tracker.example/announce",
                "--no-listen",
                "--no-seed",
                "--no-resume",
                "--seed-minutes",
                "5",
                "--timeout",
                "12.5",
                "--quiet",
                "--json",
            ]
        )

        assert args.download_dir == tmp_path
        assert args.tracker == ["http://tracker.example/announce"]
        assert args.no_listen and args.no_seed and args.no_resume and args.quiet and args.as_json
        assert args.seed_minutes == 5.0
        assert args.timeout == 12.5


class TestDownloadErrors:
    def test_a_missing_torrent_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["download", str(tmp_path / "nope.torrent")]) == 1
        assert "cannot read" in capsys.readouterr().err

    def test_a_broken_torrent_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broken = tmp_path / "broken.torrent"
        broken.write_bytes(b"not a torrent")
        assert main(["download", str(broken)]) == 1
        assert "cannot read" in capsys.readouterr().err


class TestDownloadHelpers:
    def test_progress_and_json_both_mean_quiet(self, tmp_path: Path) -> None:
        assert quiet(download_args(tmp_path / "x.torrent", tmp_path, "--quiet"))
        assert quiet(download_args(tmp_path / "x.torrent", tmp_path, "--json"))
        assert not quiet(download_args(tmp_path / "x.torrent", tmp_path))

    def test_extra_trackers_become_tiers(self, tmp_path: Path) -> None:
        assert _extra_tiers(download_args(tmp_path / "x.torrent", tmp_path)) is None

        tiers = _extra_tiers(
            download_args(tmp_path / "x.torrent", tmp_path, "--tracker", "http://t.example/a")
        )
        assert tiers is not None
        assert len(tiers) == 1
        assert tiers[0][0].url == "http://t.example/a"

    async def test_the_progress_line_reports_measured_numbers(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        engine = await build_and_get(sample_torrent, tmp_path)
        try:
            line = _progress_line(engine, elapsed=1.0)
        finally:
            await engine.aclose()

        assert "0.00%" in line
        assert f"0/{sample_torrent.piece_count} pieces" in line
        assert "0 B/s" in line

    async def test_the_summary_describes_an_unstarted_run(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        engine = await build_and_get(sample_torrent, tmp_path)
        try:
            summary = _download_summary(engine, finished=False)
        finally:
            await engine.aclose()

        assert summary["finished"] is False
        assert summary["progress"] == 0.0
        assert summary["pieces_total"] == sample_torrent.piece_count
        assert summary["bytes_downloaded"] == 0
        assert summary["state"] == "idle"

    def test_interrupted_exit_code_is_the_conventional_one(self) -> None:
        assert INTERRUPTED_EXIT_CODE == 130


@pytest.mark.integration
class TestDownloadEndToEnd:
    async def test_the_command_downloads_verifies_and_saves(
        self, payload: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = tmp_path / "downloads"
        async with MockTracker(port=0) as tracker:
            metainfo = build_torrent_bytes(
                payload, name="cli.bin", piece_length=PIECE_LENGTH, announce=tracker.announce_url
            )
            torrent_path = tmp_path / "cli.torrent"
            torrent_path.write_bytes(metainfo)
            torrent = parse_torrent(metainfo)

            seeders: list[MockPeer] = []
            for _ in range(SEEDER_COUNT):
                seeder = MockPeer(payload, info_hash=torrent.info_hash, piece_length=PIECE_LENGTH)
                await seeder.start()
                tracker.add_peer(torrent.info_hash, seeder.host, seeder.port, left=0)
                seeders.append(seeder)

            try:
                code = await _run_download(
                    torrent, download_args(torrent_path, directory, "--timeout", "60")
                )
            finally:
                for seeder in seeders:
                    await seeder.stop()

        output = capsys.readouterr().out
        assert code == 0, output
        assert "16/16 verified" in output
        assert (directory / "cli.bin").read_bytes() == payload

    async def test_a_second_run_says_what_it_resumed(
        self, payload: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The resume line comes from the state file, not from a guess."""
        directory = tmp_path / "downloads"
        metainfo = build_torrent_bytes(
            payload, name="cli.bin", piece_length=PIECE_LENGTH, announce="http://127.0.0.1:1/a"
        )
        torrent_path = tmp_path / "cli.torrent"
        torrent_path.write_bytes(metainfo)
        torrent = parse_torrent(metainfo)

        first = await build_engine(torrent, download_directory=directory, listen=False)
        await first.storage.prepare()
        await first.storage.write_piece(0, payload[:PIECE_LENGTH])
        await first.storage.save_resume(uploaded=0)
        await first.aclose()

        capsys.readouterr()
        code = await _run_download(
            torrent, download_args(torrent_path, directory, "--timeout", "5")
        )
        output = capsys.readouterr().out

        # Nothing is serving, so the run gives up — but it must have said, up
        # front, that it took one piece from disk.
        assert code == 1
        assert "resumed     1 piece(s) from disk" in output

    async def test_json_output_is_parseable(
        self, payload: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        directory = tmp_path / "downloads"
        async with MockTracker(port=0) as tracker:
            metainfo = build_torrent_bytes(
                payload, name="cli.bin", piece_length=PIECE_LENGTH, announce=tracker.announce_url
            )
            torrent_path = tmp_path / "cli.torrent"
            torrent_path.write_bytes(metainfo)
            torrent = parse_torrent(metainfo)

            seeder = MockPeer(payload, info_hash=torrent.info_hash, piece_length=PIECE_LENGTH)
            await seeder.start()
            tracker.add_peer(torrent.info_hash, seeder.host, seeder.port, left=0)

            try:
                code = await _run_download(
                    torrent, download_args(torrent_path, directory, "--json", "--timeout", "60")
                )
            finally:
                await seeder.stop()

        assert code == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["finished"] is True
        assert summary["pieces_verified"] == 16
        assert summary["bytes_downloaded"] == len(payload)


class TestSeeding:
    async def test_seeding_reports_what_it_serves(
        self, sample_torrent: Torrent, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        engine = await build_engine(sample_torrent, download_directory=tmp_path, listen=True)
        try:
            await engine.start()
            # Half a second is long enough for one progress tick, and the
            # deadline is what makes it stop.
            await _seed(engine, minutes=0.008)
        finally:
            await engine.aclose()

        output = capsys.readouterr().out
        assert "seeding on port" in output
        assert "served" in output

    async def test_an_interrupted_run_says_progress_is_saved(
        self,
        torrent: Torrent,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ctrl-C must not lose what is already on disk."""

        async def interrupt(engine: Engine, *, timeout: float, quiet: bool) -> bool:
            raise KeyboardInterrupt

        monkeypatch.setattr("cli.main._wait_with_progress", interrupt)
        arguments = download_args(tmp_path / "x.torrent", tmp_path, "--timeout", "1")

        code = await _run_download(torrent, arguments)

        assert code == INTERRUPTED_EXIT_CODE
        assert "progress saved" in capsys.readouterr().out

    def test_the_command_runs_its_own_event_loop(
        self,
        torrent_path_factory: TorrentPathFactory,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``command_download`` is the synchronous shell entry point.

        It wraps the coroutine in ``asyncio.run``, so it can only be called
        from outside a loop — which is exactly how the shell calls it.
        """
        torrent_path = torrent_path_factory(tmp_path, b"x" * 32 * 1024, "loop.bin")
        arguments = download_args(torrent_path, tmp_path / "out", "--timeout", "0.05")

        assert command_download(arguments) == 1
        assert "gave up" in capsys.readouterr().out

    async def test_a_run_that_finishes_without_seeding_stops_cleanly(
        self, payload: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The whole command, end to end, with nothing left running after."""
        directory = tmp_path / "downloads"
        metainfo = build_torrent_bytes(
            payload, name="cli.bin", piece_length=PIECE_LENGTH, announce=None
        )
        torrent_path = tmp_path / "cli.torrent"
        torrent_path.write_bytes(metainfo)
        torrent = parse_torrent(metainfo)

        first = await build_engine(torrent, download_directory=directory, listen=False)
        await first.storage.prepare()
        for index in range(torrent.piece_count):
            start = index * PIECE_LENGTH
            await first.storage.write_piece(index, payload[start : start + PIECE_LENGTH])
        await first.storage.save_resume(uploaded=0)
        await first.aclose()

        capsys.readouterr()
        code = await _run_download(
            torrent, download_args(torrent_path, directory, "--timeout", "5")
        )
        output = capsys.readouterr().out

        assert code == 0
        assert f"resumed     {torrent.piece_count} piece(s) from disk" in output
        assert (directory / "cli.bin").read_bytes() == payload

    async def test_a_finished_run_seeds_for_as_long_as_it_was_told(
        self, payload: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Complete, then seed for a moment: the default is to give back."""
        directory = tmp_path / "downloads"
        metainfo = build_torrent_bytes(
            payload, name="cli.bin", piece_length=PIECE_LENGTH, announce=None
        )
        torrent_path = tmp_path / "cli.torrent"
        torrent_path.write_bytes(metainfo)
        torrent = parse_torrent(metainfo)

        first = await build_engine(torrent, download_directory=directory, listen=False)
        await first.storage.prepare()
        for index in range(torrent.piece_count):
            start = index * PIECE_LENGTH
            await first.storage.write_piece(index, payload[start : start + PIECE_LENGTH])
        await first.storage.save_resume(uploaded=0)
        await first.aclose()

        arguments = build_parser().parse_args(
            [
                "download",
                str(torrent_path),
                "--download-dir",
                str(directory),
                "--seed-minutes",
                "0.008",
                "--timeout",
                "5",
            ]
        )
        capsys.readouterr()
        code = await _run_download(torrent, arguments)

        assert code == 0
        assert "seeding on port" in capsys.readouterr().out
