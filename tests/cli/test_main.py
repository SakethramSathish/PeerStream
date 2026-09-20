"""Unit tests for the command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app import __version__
from app.torrent import parse_torrent_file
from cli.main import human_size, main, torrent_summary
from tools.make_test_torrent import write_test_torrent
from tools.mock_tracker import MockTracker


@pytest.fixture
def torrent_path(tmp_path: Path) -> Path:
    path, _ = write_test_torrent(tmp_path, size=64 * 1024, name="sample.bin", file_count=1)
    return path


@pytest.fixture
def multi_file_torrent_path(tmp_path: Path) -> Path:
    path, _ = write_test_torrent(tmp_path, size=96 * 1024, name="bundle", file_count=4)
    return path


class TestInfoCommand:
    def test_prints_metadata(self, torrent_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["info", str(torrent_path)]) == 0
        output = capsys.readouterr().out

        assert "sample.bin" in output
        assert "info hash" in output
        assert "pieces" in output
        assert "trackers" in output

    def test_info_hash_matches_the_parsed_torrent(
        self, torrent_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["info", str(torrent_path)])
        assert parse_torrent_file(torrent_path).hex_info_hash in capsys.readouterr().out

    def test_json_output_is_parseable(
        self, torrent_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["info", str(torrent_path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["name"] == "sample.bin"
        assert payload["total_length"] == 64 * 1024
        assert payload["piece_count"] == 4
        assert payload["single_file"] is True
        assert payload["announce"].startswith("http://")

    def test_lists_multiple_files(
        self, multi_file_torrent_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["info", str(multi_file_torrent_path)]) == 0
        output = capsys.readouterr().out
        assert "4 file(s)" in output
        assert "part000.bin" in output
        assert "data/part002.bin" in output

    def test_max_files_truncates_the_listing(
        self, multi_file_torrent_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["info", str(multi_file_torrent_path), "--max-files", "1"])
        output = capsys.readouterr().out

        assert "part000.bin" in output
        assert "part001.bin" not in output
        assert "3 more" in output

    def test_missing_file_reports_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["info", str(tmp_path / "absent.torrent")]) == 1
        assert "cannot read" in capsys.readouterr().err

    def test_invalid_torrent_reports_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "broken.torrent"
        path.write_bytes(b"not a torrent")
        assert main(["info", str(path)]) == 1
        assert "cannot read" in capsys.readouterr().err


class TestSummary:
    def test_summary_matches_the_torrent(self, torrent_path: Path) -> None:
        torrent = parse_torrent_file(torrent_path)
        summary = torrent_summary(torrent)

        assert summary["info_hash"] == torrent.hex_info_hash
        assert summary["piece_count"] == torrent.piece_count
        assert summary["last_piece_length"] == torrent.last_piece_length
        assert summary["file_count"] == len(torrent.files)
        assert summary["files_truncated"] is False
        assert len(summary["files"]) == len(torrent.files)

    def test_summary_marks_truncation(self, multi_file_torrent_path: Path) -> None:
        torrent = parse_torrent_file(multi_file_torrent_path)
        summary = torrent_summary(torrent, max_files=1)
        assert summary["files_truncated"] is True
        assert len(summary["files"]) == 1


class TestHumanSize:
    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1023, "1023 B"),
            (1024, "1.00 KiB"),
            (1536, "1.50 KiB"),
            (1024**2, "1.00 MiB"),
            (5_700_000_000, "5.31 GiB"),
            (1024**4, "1.00 TiB"),
        ],
    )
    def test_formats_binary_units(self, size: int, expected: str) -> None:
        assert human_size(size) == expected


class TestEntryPoint:
    def test_version_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0
        assert __version__ in capsys.readouterr().out

    def test_missing_subcommand_exits_with_usage(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert excinfo.value.code == 2


class TestAnnounceCommand:
    def test_announces_to_a_tracker_and_lists_peers(
        self,
        torrent_path: Path,
        mock_tracker: MockTracker,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        torrent = parse_torrent_file(torrent_path)
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.5", 6882, left=0)

        assert main(["announce", str(torrent_path), "--tracker", mock_tracker.announce_url]) == 0
        output = capsys.readouterr().out

        assert "sample.bin" in output
        assert "10.0.0.5:6882" in output
        assert "1 seeders" in output
        assert mock_tracker.announce_url in output

    def test_json_output_is_parseable(
        self,
        torrent_path: Path,
        mock_tracker: MockTracker,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        torrent = parse_torrent_file(torrent_path)
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.5", 6882)

        assert (
            main(
                [
                    "announce",
                    str(torrent_path),
                    "--tracker",
                    mock_tracker.announce_url,
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["info_hash"] == torrent.hex_info_hash
        assert payload["peers"] == ["10.0.0.5:6882"]
        assert payload["tracker"] == mock_tracker.announce_url

    def test_reports_failure_when_unreachable(
        self,
        torrent_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert (
            main(
                [
                    "announce",
                    str(torrent_path),
                    "--tracker",
                    "http://127.0.0.1:1/announce",
                    "--timeout",
                    "1",
                ]
            )
            == 1
        )
        assert "announce failed" in capsys.readouterr().err

    def test_accepts_a_udp_tracker_url(
        self,
        torrent_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # UDP stopped being an unsupported scheme in M14: the CLI now builds a
        # UDP client and fails on the network, not on the scheme. Nothing is
        # listening on port 1, so this is a timeout, not a refusal.
        result = main(
            [
                "announce",
                str(torrent_path),
                "--tracker",
                "udp://127.0.0.1:1",
                "--timeout",
                "0.2",
            ]
        )
        assert result == 1
        error = capsys.readouterr().err
        assert "cannot speak" not in error
        assert "did not answer" in error

    def test_rejects_unsupported_schemes(
        self,
        torrent_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert (
            main(["announce", str(torrent_path), "--tracker", "wss://tracker.example/announce"])
            == 1
        )
        assert "cannot speak wss" in capsys.readouterr().err

    def test_reports_unreadable_torrents(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "nope.torrent"
        assert main(["announce", str(missing)]) == 1
        assert "cannot read" in capsys.readouterr().err

    def test_uses_the_torrent_own_trackers(
        self, tmp_path: Path, mock_tracker: MockTracker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path, _ = write_test_torrent(
            tmp_path, size=32 * 1024, name="with-tracker.bin", announce=mock_tracker.announce_url
        )
        torrent = parse_torrent_file(path)
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.9", 6999)

        assert main(["announce", str(path)]) == 0
        assert "10.0.0.9:6999" in capsys.readouterr().out
