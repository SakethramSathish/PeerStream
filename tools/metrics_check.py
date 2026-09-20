"""Measure a real download and a real upload with the statistics engine.

This is the statistics engine's end-to-end proof, and it is deliberately the
same shape as the others: a real payload, real seeders over TCP, the real
download engine, and a :class:`MetricsCollector` sampling while the bytes move.
Then the torrent is seeded back to real leechers, so both directions of the
snapshot are filled in by something that actually happened.

The report is not a summary of what the engines said about themselves. The
rates come from bytes arriving and leaving, the ETA comes from the rates, and
the history is checked against its own bound.

Examples::

    python tools/metrics_check.py --size 8MiB --seeds 3 --out /tmp/metrics
    python tools/metrics_check.py --size 2MiB --interval 0.05 --leechers 2
    python tools/metrics_check.py --size 1MiB --no-upload --out /tmp/metrics
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import (
    DownloadConfig,
    NetworkConfig,
    StatsConfig,
    StorageConfig,
    UploadConfig,
)
from app.core.event_bus import EventBus
from app.core.peer_id import generate_peer_id
from app.download.manager import DownloadManager
from app.peer.bitfield import Bitfield
from app.peer.connection import SwarmContext
from app.peer.discovery.listener import PeerListener
from app.peer.discovery.peer_manager import PeerManager
from app.statistics.metrics import (
    SERIES_DOWNLOAD_INSTANT,
    SERIES_ETA,
    SERIES_PROGRESS,
    MetricsCollector,
    MetricsSnapshot,
)
from app.storage.manager import StorageManager
from app.torrent import Torrent, parse_torrent
from app.upload.manager import UploadManager
from tests.mocks.mock_leecher import MockLeecher
from tests.mocks.mock_peer import MockPeer

from tools.make_test_torrent import (
    build_torrent_bytes,
    generate_payload,
    parse_size,
)
from tools.swarm_download import _human_bytes

logger = logging.getLogger(__name__)

DEFAULT_SIZE: str = "8MiB"
DEFAULT_SEEDS: int = 3
DEFAULT_LEECHERS: int = 2
DEFAULT_PIECE_LENGTH: int = 256 * 1024
DEFAULT_INTERVAL: float = 0.1
DEFAULT_SEED: int = 2024
BLOCK = 16 * 1024


@dataclass(frozen=True, slots=True)
class MetricsCheck:
    """Everything one run measured.

    The two rates are the point of the exercise: ``downloaded_bytes`` and
    ``uploaded_bytes`` were counted as the bytes moved, and every rate here is
    one of those numbers divided by a time that was measured with the same
    clock.
    """

    name: str
    hex_info_hash: str
    total_length: int
    piece_count: int
    seeds: int
    leechers: int
    downloaded_bytes: int
    uploaded_bytes: int
    elapsed: float
    samples_taken: int
    history_capacity: int
    download_average: float
    download_peak: float
    upload_average: float
    first_eta: float | None
    last_eta: float | None
    pieces_verified: int
    wasted_bytes: int
    verified: bool
    complete: bool
    final: MetricsSnapshot
    series: dict[str, tuple[tuple[float, float], ...]] = field(default_factory=dict)

    @property
    def successful(self) -> bool:
        """True when the bytes moved, were counted, and the result verified."""
        return (
            self.verified
            and self.downloaded_bytes > 0
            and self.download_average > 0
            and self.pieces_verified == self.piece_count
        )

    @property
    def history_within_bounds(self) -> bool:
        """No series grew past its capacity, however long the run lasted."""
        return all(len(samples) <= self.history_capacity for samples in self.series.values())


async def run_metrics_check(
    torrent: Torrent,
    payload: bytes,
    *,
    directory: Path,
    state_directory: Path,
    seeds: int = DEFAULT_SEEDS,
    leechers: int = DEFAULT_LEECHERS,
    interval: float = DEFAULT_INTERVAL,
    upload: bool = True,
    slow_delay: float = 0.0,
) -> MetricsCheck:
    """Download ``torrent`` from local seeders while sampling, then seed it back.

    Args:
        torrent: The torrent to download; its hashes describe ``payload``.
        payload: The real data the seeders serve.
        directory: Where the download is written.
        state_directory: Where resume state is written.
        seeds: How many seeders to download from.
        leechers: How many peers to seed back to afterwards.
        interval: How often to sample, in seconds.
        upload: Whether to seed back at all (the upload half of the snapshot).
        slow_delay: Seconds each seeder waits before answering a block. Zero
            is the fast path; a small delay stretches the download across
            enough samples to watch an ETA fall, which is the only way to
            test one honestly.

    Returns:
        The measured outcome, with the series the graphs would draw.
    """
    seeders = [
        MockPeer(
            payload,
            info_hash=torrent.info_hash,
            piece_length=torrent.piece_length,
            request_delay=slow_delay,
        )
        for _ in range(max(1, seeds))
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
    bus = EventBus()
    context = SwarmContext.from_torrent(torrent)
    peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
    manager = DownloadManager(
        torrent,
        storage=storage,
        peers=peers,
        config=DownloadConfig(block_size=BLOCK, max_outstanding_requests=8),
        event_bus=bus,
        block_timeout=5.0,
    )
    peers.on_block = manager.on_block
    peers.on_disconnect = manager.on_disconnect
    peers.on_have = manager.on_have
    peers.add_peers([seeder.address for seeder in seeders], source="metrics check")

    collector = MetricsCollector(
        torrent,
        download=manager,
        peers=peers,
        event_bus=bus,
        config=StatsConfig(sample_interval=interval),
    )

    started = time.monotonic()
    try:
        await peers.fill()
        await collector.start()
        await manager.start()
        await manager.wait_until_complete(timeout=_timeout_for(torrent.total_length))
        await manager.flush()
        collector.sample()  # the finished state deserves its own point
        await collector.stop()

        if upload:
            await _seed_back(
                torrent, payload, storage=storage, leechers=leechers, collector=collector, bus=bus
            )
    finally:
        await manager.stop()
        await peers.stop()
        for seeder in seeders:
            await seeder.stop()
        if not collector.running:
            collector.close()
        else:
            await collector.stop()
            collector.close()
    elapsed = time.monotonic() - started

    verified = await _matches(torrent, directory)
    rates = collector.history.series(SERIES_DOWNLOAD_INSTANT)
    progress = collector.history.series(SERIES_PROGRESS)
    final = collector.snapshot()
    check = MetricsCheck(
        name=torrent.name,
        hex_info_hash=torrent.hex_info_hash,
        total_length=torrent.total_length,
        piece_count=torrent.piece_count,
        seeds=len(seeders),
        leechers=leechers if upload else 0,
        downloaded_bytes=collector.download_speed.total,
        uploaded_bytes=collector.upload_speed.total,
        elapsed=elapsed,
        samples_taken=len(progress),
        history_capacity=collector.history.capacity,
        download_average=collector.download_speed.average,
        download_peak=max((value for _stamp, value in rates), default=0.0),
        upload_average=collector.upload_speed.average,
        first_eta=_first_value(collector.history.series(SERIES_ETA)),
        last_eta=_last_value(collector.history.series(SERIES_ETA)),
        pieces_verified=final.pieces_verified,
        wasted_bytes=final.wasted_bytes,
        verified=verified,
        complete=final.complete,
        final=final,
        series={
            name: tuple((sample.timestamp, sample.value) for sample in samples)
            for name, samples in collector.history.as_dict().items()
        },
    )
    await storage.aclose()
    return check


async def _seed_back(
    torrent: Torrent,
    payload: bytes,
    *,
    storage: StorageManager,
    leechers: int,
    collector: MetricsCollector,
    bus: EventBus,
) -> int:
    """Seed the finished download to real leechers. Returns bytes served.

    The collector and the bus are the download's own, so the bytes we send are
    counted by the same meters that counted the bytes we received.
    """
    context = SwarmContext.from_torrent(torrent)
    peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
    have = Bitfield(torrent.piece_count)
    for index in range(torrent.piece_count):
        have.set(index)
    upload = UploadManager(
        torrent,
        storage=storage,
        peers=peers,
        config=UploadConfig(choke_interval=0.2),
        event_bus=bus,
        have=have,
    )
    peers.on_request = upload.on_request
    peers.on_cancel = upload.on_cancel  # type: ignore[assignment]
    peers.on_disconnect = upload.on_disconnect
    listener = PeerListener(peers, host="127.0.0.1", port=0)
    port = await listener.start()
    swarm = [
        MockLeecher(info_hash=torrent.info_hash, piece_length=torrent.piece_length, payload=payload)
        for _ in range(max(1, leechers))
    ]
    try:
        await upload.start()
        for leecher in swarm:
            await leecher.connect("127.0.0.1", port)
            await leecher.interested()
        for leecher in swarm:
            await leecher.wait_for_unchoke(timeout=30.0)
            await leecher.request_piece(0)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and any(not leecher.blocks for leecher in swarm):
            await asyncio.sleep(0.02)
        await upload.flush()
    finally:
        for leecher in swarm:
            await leecher.aclose()
        await upload.stop()
        await listener.stop()
        await peers.stop()
    return upload.stats.bytes_uploaded


def _timeout_for(total_length: int) -> float:
    """Generous but finite: a deadlock must fail the run, not hang it."""
    return max(60.0, total_length / (256 * 1024))


def _first_value(samples: Sequence[tuple[float, float]]) -> float | None:
    return samples[0][1] if samples else None


def _last_value(samples: Sequence[tuple[float, float]]) -> float | None:
    return samples[-1][1] if samples else None


async def _matches(torrent: Torrent, directory: Path) -> bool:
    """Whether every piece on disk hashes to what the torrent promised."""
    from tools.swarm_download import _read_piece

    return all(
        _read_piece(torrent, directory, index) is not None for index in range(torrent.piece_count)
    )


def render(check: MetricsCheck, *, directory: Path) -> None:
    """Print a metrics check report."""
    print(f"Metrics check: {check.name}")
    print(f"  info hash    {check.hex_info_hash}")
    print(f"  size         {_human_bytes(check.total_length)} in {check.piece_count} piece(s)")
    print(f"  directory    {directory}")
    print(f"  swarm        {check.seeds} seeder(s), {check.leechers} leecher(s) seeded back to")
    print()
    print(
        f"  download     {_human_bytes(check.downloaded_bytes)} in {check.elapsed:.2f}s "
        f"({_human_bytes(int(check.download_average))}/s average, "
        f"{_human_bytes(int(check.download_peak))}/s peak)"
    )
    print(
        f"  upload       {_human_bytes(check.uploaded_bytes)} "
        f"({_human_bytes(int(check.upload_average))}/s average)"
    )
    print(f"  samples      {check.samples_taken} taken, history holds {check.history_capacity}")
    eta = "→".join(
        "never" if value is None else f"{value:.1f}s" for value in (check.first_eta, check.last_eta)
    )
    print(f"  eta          {eta} (first → last sample)")
    print(
        f"  pieces       {check.pieces_verified}/{check.piece_count} verified, "
        f"{_human_bytes(check.wasted_bytes)} wasted"
    )
    print()
    if check.successful and check.history_within_bounds:
        print("  RESULT ok: every number above was measured from bytes that moved")
    else:
        print("  RESULT failed: see the lines above for what did not add up")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="metrics_check",
        description="Download and seed a generated torrent while sampling metrics.",
    )
    parser.add_argument(
        "--size", default=DEFAULT_SIZE, help=f"payload size (default: {DEFAULT_SIZE})"
    )
    parser.add_argument("--piece-length", type=int, default=DEFAULT_PIECE_LENGTH)
    parser.add_argument("--seeds", type=int, default=DEFAULT_SEEDS, help="seeders to download from")
    parser.add_argument(
        "--leechers",
        type=int,
        default=DEFAULT_LEECHERS,
        help="peers to seed back to afterwards",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="seconds between samples (default: 0.1)",
    )
    parser.add_argument(
        "--slow-delay",
        type=float,
        default=0.0,
        help="seconds each seeder waits per block (default: 0, as fast as it goes)",
    )
    parser.add_argument("--no-upload", action="store_true", help="download only; do not seed back")
    parser.add_argument("--out", type=Path, default=Path("data/metrics-check"))
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--keep", action="store_true", help="keep the download directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns 0 when every measurement held up."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    payload = generate_payload(parse_size(args.size), seed=DEFAULT_SEED)
    torrent = parse_torrent(
        build_torrent_bytes(payload, name="metrics", piece_length=args.piece_length, file_count=1)
    )
    directory: Path = args.out
    state_directory = args.state_dir or directory.parent / "state"
    if directory.exists():
        shutil.rmtree(directory)

    try:
        check = asyncio.run(
            run_metrics_check(
                torrent,
                payload,
                directory=directory,
                state_directory=state_directory,
                seeds=args.seeds,
                leechers=args.leechers,
                interval=args.interval,
                upload=not args.no_upload,
                slow_delay=args.slow_delay,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a diagnostic tool reports, never crashes
        print(f"metrics check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep and directory.exists():
            shutil.rmtree(directory, ignore_errors=True)

    render(check, directory=directory)
    return 0 if check.successful and check.history_within_bounds else 1


if __name__ == "__main__":
    raise SystemExit(main())
