"""Render the interface offscreen, against a real swarm.

Screenshots are how the interface gets reviewed without a display attached, and
a screenshot of an empty window proves very little. So this tool builds the real
window and — unless told otherwise — stands up a real local swarm first:

    MockTracker ──announce──▶ client ──peer wire──▶ MockPeer seeder(s)

That is the same path a download takes in production, over real HTTP and real
sockets on the loopback interface. The numbers in the resulting PNGs are
therefore measured, not staged: if a panel shows 0 B/s, it was doing 0 B/s.

Usage::

    QT_QPA_PLATFORM=offscreen python -m tools.screenshot --out docs/screenshots

    # your own torrent, with three seeders and a longer run
    QT_QPA_PLATFORM=offscreen python -m tools.screenshot \\
        --torrent data/torrents/test.torrent --payload /tmp/test-payload.bin \\
        --seeders 3 --seconds 8

Nothing needs to be running beforehand: the tool starts its own tracker on a
free port and shuts it down when it is finished. With no ``--torrent`` it
generates a deterministic 4 MiB test torrent in a temporary directory, so the
bare command produces screenshots with real traffic in them.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

if sys.platform == "win32" and "QT_QPA_FONTDIR" not in os.environ:
    _win_fonts = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
    if _win_fonts.is_dir():
        os.environ["QT_QPA_FONTDIR"] = str(_win_fonts)

from app.core.constants import PEER_ID_SIZE
from app.tracker.base import AnnounceRequest, TrackerEvent
from app.tracker.factory import build_tracker
from PySide6.QtCore import QCoreApplication

from tools.mock_tracker import MockTracker

PROGRAM_NAME: Final[str] = "tools.screenshot"
DEFAULT_OUT: Final[Path] = Path("docs/screenshots")
DEFAULT_SECONDS: Final[float] = 6.0
DEFAULT_SEEDERS: Final[int] = 2
#: Nodes in the local DHT cluster the client bootstraps against. Four is a
#: routing table with a shape to it; zero disables DHT and photographs the
#: screen that says so.
DEFAULT_DHT_NODES: Final[int] = 4
DEFAULT_SIZE: Final[str] = "4MiB"

# The detail page reads the swarm and the pieces from the engine loop, and the
# first read lands shortly after the page is shown. This is how long the tool
# waits before photographing a tab.
DETAIL_SETTLE_SECONDS: Final[float] = 1.6
#: How long the DHT screen gets to bootstrap against the local cluster.
DHT_SETTLE_SECONDS: Final[float] = 2.0
#: A page is ``"key"``; a detail tab is ``"detail:tab"``. The tab form exists
#: because the detail page is six screens, and a single PNG of whichever tab
#: happened to be current would not be a review of anything.
DEFAULT_PAGES: Final[tuple[str, ...]] = (
    "overview",
    "library",
    "detail:overview",
    "detail:peers",
    "detail:pieces",
    "detail:files",
    "detail:trackers",
    "detail:log",
    "logs",
    "dht",
    "settings",
)

logger = logging.getLogger(PROGRAM_NAME)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Render the Qt interface offscreen, with real data where possible.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="directory to write PNGs to")
    parser.add_argument(
        "--torrent", type=Path, default=None, help="a .torrent to add (needs --payload too)"
    )
    parser.add_argument(
        "--payload",
        type=Path,
        default=None,
        help="the file holding the torrent's bytes, for the local seeder to serve",
    )
    parser.add_argument(
        "--seeders", type=int, default=DEFAULT_SEEDERS, help="how many local seeders to run"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SECONDS,
        help="how long to let the swarm run before the first screenshot",
    )
    parser.add_argument("--pages", default=",".join(DEFAULT_PAGES), help="pages to capture")
    parser.add_argument("--theme", default="dark", choices=("dark", "light", "amoled"))
    parser.add_argument(
        "--dht-nodes",
        type=int,
        default=DEFAULT_DHT_NODES,
        help="how many local DHT nodes to run for the DHT screen to join (0 disables DHT)",
    )
    parser.add_argument(
        "--magnet",
        default=None,
        help="also photograph the add dialog with this magnet link filled in",
    )
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write a JSON summary of what was on screen, for machines to read",
    )
    parser.add_argument(
        "--no-swarm",
        action="store_true",
        help="skip the local tracker and seeders; capture the empty interface",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


# --------------------------------------------------------------------- the swarm


async def _serve_swarm(
    tracker: MockTracker,
    torrent_path: Path,
    payload_path: Path,
    *,
    seeders: int,
    ready: threading.Event,
    stop: asyncio.Event,
) -> None:
    """Run seeders against a running tracker until ``stop`` is set."""
    from app.torrent import parse_torrent_file
    from tests.mocks.mock_peer import MockPeer

    torrent = parse_torrent_file(torrent_path)
    payload = payload_path.read_bytes()
    if len(payload) != torrent.total_length:
        raise ValueError(
            f"payload is {len(payload):,} bytes but {torrent_path} describes "
            f"{torrent.total_length:,}"
        )

    url = tracker.announce_url
    logger.info("tracker on %s", url)

    peers: list[MockPeer] = []
    for index in range(max(1, seeders)):
        peer = MockPeer(
            payload,
            info_hash=torrent.info_hash,
            piece_length=torrent.piece_length,
            host="127.0.0.1",
            port=0,
            peer_id=_peer_id(index),
        )
        address = await peer.start()
        peers.append(peer)

        # A seeder that never announces is not in the swarm. Announcing here is
        # what makes the client's own announce return real addresses.
        client = build_tracker(url, timeout=10.0)
        try:
            response = await client.announce(
                AnnounceRequest(
                    info_hash=torrent.info_hash,
                    peer_id=peer.peer_id,
                    port=address.port,
                    uploaded=0,
                    downloaded=torrent.total_length,
                    left=0,
                    event=TrackerEvent.STARTED,
                )
            )
            logger.info(
                "seeder %d announced (%s:%s): %s seeder(s), %s leecher(s)",
                index,
                address.host,
                address.port,
                response.seeders,
                response.leechers,
            )
        finally:
            await client.aclose()

    ready.set()
    await stop.wait()

    for peer in peers:
        await peer.stop()


def _where(thread: threading.Thread) -> str:
    """Where a thread was when we gave up waiting on it.

    A timeout that says only "did not start" costs an afternoon; a timeout that
    names the line the thread is sitting on costs a minute.
    """
    ident = thread.ident
    if ident is None:
        return " (the swarm thread never ran)"
    frame = sys._current_frames().get(ident)
    if frame is None:
        return " (the swarm thread is no longer running)"
    location = f"{frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}"
    return f" (the swarm thread is at {location})"


def _peer_id(index: int) -> bytes:
    """A recognisable peer id for a seeder, so packet logs stay readable."""
    return f"-SC0000-{index:012d}".encode()[:PEER_ID_SIZE]


class Swarm:
    """A local tracker plus seeders, running on a daemon thread.

    The GUI thread cannot run an asyncio loop *and* paint, so the swarm gets its
    own thread and its own loop — the same arrangement as running the tracker
    and seeders in separate processes, but with one command.
    """

    def __init__(self, torrent: Path, payload: Path, *, seeders: int) -> None:
        self._torrent = torrent
        self._payload = payload
        self._seeders = seeders
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self.url: str = ""

    def start(self, *, timeout: float = 30.0) -> str:
        """Start the swarm and wait until every seeder has announced."""
        ready = threading.Event()

        def run() -> None:
            async def main() -> None:
                self._loop = asyncio.get_running_loop()
                self._stop = stop = asyncio.Event()
                tracker = MockTracker(host="127.0.0.1", port=0)
                await tracker.start()
                # Read the announce URL *after* starting: the port is only
                # known once the server is bound. And it is the announce URL,
                # not the base, because a tracker without a path is a 404.
                self.url = tracker.announce_url
                try:
                    await _serve_swarm(
                        tracker,
                        self._torrent,
                        self._payload,
                        seeders=self._seeders,
                        ready=ready,
                        stop=stop,
                    )
                finally:
                    await tracker.stop()

            try:
                asyncio.run(main())
            except BaseException as error:  # noqa: BLE001 - reported by the caller
                self._error = error
                ready.set()

        thread = threading.Thread(target=run, name="swarm", daemon=True)
        self._thread = thread
        thread.start()
        if not ready.wait(timeout=timeout):
            raise TimeoutError(f"the local swarm did not start in {timeout}s{_where(thread)}")
        if self._error is not None:
            raise RuntimeError(f"the local swarm failed: {self._error}") from self._error
        return self.url

    def stop(self, *, timeout: float = 15.0) -> None:
        """Stop the swarm and join its thread."""
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            with contextlib.suppress(RuntimeError):  # already closed, already done
                loop.call_soon_threadsafe(stop.set)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None


class DhtCluster:
    """A handful of real DHT nodes on loopback, for the DHT screen to join.

    The DHT page is only worth photographing with a routing table in it, and a
    routing table can only be filled by real nodes answering real KRPC
    questions. So the tool starts its own cluster — the same trick as the local
    seeders, and for the same reason: a screenshot of zeroes would not be a
    review of anything.

    The nodes are wired to each other first, so that when the client's own node
    bootstraps against any one of them it learns about the rest and the walk has
    somewhere to go.
    """

    def __init__(self, nodes: int = 4) -> None:
        self._count = nodes
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self.addresses: tuple[tuple[str, int], ...] = ()

    def start(self, *, timeout: float = 30.0) -> tuple[tuple[str, int], ...]:
        """Start the cluster and return the addresses to bootstrap from."""
        from app.discovery.dht.node import DhtNode

        ready = threading.Event()

        def run() -> None:
            async def main() -> None:
                self._loop = asyncio.get_running_loop()
                self._stop = stop = asyncio.Event()
                nodes = [DhtNode(host="127.0.0.1", port=0) for _ in range(self._count)]
                for node in nodes:
                    await node.start()
                # Wire them to each other: node i learns about i+1, so a walk
                # from any of them reaches all of them.
                for index, node in enumerate(nodes[1:], start=1):
                    await node.bootstrap([nodes[index - 1].address])
                self.addresses = tuple(node.address for node in nodes)
                ready.set()
                try:
                    await stop.wait()
                finally:
                    for node in nodes:
                        await node.aclose()

            try:
                asyncio.run(main())
            except BaseException as error:  # noqa: BLE001 - reported by the caller
                self._error = error
                ready.set()

        thread = threading.Thread(target=run, name="dht", daemon=True)
        self._thread = thread
        thread.start()
        if not ready.wait(timeout=timeout):
            raise TimeoutError(f"the local DHT did not start in {timeout}s{_where(thread)}")
        if self._error is not None:
            raise RuntimeError(f"the local DHT failed: {self._error}") from self._error
        return self.addresses

    def stop(self, *, timeout: float = 15.0) -> None:
        """Stop every node and join the thread."""
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            with contextlib.suppress(RuntimeError):  # already closed, already done
                loop.call_soon_threadsafe(stop.set)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None


# -------------------------------------------------------------------- fixtures


def make_test_torrent(directory: Path, *, size: str = DEFAULT_SIZE) -> tuple[Path, Path]:
    """Generate a deterministic torrent and its payload in ``directory``.

    Returns:
        ``(torrent path, payload path)``.
    """
    from tools import make_test_torrent as generator

    torrent_path = directory / "test.torrent"
    generator.main(
        [
            "--size",
            size,
            "--out",
            str(torrent_path),
            "--dir",
            str(directory),
        ]
    )
    payload = directory / generator.DEFAULT_NAME
    if not payload.exists():
        raise FileNotFoundError(f"the generator wrote no payload at {payload}")
    return torrent_path, payload


# ------------------------------------------------------------------ the capture


def pump(qt: QCoreApplication, seconds: float) -> None:
    """Turn the Qt event loop for ``seconds`` without starting ``exec()``.

    ``QApplication.exec()`` blocks until the window closes, which is not what a
    screenshot tool wants. Processing events in a short loop keeps the timers —
    and therefore the state pump — running while this thread stays in control.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(0.02)
    qt.processEvents()


def report(handle: object, images: Sequence[Path]) -> dict[str, object]:
    """What was actually on screen when the pictures were taken.

    A PNG proves the interface rendered; it does not prove the numbers in it
    were real. This is the same summary the window drew, written down so a test
    can assert that peers connected and bytes moved.
    """
    from app.ui.app import UiHandle

    assert isinstance(handle, UiHandle)
    data = handle.window.view_model.as_dict()
    data["images"] = [str(path) for path in images]
    # The DHT page is the one screen whose emptiness means two different
    # things, so the report says which: enabled-and-listening, or off.
    dht = handle.window._dht_vm.summary
    data["dht"] = {
        "enabled": dht.enabled,
        "running": dht.running,
        "port": dht.port,
        "contacts": dht.contacts,
        "buckets": dht.buckets,
        "peers_known": dht.peers_known,
        "published": handle.window.session.announcer.published,
        "contacts_shown": len(handle.window._dht_vm.contacts),
        "note": dht.note,
    }
    detail = handle.window._detail_vm.as_dict()
    data["detail"] = {
        "info_hash": detail["info_hash"],
        "name": detail["name"],
        "state": detail["state"],
        "progress": detail["progress"],
        "peers": detail["peers"],
        "pieces": detail["pieces"],
        "samples": detail["samples"],
    }
    return data


def capture(handle: object, pages: Sequence[str], out: Path, *, seconds: float = 0.0) -> list[Path]:
    """Let the interface run, then write one PNG per page.

    A page may name a detail tab as ``"detail:peers"``; the swarm, the matrix
    and the graph are different screens and each one gets its own PNG.

    Args:
        handle: The :class:`~app.ui.app.UiHandle` built by :func:`build_ui`.
        pages: Page keys (with an optional ``:tab``) to capture, in order.
        out: Directory to write into; created if missing.
        seconds: How long to keep the event loop turning before capturing.

    Returns:
        The paths written.
    """
    from app.ui.app import UiHandle

    assert isinstance(handle, UiHandle)
    qt = QCoreApplication.instance()
    assert qt is not None

    out.mkdir(parents=True, exist_ok=True)
    if seconds > 0:
        pump(qt, seconds)

    written: list[Path] = []
    for key in pages:
        page, _, tab = key.partition(":")
        handle.window.show_page(page)
        name = key.replace(":", "-")
        if page == "dht":
            # Bootstrap is two or three round trips against the local cluster;
            # a photograph taken before it lands is a photograph of an empty
            # routing table.
            pump(qt, DHT_SETTLE_SECONDS)
            handle.window._refresh_dht()  # a tool may read the window it photographs
        if tab:
            # Give the detail page a moment to read the swarm and the pieces:
            # a screenshot of a tab that has not been filled yet is a
            # screenshot of a placeholder.
            pump(qt, DETAIL_SETTLE_SECONDS)
            handle.window.page("detail").show_tab(tab)  # type: ignore[attr-defined]
        pump(qt, 0.4)  # let the page lay out and the charts draw
        target = out / f"{name}.png"
        image = handle.window.grab()
        if not image.save(str(target), "PNG"):
            raise OSError(f"could not write {target}")
        written.append(target)
        # What was on screen, written next to the file: a PNG alone does not
        # say whether the swarm was mid-transfer or finished.
        summary = handle.window.view_model.summary
        print(
            f"  {target}  ({image.width()}x{image.height()})  "
            f"{summary.progress * 100:.0f}% verified · {summary.peers_connected} peer(s) · "
            f"{handle.window.view_model.as_dict()['download_rate']:.0f} B/s down"
        )
    return written


def capture_dialog(handle: object, source: str, out: Path, *, name: str) -> Path:
    """Photograph the add-torrent dialog with ``source`` already filled in.

    The magnet path is the one screen where the interface has to admit what it
    does not know, so it is worth a picture: the size is absent, and the dialog
    says why rather than printing ``0 B``.

    Args:
        handle: The built UI.
        source: What to put in the source field: a path or a magnet link.
        out: Directory to write into.
        name: File name, without the extension.

    Returns:
        The path written.
    """
    from app.ui.app import UiHandle
    from app.ui.widgets.add_torrent_dialog import AddTorrentDialog

    assert isinstance(handle, UiHandle)
    qt = QCoreApplication.instance()
    assert qt is not None
    out.mkdir(parents=True, exist_ok=True)

    dialog = AddTorrentDialog(default_directory=str(out.parent), parent=handle.window)
    dialog.set_source(source)
    dialog.show()
    pump(qt, 0.5)
    target = out / f"{name}.png"
    image = dialog.grab()
    if not image.save(str(target), "PNG"):
        raise OSError(f"could not write {target}")
    print(f"  {target}  ({image.width()}x{image.height()})  dialog: {source[:48]}")
    dialog.reject()
    return target


# ---------------------------------------------------------------------- the tool


def _config_path(
    directory: Path,
    theme: str,
    download_directory: Path,
    *,
    dht_nodes: tuple[tuple[str, int], ...] = (),
) -> Path:
    """Write a temporary configuration and return its path.

    The composition root reads its configuration from a file, so a tool that
    wants a different theme writes one — rather than reaching into the
    ``Application`` and rebuilding its session by hand.
    """
    from app.core.config import Config

    config = Config().with_overrides(
        ui={"theme": theme},
        storage={"download_directory": str(download_directory)},
        network={"accept_incoming_connections": False},
        # DHT is on only when the tool started a cluster for it to join; the
        # bootstrap nodes are that cluster, not the public routers, so the
        # screenshot does not depend on the internet.
        dht={"enabled": bool(dht_nodes), "bootstrap_nodes": list(dht_nodes)},
    )
    path = directory / "config.json"
    config.save(path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    if args.torrent is not None and args.payload is None:
        print("--torrent needs --payload: a seeder has to have something to serve")
        return 2

    pages = [page.strip() for page in args.pages.split(",") if page.strip()]
    swarm: Swarm | None = None
    dht: DhtCluster | None = None
    workspace: Path | None = None
    _added_info_hash = ""
    code = 0

    try:
        with tempfile.TemporaryDirectory(prefix="screenshot-") as temporary:
            workspace = Path(temporary)
            torrent, payload = (args.torrent, args.payload)
            if torrent is None:
                torrent, payload = make_test_torrent(workspace)
                print(f"{PROGRAM_NAME}: generated {torrent.name} ({DEFAULT_SIZE})")

            if not args.no_swarm:
                assert payload is not None
                swarm = Swarm(torrent, payload, seeders=args.seeders)
                swarm.start()
                print(f"{PROGRAM_NAME}: local swarm ready")

            dht_nodes: tuple[tuple[str, int], ...] = ()
            if args.dht_nodes and not args.no_swarm:
                dht = DhtCluster(args.dht_nodes)
                dht_nodes = dht.start()
                print(f"{PROGRAM_NAME}: local DHT ready with {len(dht_nodes)} node(s)")

            # Imported here so a missing display is reported after the argument
            # check, with a message that says what to set.
            from app.ui.app import build_ui

            downloads = workspace / "downloads"
            downloads.mkdir(exist_ok=True)
            handle = build_ui(
                [PROGRAM_NAME],
                config_path=_config_path(workspace, args.theme, downloads, dht_nodes=dht_nodes),
                load_config=True,
            )
            handle.window.set_config_saver(None)
            handle.bridge.start()
            # Start the application too, which is what starts the DHT: the
            # screenshot tool drives the same composition root the real client
            # does, so a screen that is empty here would be empty for a user.
            handle.bridge.submit(handle.application.start())
            handle.window.resize(args.width, args.height)
            handle.window.show()

            if swarm is not None:
                _added_info_hash = _add_torrent(handle, torrent, swarm.url)
                # The detail page is only a picture of something once a torrent
                # is selected; selecting it also starts its slower reads.
                handle.window.select_torrent(_added_info_hash)

            print(f"{PROGRAM_NAME}: capturing {', '.join(pages)}")
            images = capture(handle, pages, args.out, seconds=args.seconds)
            if args.magnet:
                images.append(capture_dialog(handle, args.magnet, args.out, name="add-magnet"))
            print(f"{PROGRAM_NAME}: wrote {len(images)} image(s) to {args.out}")

            summary = report(handle, images)
            if args.report is not None:
                args.report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            print(
                f"{PROGRAM_NAME}: {summary['peers_connected']} peer(s), "
                f"{summary['downloaded_bytes']} byte(s) received"
            )

            handle.bridge.submit(handle.application.stop()).result(timeout=15)
            handle.bridge.stop()
    except Exception as error:  # a tool reports the failure; it does not raise
        print(f"{PROGRAM_NAME}: failed: {error}")
        logger.debug("screenshot run failed", exc_info=True)
        code = 1
    finally:
        if dht is not None:
            dht.stop()
        if swarm is not None:
            swarm.stop()
        _ = workspace
    return code


def _add_torrent(handle: object, torrent_path: Path, tracker_url: str) -> str:
    """Add a torrent to the running session without blocking the GUI thread."""
    from app.torrent import parse_torrent_file
    from app.ui.app import UiHandle

    assert isinstance(handle, UiHandle)
    torrent = parse_torrent_file(torrent_path)
    url = tracker_url

    async def add() -> str:
        trackers = [[build_tracker(url, timeout=10.0)]]
        engine = await handle.session.add_torrent(torrent, trackers=trackers)
        return engine.hex_info_hash

    # The bridge must be running before anything can be submitted to it; the
    # caller starts it, and this is where the first work is handed over.
    added: str = handle.bridge.submit(add()).result(timeout=30)
    handle.bridge.refresh()
    print(f"{PROGRAM_NAME}: added {torrent.name} ({added})")
    return added


if __name__ == "__main__":
    sys.exit(main())
