"""Download a real torrent from a local swarm, using the real engine.

This is the download engine's end-to-end proof: it starts N seeders over a real
payload, connects to them with the real peer manager over real TCP, runs
:class:`~app.download.manager.DownloadManager`, and checks that what lands on
disk is byte-for-byte what the torrent's hashes promised.

Nothing here is simulated except the swarm itself, and the swarm is not
simulated either — the seeders are real servers speaking the real wire
protocol over real sockets.

Examples::

    python tools/swarm_download.py --size 8MiB --seeds 3 --out /tmp/swarm
    python tools/swarm_download.py --size 2MiB --seeds 1 --blocks 4 --out /tmp/swarm
    python tools/swarm_download.py --size 1MiB --strategy sequential --out /tmp/swarm
    python tools/swarm_download.py --size 2MiB --kill-after 20 --out /tmp/swarm
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.config import DownloadConfig, NetworkConfig, PieceStrategy, StorageConfig
from app.core.event_bus import EventBus
from app.core.peer_id import generate_peer_id
from app.download.manager import DownloadManager
from app.peer.connection import SwarmContext
from app.peer.discovery.peer_manager import PeerManager
from app.storage.manager import StorageManager
from app.torrent import Torrent, parse_torrent
from tests.mocks.mock_peer import MockPeer

from tools.make_test_torrent import (
    build_torrent_bytes,
    generate_payload,
    parse_size,
)

logger = logging.getLogger(__name__)

DEFAULT_SIZE: str = "8MiB"
DEFAULT_SEEDS: int = 3
DEFAULT_PIECE_LENGTH: int = 256 * 1024
DEFAULT_SEED: int = 2024


@dataclass(frozen=True, slots=True)
class SwarmReport:
    """Everything one run measured."""

    name: str
    hex_info_hash: str
    total_length: int
    piece_count: int
    seeds: int
    seeds_killed: int
    slow_seeds: int
    pieces_verified: int
    pieces_failed: int
    blocks_received: int
    duplicate_blocks: int
    wasted_bytes: int
    requests_sent: int
    elapsed: float
    verified: bool
    strategy: str

    @property
    def bytes_per_second(self) -> float:
        """Measured throughput over the whole run."""
        if self.elapsed <= 0:
            return 0.0
        return self.total_length / self.elapsed

    @property
    def successful(self) -> bool:
        """True when the download finished and the data hashes correctly."""
        return self.verified and self.pieces_verified == self.piece_count


async def run_swarm(
    torrent: Torrent,
    payload: bytes,
    *,
    directory: Path,
    state_directory: Path,
    seeds: int = DEFAULT_SEEDS,
    config: DownloadConfig | None = None,
    kill_after: int | None = None,
    slow_seeds: int = 0,
    slow_delay: float = 0.5,
) -> SwarmReport:
    """Download ``torrent`` from a local swarm of seeders.

    Args:
        torrent: The torrent to download; its hashes describe ``payload``.
        payload: The real data the seeders serve.
        directory: Where to write the download.
        state_directory: Where resume state is written.
        seeds: How many seeders to start.
        config: Download settings.
        kill_after: Kill this many seeders once the download is under way, to
            prove that losing peers mid-download costs only time.
        slow_seeds: How many seeders answer every block after ``slow_delay``
            seconds — the case endgame mode exists to work around.
        slow_delay: How long the slow seeders take per block.

    Returns:
        The measured outcome.
    """
    settings = config or DownloadConfig()
    slow = max(0, min(slow_seeds, max(1, seeds)))
    seeders = [
        MockPeer(
            payload,
            info_hash=torrent.info_hash,
            piece_length=torrent.piece_length,
            request_delay=slow_delay if number < slow else 0.0,
        )
        for number in range(max(1, seeds))
    ]
    for seeder in seeders:
        await seeder.start()

    storage = StorageManager(
        torrent,
        directory,
        config=StorageConfig(
            state_directory=state_directory, preallocate_files=True, verify_before_write=True
        ),
    )
    await storage.prepare()

    context = SwarmContext.from_torrent(torrent)
    bus = EventBus()
    peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
    manager = DownloadManager(torrent, storage=storage, peers=peers, config=settings, event_bus=bus)
    peers.on_block = manager.on_block
    peers.on_disconnect = manager.on_disconnect
    peers.on_have = manager.on_have
    peers.add_peers([seeder.address for seeder in seeders], source="local swarm")

    killed = 0
    started = time.monotonic()
    try:
        await peers.fill()
        await manager.start()
        if kill_after:
            killed = await _kill_seeders(seeders, kill_after, manager)
        await manager.wait_until_complete(timeout=_timeout_for(torrent.total_length))
        await manager.flush()
    finally:
        await manager.stop()
        await peers.stop()
        for seeder in seeders:
            await seeder.stop()
    elapsed = time.monotonic() - started

    verified = await _matches(torrent, directory)
    report = SwarmReport(
        name=torrent.name,
        hex_info_hash=torrent.hex_info_hash,
        total_length=torrent.total_length,
        piece_count=torrent.piece_count,
        seeds=len(seeders),
        seeds_killed=killed,
        slow_seeds=slow,
        pieces_verified=manager.stats.pieces_verified,
        pieces_failed=manager.stats.pieces_failed,
        blocks_received=manager.stats.blocks_received,
        duplicate_blocks=manager.stats.blocks_duplicate,
        wasted_bytes=manager.stats.wasted_bytes,
        requests_sent=manager.stats.requests_sent,
        elapsed=elapsed,
        verified=verified,
        strategy=settings.piece_strategy.value,
    )
    await storage.aclose()
    return report


def _timeout_for(total_length: int) -> float:
    """Generous but finite: a deadlock must fail the run, not hang it."""
    return max(30.0, total_length / (256 * 1024))


async def _kill_seeders(seeders: list[MockPeer], count: int, manager: DownloadManager) -> int:
    """Stop ``count`` seeders once the download is under way."""
    killed = 0
    for seeder in seeders[:count]:
        deadline = time.monotonic() + 20.0
        while manager.stats.pieces_verified == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await seeder.stop()
        killed += 1
    return killed


async def _matches(torrent: Torrent, directory: Path) -> bool:
    """Re-read every file from disk and compare it to the torrent's hashes."""
    expected = torrent.piece_hashes
    for index in range(torrent.piece_count):
        blob = await asyncio.to_thread(_read_piece, torrent, directory, index)
        if blob is None:
            return False
        if hashlib.sha1(blob).digest() != expected[index]:
            return False
    return True


def _read_piece(torrent: Torrent, directory: Path, index: int) -> bytes | None:
    """Read one piece's bytes back out of the downloaded files."""
    start = torrent.piece_offset(index)
    size = torrent.piece_size(index)
    chunks: list[bytes] = []
    for entry in torrent.files_in_piece(index):
        path = directory / entry.path
        local_start = max(start, entry.offset) - entry.offset
        local_end = min(start + size, entry.end_offset) - entry.offset
        if local_end <= local_start:
            continue
        try:
            with path.open("rb") as handle:
                handle.seek(local_start)
                chunks.append(handle.read(local_end - local_start))
        except OSError:
            return None
    blob = b"".join(chunks)
    return blob if len(blob) == size else None


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def render(report: SwarmReport, *, directory: Path) -> None:
    """Print a swarm download report."""
    print(f"Swarm download: {report.name}")
    print(f"  info hash    {report.hex_info_hash}")
    print(
        f"  size         {_human_bytes(report.total_length)} in "
        f"{report.piece_count} pieces, {report.strategy} selection"
    )
    print(f"  directory    {directory}")
    print(
        f"  swarm        {report.seeds} seeder(s)"
        + (f", {report.seeds_killed} killed" if report.seeds_killed else "")
    )
    print()
    print(
        f"  pieces       {report.pieces_verified}/{report.piece_count} verified, {report.pieces_failed} rejected"
    )
    print(f"  blocks       {report.blocks_received} received, {report.duplicate_blocks} duplicate")
    print(f"  requests     {report.requests_sent} sent, {_human_bytes(report.wasted_bytes)} wasted")
    print(f"  time         {report.elapsed:.2f}s ({_human_bytes(int(report.bytes_per_second))}/s)")
    print()
    if report.successful:
        print("  RESULT ok: the file on disk hashes to exactly what the torrent promised")
    else:
        print("  RESULT failed: the download did not finish, or the data does not match")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="swarm_download",
        description="Download a generated torrent from a local swarm of real seeders.",
    )
    parser.add_argument(
        "--size", default=DEFAULT_SIZE, help=f"payload size (default: {DEFAULT_SIZE})"
    )
    parser.add_argument("--piece-length", type=int, default=DEFAULT_PIECE_LENGTH)
    parser.add_argument("--blocks", type=int, default=16, help="pipelined requests per peer")
    parser.add_argument("--seeds", type=int, default=DEFAULT_SEEDS, help="seeder count")
    parser.add_argument("--files", type=int, default=1, help="split the payload into N files")
    parser.add_argument(
        "--kill-after",
        type=int,
        default=None,
        metavar="N",
        help="kill N seeders mid-download to test recovery",
    )
    parser.add_argument(
        "--slow-seeds",
        type=int,
        default=0,
        metavar="N",
        help="make N seeders answer every block late",
    )
    parser.add_argument(
        "--slow-delay",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="how late the slow seeders are (default: 0.5)",
    )
    parser.add_argument(
        "--strategy",
        type=PieceStrategy,
        choices=list(PieceStrategy),
        default=PieceStrategy.RAREST_FIRST,
    )
    parser.add_argument("--no-endgame", action="store_true", help="disable endgame mode")
    parser.add_argument("--out", type=Path, default=Path("data/swarm-download"))
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--keep", action="store_true", help="keep the download directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns 0 when the download completed and verified."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    payload = generate_payload(parse_size(args.size), seed=DEFAULT_SEED)
    torrent = parse_torrent(
        build_torrent_bytes(
            payload, name="swarm", piece_length=args.piece_length, file_count=max(1, args.files)
        )
    )
    directory: Path = args.out
    state_directory = args.state_dir or directory.parent / "state"
    if directory.exists():
        shutil.rmtree(directory)

    config = DownloadConfig(
        block_size=16 * 1024,
        max_outstanding_requests=max(1, args.blocks),
        piece_strategy=args.strategy,
        endgame_enabled=not args.no_endgame,
    )
    try:
        report = asyncio.run(
            run_swarm(
                torrent,
                payload,
                directory=directory,
                state_directory=state_directory,
                seeds=args.seeds,
                config=config,
                kill_after=args.kill_after,
                slow_seeds=args.slow_seeds,
                slow_delay=args.slow_delay,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a diagnostic tool reports, never crashes
        print(f"swarm download failed: {exc}", file=sys.stderr)
        return 1

    render(report, directory=directory)
    if not args.keep:
        shutil.rmtree(directory, ignore_errors=True)
    return 0 if report.successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
