"""Tests for the metrics check tool.

The tool is the statistics engine's end-to-end proof, so these tests drive it
the way the CLI does: a real payload, real seeders on real sockets, the real
download engine, and a collector sampling while the bytes move. What comes back
has to add up — bytes counted against bytes written, samples against the
history's bound, and an ETA that could only have come from a rate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.torrent import Torrent, parse_torrent
from tools.make_test_torrent import build_torrent_bytes, generate_payload
from tools.metrics_check import (
    MetricsCheck,
    build_parser,
    main,
    render,
    run_metrics_check,
)

SIZE = 512 * 1024
PIECE_LENGTH = 64 * 1024


def make_torrent(payload: bytes) -> Torrent:
    return parse_torrent(
        build_torrent_bytes(payload, name="metrics", piece_length=PIECE_LENGTH, file_count=1)
    )


@pytest.fixture
def metrics_payload() -> bytes:
    return generate_payload(SIZE, seed=11)


class TestRunMetricsCheck:
    async def test_the_download_is_measured_and_verified(
        self, metrics_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(metrics_payload)

        check = await run_metrics_check(
            torrent,
            metrics_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            seeds=2,
            leechers=1,
            interval=0.05,
        )

        assert check.successful is True
        assert check.verified is True
        assert check.complete is True
        assert check.pieces_verified == torrent.piece_count
        assert check.downloaded_bytes >= torrent.total_length
        assert check.download_average > 0.0

    async def test_samples_are_taken_while_the_bytes_move(
        self, metrics_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(metrics_payload)

        check = await run_metrics_check(
            torrent,
            metrics_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            interval=0.02,
        )

        assert check.samples_taken > 1
        assert "progress" in check.series
        assert check.series["progress"][-1][1] == 1.0

    async def test_the_history_never_passes_its_bound(
        self, metrics_payload: bytes, tmp_path: Path
    ) -> None:
        """A long run costs the same memory as a short one."""
        torrent = make_torrent(metrics_payload)

        check = await run_metrics_check(
            torrent,
            metrics_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            interval=0.01,
        )

        assert check.history_within_bounds is True
        assert all(len(samples) <= check.history_capacity for samples in check.series.values())

    async def test_the_eta_comes_from_a_rate_and_comes_down(
        self, metrics_payload: bytes, tmp_path: Path
    ) -> None:
        """An ETA is measured, and it falls as the torrent fills.

        The seeders are deliberately slow: on loopback a small torrent can
        finish inside one sample interval, and an ETA sampled after completion
        is zero for the boring reason. Slowing the swarm down is what makes
        the number worth asserting on.
        """
        torrent = make_torrent(metrics_payload)

        check = await run_metrics_check(
            torrent,
            metrics_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            interval=0.02,
            slow_delay=0.03,
            upload=False,
        )

        assert check.first_eta is not None
        assert check.last_eta is not None
        assert check.first_eta > 0.0
        assert check.last_eta < check.first_eta

    async def test_seeding_back_is_counted_on_the_upload_side(
        self, metrics_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(metrics_payload)

        check = await run_metrics_check(
            torrent,
            metrics_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            leechers=2,
        )

        assert check.uploaded_bytes > 0
        assert check.leechers == 2
        assert check.upload_average > 0.0

    async def test_without_seeding_back_there_is_no_upload(
        self, metrics_payload: bytes, tmp_path: Path
    ) -> None:
        """An unmeasured direction reads zero, not a guess."""
        torrent = make_torrent(metrics_payload)

        check = await run_metrics_check(
            torrent,
            metrics_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
            upload=False,
        )

        assert check.uploaded_bytes == 0
        assert check.upload_average == 0.0
        assert check.leechers == 0
        assert check.downloaded_bytes > 0

    async def test_the_final_snapshot_is_the_finished_torrent(
        self, metrics_payload: bytes, tmp_path: Path
    ) -> None:
        torrent = make_torrent(metrics_payload)

        check = await run_metrics_check(
            torrent,
            metrics_payload,
            directory=tmp_path / "dl",
            state_directory=tmp_path / "state",
        )

        assert check.final.complete is True
        assert check.final.progress == 1.0
        assert check.final.eta_seconds == 0.0


class TestRender:
    def _check(self) -> MetricsCheck:
        from app.statistics.metrics import MetricsSnapshot

        return MetricsCheck(
            name="metrics",
            hex_info_hash="abc123",
            total_length=8 * 1024 * 1024,
            piece_count=32,
            seeds=3,
            leechers=2,
            downloaded_bytes=8 * 1024 * 1024,
            uploaded_bytes=512 * 1024,
            elapsed=1.25,
            samples_taken=42,
            history_capacity=300,
            download_average=6.4 * 1024 * 1024,
            download_peak=9.1 * 1024 * 1024,
            upload_average=2.0 * 1024 * 1024,
            first_eta=4.2,
            last_eta=0.1,
            pieces_verified=32,
            wasted_bytes=16 * 1024,
            verified=True,
            complete=True,
            final=MetricsSnapshot(pieces_total=32),
            series={"progress": ((0.0, 0.0), (1.0, 1.0))},
        )

    def test_a_good_run_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        render(self._check(), directory=Path("/tmp/metrics"))

        out = capsys.readouterr().out

        assert "Metrics check: metrics" in out
        assert "32 piece(s)" in out
        assert "3 seeder(s)" in out
        assert "42 taken" in out
        assert "4.2s→0.1s" in out
        assert "RESULT ok" in out

    def test_a_run_with_no_eta_says_never(self, capsys: pytest.CaptureFixture[str]) -> None:
        from dataclasses import replace

        check = replace(self._check(), first_eta=None, last_eta=None)

        render(check, directory=Path("/tmp/metrics"))

        assert "never→never" in capsys.readouterr().out

    def test_a_broken_run_is_reported_as_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        from dataclasses import replace

        check = replace(self._check(), verified=False, downloaded_bytes=0)

        render(check, directory=Path("/tmp/metrics"))

        assert "RESULT failed" in capsys.readouterr().out


class TestCommandLine:
    def test_the_tool_runs_and_reports(self, tmp_path: Path, capsys) -> None:
        directory = tmp_path / "dl"

        code = main(
            [
                "--size",
                "256KiB",
                "--seeds",
                "2",
                "--leechers",
                "1",
                "--interval",
                "0.05",
                "--out",
                str(directory),
            ]
        )
        out = capsys.readouterr().out

        assert code == 0
        assert "RESULT ok" in out
        assert not directory.exists()  # cleaned up unless --keep

    def test_keeping_the_directory_is_an_option(self, tmp_path: Path) -> None:
        directory = tmp_path / "dl"

        code = main(
            [
                "--size",
                "256KiB",
                "--no-upload",
                "--interval",
                "0.05",
                "--out",
                str(directory),
                "--keep",
            ]
        )

        assert code == 0
        assert directory.exists()

    def test_the_parser_has_the_expected_defaults(self) -> None:
        args = build_parser().parse_args([])

        assert args.size == "8MiB"
        assert args.seeds == 3
        assert args.leechers == 2
        assert args.no_upload is False  # the upload half runs by default

    def test_a_failure_is_reported_not_raised(self, tmp_path: Path, monkeypatch) -> None:
        """A diagnostic tool reports; it does not throw at the user."""

        async def boom(*args: object, **kwargs: object) -> MetricsCheck:
            raise RuntimeError("the swarm went away")

        monkeypatch.setattr("tools.metrics_check.run_metrics_check", boom)
        code = main(["--size", "64KiB", "--out", str(tmp_path / "dl")])

        assert code == 1
