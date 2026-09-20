"""Command-line front-end.

The CLI exists for three reasons: it proves the engine works before any UI
does, it is the fastest way to debug protocol behaviour, and it lets the client
run headless. It drives exactly the same application services the desktop UI
will use — there is no CLI-specific engine path (TRD §35).

Commands are added as milestones land:

* ``info`` — inspect torrent metadata (available now)
* ``announce`` — ask a tracker for peers (available now)
* ``download`` — fetch a torrent through the real engine, then seed it (M10)

Usage::

    python -m cli.main info ubuntu.torrent --files
    python -m cli.main announce ubuntu.torrent
    python -m cli.main announce ubuntu.torrent --tracker http://tracker:6969/a
    python -m cli.main download ubuntu.torrent --download-dir ./incoming
    python -m cli.main download ubuntu.torrent --no-seed --timeout 600

``download`` runs the same :class:`~app.services.session.Session` the desktop
UI will drive: real trackers, real peer connections, real disk. It prints
progress measured from the wire, and it stops cleanly on Ctrl-C — which saves
resume state, so the next run picks up where this one stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__
from app.core.config import Config, TrackerConfig
from app.core.constants import DEFAULT_LISTEN_PORT
from app.services import Engine, Session
from app.torrent import Torrent, parse_torrent_file
from app.torrent.errors import TorrentError
from app.tracker import AnnounceOutcome, Tracker, TrackerEvent, TrackerManager
from app.tracker.errors import TrackerError, UnsupportedTrackerError
from app.tracker.factory import build_tracker

PROGRAM_NAME: str = "bittorrent"
DEFAULT_MAX_FILES: int = 20
DEFAULT_ANNOUNCE_PEER_LIMIT: int = 10

# How long ``download`` waits by default. Six hours: long enough for a real
# torrent, short enough that a wedged client says so instead of hanging.
DEFAULT_DOWNLOAD_TIMEOUT: float = 6 * 3600.0
# How often the progress line is redrawn. Fast enough to feel alive, slow
# enough that printing is not the bottleneck.
PROGRESS_INTERVAL: float = 0.5
# Exit code for Ctrl-C, by convention.
INTERRUPTED_EXIT_CODE: int = 130

_SIZE_UNITS: tuple[tuple[float, str], ...] = (
    (1024**4, "TiB"),
    (1024**3, "GiB"),
    (1024**2, "MiB"),
    (1024, "KiB"),
)


def human_size(size: int) -> str:
    """Render a byte count in binary units, e.g. ``5.7 GiB``."""
    for factor, suffix in _SIZE_UNITS:
        if size >= factor:
            return f"{size / factor:.2f} {suffix}"
    return f"{size} B"


def _format_timestamp(timestamp: int | None) -> str:
    if timestamp is None:
        return "unknown"
    try:
        moment = datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return "invalid"
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def torrent_summary(torrent: Torrent, *, max_files: int = DEFAULT_MAX_FILES) -> dict[str, Any]:
    """Build a JSON-serialisable summary of a torrent."""
    return {
        "name": torrent.name,
        "info_hash": torrent.hex_info_hash,
        "total_length": torrent.total_length,
        "human_size": human_size(torrent.total_length),
        "piece_length": torrent.piece_length,
        "piece_count": torrent.piece_count,
        "last_piece_length": torrent.last_piece_length,
        "private": torrent.private,
        "single_file": torrent.is_single_file,
        "announce": torrent.announce,
        "trackers": list(torrent.trackers),
        "created_by": torrent.created_by,
        "creation_date": torrent.creation_date,
        "creation_date_utc": _format_timestamp(torrent.creation_date),
        "comment": torrent.comment,
        "files": [
            {"path": str(entry.path), "length": entry.length, "offset": entry.offset}
            for entry in torrent.files[:max_files]
        ],
        "file_count": len(torrent.files),
        "files_truncated": len(torrent.files) > max_files,
    }


def _print_human_summary(torrent: Torrent, *, max_files: int) -> None:
    """Print a readable metadata report."""
    print(torrent.name)
    print(f"  info hash   {torrent.hex_info_hash}")
    print(
        f"  size        {human_size(torrent.total_length)} "
        f"({torrent.total_length:,} bytes) in {len(torrent.files)} file(s)"
    )
    print(
        f"  pieces      {torrent.piece_count:,} x {human_size(torrent.piece_length)}"
        f"  (last piece {torrent.last_piece_length:,} bytes)"
    )
    print(f"  created by  {torrent.created_by or 'unknown'}")
    print(f"  created     {_format_timestamp(torrent.creation_date)}")
    print(f"  comment     {torrent.comment or '-'}")
    print(f"  private     {'yes' if torrent.private else 'no'}")

    if torrent.trackers:
        print(f"  trackers    {len(torrent.trackers)}")
        for url in torrent.trackers:
            print(f"              {url}")
    else:
        print("  trackers    none (DHT/PEX required)")

    shown = torrent.files[:max_files]
    print(f"  files       {len(torrent.files)}")
    for entry in shown:
        print(f"              {entry.length:>14,}  {entry.path}")
    if len(torrent.files) > len(shown):
        print(f"              ... {len(torrent.files) - len(shown)} more (--max-files)")


def command_info(args: argparse.Namespace) -> int:
    """Handle ``info``: parse and describe a torrent file."""
    try:
        torrent = parse_torrent_file(args.torrent)
    except TorrentError as exc:
        print(f"{PROGRAM_NAME}: cannot read {args.torrent}: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(
            json.dumps(
                torrent_summary(torrent, max_files=args.max_files),
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        _print_human_summary(torrent, max_files=args.max_files)
    return 0


def _build_tracker_tiers(args: argparse.Namespace) -> tuple[tuple[Tracker, ...], ...]:
    """Build tracker tiers from ``--tracker``, or from the torrent when absent.

    The client is chosen from the URL's scheme, so ``--tracker udp://host:6969``
    works the same way as an HTTP one.

    Raises:
        UnsupportedTrackerError: If a URL uses a scheme we cannot speak.
    """
    if args.tracker:
        return tuple((build_tracker(url, timeout=args.timeout),) for url in args.tracker)
    return ()


def _print_announce(outcome: AnnounceOutcome, *, limit: int) -> None:
    """Print the result of an announce in human-readable form."""
    response = outcome.response
    print(f"  tracker      {outcome.tracker.url}")
    print(
        f"  result       ok in {outcome.latency_ms:.0f} ms "
        f"(interval {response.interval}s, next announce in "
        f"{outcome.next_announce_in:.0f}s)"
    )
    if response.swarm_reported:
        print(f"  swarm        {response.seeders} seeders, {response.leechers} leechers")
    else:
        print("  swarm        not reported by this tracker")
    if response.warning_message:
        print(f"  warning      {response.warning_message}")
    print(f"  peers        {len(outcome.peers)} ({len(outcome.new_peers)} new)")

    for peer in outcome.peers[:limit]:
        marker = "*" if peer in outcome.new_peers else " "
        print(f"             {marker} {peer.address[0]}:{peer.address[1]}")
    if len(outcome.peers) > limit:
        print(f"               ... {len(outcome.peers) - limit} more (--max-peers)")


def command_announce(args: argparse.Namespace) -> int:
    """Handle ``announce``: ask trackers for peers and print the answer."""
    try:
        torrent = parse_torrent_file(args.torrent)
        tiers = _build_tracker_tiers(args)
    except (TorrentError, UnsupportedTrackerError) as exc:
        print(f"{PROGRAM_NAME}: {exc}", file=sys.stderr)
        return 1

    config = TrackerConfig(http_timeout=args.timeout)
    manager = TrackerManager(
        torrent,
        trackers=tiers,
        config=config,
        port=args.port,
    )

    async def announce_and_close() -> AnnounceOutcome:
        try:
            return await manager.announce(event=TrackerEvent.STARTED, num_want=args.num_want)
        finally:
            # One loop for the work and the cleanup: a UDP socket opened on one
            # loop cannot be closed on another, and the CLI used to try.
            await manager.aclose()

    try:
        outcome = asyncio.run(announce_and_close())
    except TrackerError as exc:
        print(f"{PROGRAM_NAME}: announce failed: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(
            json.dumps(
                {
                    "info_hash": torrent.hex_info_hash,
                    "tracker": outcome.tracker.url,
                    "latency_ms": round(outcome.latency_ms, 2),
                    "interval": outcome.response.interval,
                    "min_interval": outcome.response.min_interval,
                    "seeders": outcome.response.seeders,
                    "leechers": outcome.response.leechers,
                    "warning": outcome.response.warning_message,
                    "peers": [f"{peer.host}:{peer.port}" for peer in outcome.peers],
                },
                indent=2,
            )
        )
        return 0

    print(torrent.name)
    _print_announce(outcome, limit=args.max_peers)
    return 0


def _progress_line(engine: Engine, *, elapsed: float) -> str:
    """One line of measured progress. Nothing on it is guessed."""
    snapshot = engine.snapshot()
    downloaded = snapshot.download.total
    rate = snapshot.download.short
    eta = snapshot.eta_seconds
    parts = [
        f"{snapshot.progress * 100:6.2f}%",
        f"{human_size(downloaded)} / {human_size(snapshot.total_bytes)}",
        f"{human_size(int(rate))}/s",
        f"eta {'--' if eta is None else f'{eta / 60:.1f}m'}",
        f"peers {snapshot.peers_connected} ({snapshot.peers_unchoked} unchoked)",
        f"{snapshot.pieces_verified}/{snapshot.pieces_total} pieces",
        f"{elapsed:.0f}s",
    ]
    return "  ".join(parts)


async def _show_progress(engine: Engine, *, interval: float = PROGRESS_INTERVAL) -> None:
    """Redraw the progress line until cancelled."""
    start = time.monotonic()
    while True:
        print(
            f"\r  {_progress_line(engine, elapsed=time.monotonic() - start):<100}",
            end="",
            flush=True,
        )
        await asyncio.sleep(interval)


async def _wait_with_progress(engine: Engine, *, timeout: float, quiet: bool) -> bool:
    """Wait for completion, showing measured progress unless told not to."""
    if quiet:
        return await engine.wait_until_complete(timeout=timeout)
    task = asyncio.create_task(_show_progress(engine))
    try:
        return await engine.wait_until_complete(timeout=timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        print()


async def _seed(engine: Engine, *, minutes: float) -> None:
    """Keep serving the torrent until the time is up or we are interrupted."""
    deadline = None if minutes <= 0 else time.monotonic() + minutes * 60.0
    print(f"  seeding on port {engine.port} — press Ctrl-C to stop")
    while deadline is None or time.monotonic() < deadline:
        snapshot = engine.snapshot()
        print(
            f"\r  up {human_size(int(snapshot.upload.short))}/s   "
            f"served {human_size(snapshot.upload.total)}   "
            f"peers {snapshot.peers_connected:<3}      ",
            end="",
            flush=True,
        )
        await asyncio.sleep(PROGRESS_INTERVAL)


async def _run_download(torrent: Torrent, args: argparse.Namespace) -> int:
    """Download (and optionally seed) one torrent. Returns an exit code."""
    directory = Path(args.download_dir).expanduser().resolve() if args.download_dir else None
    config = Config()
    if directory is not None:
        config = config.with_overrides(storage={"download_directory": str(directory)})

    async with Session(config) as session:
        engine = await session.add_torrent(
            torrent,
            listen=not args.no_listen,
            resume=not args.no_resume,
            trackers=_extra_tiers(args),
        )
        if not quiet(args):
            print(torrent.name)
            print(f"  info hash   {torrent.hex_info_hash}")
            print(f"  saving to   {engine.storage.root}")
            print(f"  listening   port {engine.port}" if engine.port else "  listening   off")
            if not engine.resumed.empty:
                print(f"  resumed     {engine.resumed.pieces} piece(s) from disk")

        try:
            finished = await _wait_with_progress(engine, timeout=args.timeout, quiet=quiet(args))
            if not args.no_seed and finished:
                await _seed(engine, minutes=args.seed_minutes)
        except (KeyboardInterrupt, asyncio.CancelledError):
            if not quiet(args):
                print("\n  interrupted — progress saved, resume with the same command")
            return INTERRUPTED_EXIT_CODE

        summary = _download_summary(engine, finished=finished)

    if args.as_json:
        print(json.dumps(summary, indent=2))
        return 0 if finished else 1

    _print_download_summary(summary)
    return 0 if finished else 1


def quiet(args: argparse.Namespace) -> bool:
    """Whether progress output is suppressed."""
    return bool(args.quiet or args.as_json)


def _download_summary(engine: Engine, *, finished: bool) -> dict[str, Any]:
    """Everything the run achieved, measured."""
    snapshot = engine.snapshot()
    downloaded = snapshot.download.total
    elapsed = snapshot.elapsed
    return {
        "name": engine.torrent.name,
        "info_hash": engine.hex_info_hash,
        "state": engine.state.value,
        "finished": finished,
        "progress": round(snapshot.progress, 5),
        "pieces_verified": snapshot.pieces_verified,
        "pieces_total": snapshot.pieces_total,
        "bytes_downloaded": downloaded,
        "bytes_uploaded": snapshot.upload.total,
        "download_rate": round(snapshot.download.average, 3),
        "upload_rate": round(snapshot.upload.average, 3),
        "elapsed_seconds": round(elapsed, 3),
        "peers_connected": snapshot.peers_connected,
        "directory": str(engine.storage.root),
    }


def _print_download_summary(summary: dict[str, Any]) -> None:
    """Print the end-of-run report."""
    if not summary["finished"]:
        print(f"  gave up after {summary['elapsed_seconds']:.0f}s")
    print(
        f"  downloaded  {human_size(summary['bytes_downloaded'])} "
        f"({summary['bytes_downloaded']:,} bytes)"
    )
    print(f"  pieces      {summary['pieces_verified']}/{summary['pieces_total']} verified")
    print(
        f"  rate        {human_size(int(summary['download_rate']))}/s average "
        f"over {summary['elapsed_seconds']:.1f}s"
    )
    print(f"  uploaded    {human_size(summary['bytes_uploaded'])}")
    print(f"  saved to    {summary['directory']}")


def _extra_tiers(args: argparse.Namespace) -> tuple[tuple[Tracker, ...], ...] | None:
    """``--tracker`` as tracker tiers, or ``None`` to use the torrent's own."""
    if not args.tracker:
        return None
    return tuple((build_tracker(url),) for url in args.tracker)


def command_download(args: argparse.Namespace) -> int:
    """Handle ``download``: fetch a torrent, then seed it."""
    try:
        torrent = parse_torrent_file(args.torrent)
    except TorrentError as exc:
        print(f"{PROGRAM_NAME}: cannot read {args.torrent}: {exc}", file=sys.stderr)
        return 1

    try:
        return asyncio.run(_run_download(torrent, args))
    except KeyboardInterrupt:  # pragma: no cover - depends on delivery timing
        return INTERRUPTED_EXIT_CODE


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="BitTorrent client: inspect and download torrents.",
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM_NAME} {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    info = subcommands.add_parser("info", help="show torrent metadata")
    info.add_argument("torrent", type=Path, help="path to a .torrent file")
    info.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    info.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help=f"maximum files to list (default: {DEFAULT_MAX_FILES})",
    )
    info.set_defaults(handler=command_info)

    announce = subcommands.add_parser("announce", help="ask trackers for peers")
    announce.add_argument("torrent", type=Path, help="path to a .torrent file")
    announce.add_argument(
        "--tracker",
        action="append",
        metavar="URL",
        help="tracker to announce to (repeatable; defaults to the torrent's own)",
    )
    announce.add_argument(
        "--port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help=f"port we listen on (default: {DEFAULT_LISTEN_PORT})",
    )
    announce.add_argument(
        "--num-want",
        type=int,
        default=50,
        help="how many peers to ask for (default: 50)",
    )
    announce.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="per-tracker timeout in seconds (default: 15)",
    )
    announce.add_argument(
        "--max-peers",
        type=int,
        default=DEFAULT_ANNOUNCE_PEER_LIMIT,
        help=f"peers to print (default: {DEFAULT_ANNOUNCE_PEER_LIMIT})",
    )
    announce.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    announce.set_defaults(handler=command_announce)

    download = subcommands.add_parser("download", help="download a torrent, then seed it")
    download.add_argument("torrent", type=Path, help="path to a .torrent file")
    download.add_argument(
        "--download-dir",
        type=Path,
        default=None,
        help="where to save the data (default: the configured download directory)",
    )
    download.add_argument(
        "--tracker",
        action="append",
        metavar="URL",
        help="extra tracker to announce to (repeatable; adds to the torrent's own)",
    )
    download.add_argument(
        "--no-listen",
        action="store_true",
        help="do not accept inbound connections (slower; we cannot be reciprocated)",
    )
    download.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore progress from previous runs and start from scratch",
    )
    download.add_argument(
        "--no-seed",
        action="store_true",
        help="exit as soon as the download finishes instead of seeding",
    )
    download.add_argument(
        "--seed-minutes",
        type=float,
        default=0.0,
        help="seed for this many minutes after finishing (default: until Ctrl-C)",
    )
    download.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_DOWNLOAD_TIMEOUT,
        help=f"give up after this many seconds (default: {DEFAULT_DOWNLOAD_TIMEOUT:.0f})",
    )
    download.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing while downloading, only the final summary",
    )
    download.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a machine-readable summary at the end",
    )
    download.set_defaults(handler=command_download)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Argument list; ``sys.argv[1:]`` is used when omitted.

    Returns:
        Process exit code: 0 on success, 1 on a handled error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:  # pragma: no cover - argparse requires a subcommand
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
