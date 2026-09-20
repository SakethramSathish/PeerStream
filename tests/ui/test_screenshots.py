"""The screenshot tool: it renders, and what it renders is real.

Two tests, in increasing order of ambition:

1. **The empty interface renders.** No swarm, no sockets: the window is built
   offscreen and every page is written to a PNG. This is what catches a widget
   tree that cannot be constructed without a display.
2. **A real swarm renders.** A local tracker, seeders built from the real
   payload, the client's own sockets, and a PNG written at the end. This is the
   one that matters: it proves the numbers on screen came from a transfer and
   not from a fixture, so a screenshot can never be a picture of a lie.

Both are loopback-only. Nothing here touches the internet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tools.screenshot import main, make_test_torrent

PAGES: tuple[str, ...] = ("overview", "library", "dht", "settings")

# Generous: the child builds a swarm, transfers bytes and renders five screens,
# and there is no value in failing it because the machine was busy.
TOOL_TIMEOUT_SECONDS: float = 150.0


def run_tool(arguments: list[str]) -> tuple[int, str]:
    """Run ``python -m tools.screenshot`` in a clean process.

    A separate process, because the capture drives the real engine on a real
    loop and must not inherit whatever state two thousand earlier tests left
    behind.
    """
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(Path.cwd()))
    completed = subprocess.run(
        [sys.executable, "-m", "tools.screenshot", *arguments],
        capture_output=True,
        text=True,
        timeout=TOOL_TIMEOUT_SECONDS,
        env=environment,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _is_a_real_png(path: Path) -> bool:
    """The PNG signature, plus a non-trivial size: an empty window compresses
    to almost nothing, a window with a swarm in it does not."""
    if not path.exists():
        return False
    header = path.read_bytes()[:8]
    return header == b"\x89PNG\r\n\x1a\n" and path.stat().st_size > 8 * 1024


class TestTheEmptyInterfaceRenders:
    def test_every_page_is_written(
        self, qapp: object, screenshot_dir: Path, tmp_path: Path
    ) -> None:
        code = main(
            [
                "--out",
                str(screenshot_dir),
                "--no-swarm",
                "--pages",
                ",".join(PAGES),
                "--seconds",
                "0",
            ]
        )
        assert code == 0
        for page in PAGES:
            assert _is_a_real_png(screenshot_dir / f"{page}.png"), f"{page} was not written"

    def test_the_default_run_produces_the_whole_set(
        self, qapp: object, screenshot_dir: Path
    ) -> None:
        code = main(["--out", str(screenshot_dir), "--no-swarm", "--seconds", "0"])
        assert code == 0
        written = sorted(path.name for path in screenshot_dir.glob("*.png"))
        assert "overview.png" in written
        assert "settings.png" in written

    def test_a_torrent_without_a_payload_is_refused(self, qapp: object, tmp_path: Path) -> None:
        torrent, _payload = make_test_torrent(tmp_path, size="64KiB")
        code = main(["--out", str(tmp_path / "out"), "--torrent", str(torrent)])
        assert code == 2

    def test_the_detail_tabs_are_captured_one_picture_each(
        self, qapp: object, screenshot_dir: Path
    ) -> None:
        # The detail page is six screens; "detail.png" would be a picture of
        # whichever tab happened to be current.
        tabs = ("peers", "pieces", "files", "trackers", "log", "overview")
        code = main(
            [
                "--out",
                str(screenshot_dir),
                "--no-swarm",
                "--seconds",
                "0",
                "--pages",
                ",".join(f"detail:{tab}" for tab in tabs),
            ]
        )
        assert code == 0
        for tab in tabs:
            target = screenshot_dir / f"detail-{tab}.png"
            assert _is_a_real_png(target), f"{target.name} was not written"


class TestTheGenerator:
    def test_it_writes_a_torrent_and_its_payload(self, tmp_path: Path) -> None:
        torrent, payload = make_test_torrent(tmp_path, size="128KiB")
        assert torrent.exists()
        assert payload.stat().st_size == 128 * 1024

    def test_the_torrent_describes_the_payload(self, tmp_path: Path) -> None:
        from app.torrent import parse_torrent_file

        torrent, payload = make_test_torrent(tmp_path, size="128KiB")
        parsed = parse_torrent_file(torrent)
        assert parsed.total_length == payload.stat().st_size


@pytest.mark.slow
@pytest.mark.integration
class TestARealSwarmRenders:
    """One end-to-end run: tracker, seeders, client, PNGs — in a subprocess.

    The capture runs as a separate process on purpose. It is the only test in
    this package that moves real bytes over real sockets, and a leaked thread
    or lock from any of the two thousand tests before it can stall a
    ``to_thread`` call forever; a child process starts clean. The tool also
    writes a JSON report, so the test can assert on what was *on screen*
    rather than on the existence of a file.

    Marked slow because it transfers data, and integration because it uses
    loopback.
    """

    def test_the_pictures_contain_a_real_transfer(
        self, screenshot_dir: Path, tmp_path: Path
    ) -> None:
        from tools.screenshot import make_test_torrent

        torrent, payload = make_test_torrent(tmp_path, size="1MiB")
        report_path = tmp_path / "report.json"
        code, output = run_tool(
            [
                "--out",
                str(screenshot_dir),
                "--torrent",
                str(torrent),
                "--payload",
                str(payload),
                "--seeders",
                "2",
                "--seconds",
                "5",
                "--pages",
                "overview,library,detail:peers,detail:pieces,settings",
                "--report",
                str(report_path),
            ]
        )
        assert code == 0, output

        report = json.loads(report_path.read_text())
        assert report["torrents"] == 1
        assert report["peers_connected"] > 0, "the swarm never connected"
        assert report["downloaded_bytes"] > 0, "no bytes moved: nothing was drawn"
        assert report["progress"] > 0.0

        for path in report["images"]:
            assert _is_a_real_png(Path(path)), f"{path} is not a real image"

        # The detail page drew the same transfer, tab by tab.
        detail = report["detail"]
        assert detail["pieces"]["piece_count"] > 0, "the matrix had no pieces to draw"
        assert detail["pieces"]["counts"]["verified"] > 0, "no piece had verified yet"
        assert detail["peers"]["peers"] > 0, "the swarm canvas had nothing on it"
        assert "detail-peers.png" in " ".join(report["images"])
        assert "detail-pieces.png" in " ".join(report["images"])

    def test_the_default_run_stands_up_its_own_swarm(self, screenshot_dir: Path) -> None:
        """No arguments: generate a torrent, seed it, download it, shoot it."""
        code, output = run_tool(
            ["--out", str(screenshot_dir), "--seconds", "5", "--pages", "overview"]
        )
        assert code == 0, output
        assert _is_a_real_png(screenshot_dir / "overview.png")

    def test_the_dht_screen_shows_nodes_that_actually_answered(
        self, screenshot_dir: Path, tmp_path: Path
    ) -> None:
        """The DHT page is only worth a picture with a routing table in it.

        So the tool stands up a cluster of its own and the client bootstraps
        against it: every contact in the table answered a real KRPC question a
        moment before the shutter opened. The report says so in numbers, which
        is the point of a report — a PNG alone would not distinguish four nodes
        from four invented rows.
        """
        from tools.screenshot import make_test_torrent

        torrent, payload = make_test_torrent(tmp_path, size="256KiB")
        report_path = tmp_path / "report.json"
        magnet = "magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=Debian&tr=http://t.example/announce"

        code, output = run_tool(
            [
                "--out",
                str(screenshot_dir),
                "--torrent",
                str(torrent),
                "--payload",
                str(payload),
                "--seeders",
                "1",
                "--seconds",
                "3",
                "--pages",
                "dht",
                "--dht-nodes",
                "3",
                "--magnet",
                magnet,
                "--report",
                str(report_path),
            ]
        )
        assert code == 0, output

        report = json.loads(report_path.read_text())
        dht = report["dht"]
        assert dht["enabled"] is True, "the tool should have enabled the DHT"
        assert dht["running"] is True, "the node never bound its socket"
        assert dht["contacts"] >= 3, f"bootstrap found only {dht['contacts']} node(s)"
        assert dht["contacts_shown"] == dht["contacts"]
        assert _is_a_real_png(screenshot_dir / "dht.png")

        # The add dialog, photographed with a real magnet in it: the screen
        # where the interface admits it does not know the size yet.
        dialog = screenshot_dir / "add-magnet.png"
        assert dialog.exists() and dialog.stat().st_size > 8 * 1024
