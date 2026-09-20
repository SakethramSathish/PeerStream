"""Seed a real torrent to real leechers, using the real upload engine.

This is the upload engine's end-to-end proof: it stores a real payload on
disk, opens a listener, lets real leechers dial in over TCP, and checks that
what they walk away with is byte-for-byte the payload the torrent's hashes
describe. It also answers the question a seeder actually cares about: how many
peers got served, how much went out, and who got choked.

Nothing is simulated except that both ends are in this process.

Examples::

    python tools/upload_check.py --size 4MiB --leechers 3 --out /tmp/seed
    python tools/upload_check.py --size 1MiB --slots 1 --leechers 3 --out /tmp/seed
    python tools/upload_check.py --size 2MiB --rate 100k --out /tmp/seed
    python tools/upload_check.py --size 1MiB --hostile --out /tmp/seed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.core.config import NetworkConfig, StorageConfig, UploadConfig
from app.core.event_bus import EventBus
from app.core.peer_id import generate_peer_id
from app.peer.bitfield import Bitfield
from app.peer.connection import PeerConnection, SwarmContext
from app.peer.discovery.listener import PeerListener
from app.peer.discovery.peer_manager import PeerManager
from app.peer.messages import Cancel
from app.storage.manager import StorageManager
from app.torrent import Torrent, parse_torrent
from app.upload.manager import UploadManager
from tests.mocks.mock_leecher import MockLeecher

from tools.make_test_torrent import (
    build_torrent_bytes,
    generate_payload,
    parse_size,
)
from tools.swarm_download import _human_bytes

logger = logging.getLogger(__name__)

DEFAULT_SIZE: str = "4MiB"
DEFAULT_LEECHERS: int = 3
DEFAULT_PIECE_LENGTH: int = 256 * 1024
DEFAULT_SEED: int = 2024
BLOCK = 16 * 1024


@dataclass(frozen=True, slots=True)
class UploadCheck:
    """Everything one run measured."""

    name: str
    hex_info_hash: str
    total_length: int
    piece_count: int
    leechers: int
    blocks_served: int
    bytes_uploaded: int
    requests_received: int
    rejected: dict[str, int]
    blocks_checked: int
    blocks_correct: int
    peers_unchoked: int
    elapsed: float
    hostile_refused: int

    @property
    def bytes_per_second(self) -> float:
        """Measured upload rate over the whole run."""
        if self.elapsed <= 0:
            return 0.0
        return self.bytes_uploaded / self.elapsed

    @property
    def successful(self) -> bool:
        """True when every block a leecher received matched the real payload."""
        return self.blocks_served > 0 and self.blocks_correct == self.blocks_checked


async def run_upload_check(
    torrent: Torrent,
    payload: bytes,
    *,
    directory: Path,
    state_directory: Path,
    leechers: int = DEFAULT_LEECHERS,
    slots: int = 4,
    rate: int = 0,
    hostile: bool = False,
) -> UploadCheck:
    """Store a payload and seed it to a swarm of leechers.

    Args:
        torrent: The torrent to seed; its hashes describe ``payload``.
        payload: The real data.
        directory: Where the download is stored.
        state_directory: Where resume state is written.
        leechers: How many peers to let in.
        slots: How many of them may download at once.
        rate: Upload ceiling in bytes per second (0 = uncapped).
        hostile: Also send requests that must be refused.

    Returns:
        The measured outcome.
    """
    storage = StorageManager(
        torrent,
        directory,
        config=StorageConfig(
            state_directory=state_directory, preallocate_files=True, verify_before_write=True
        ),
    )
    await storage.prepare()
    have = Bitfield(torrent.piece_count)
    for index in range(torrent.piece_count):
        start = torrent.piece_offset(index)
        await storage.write_piece(index, payload[start : start + torrent.piece_size(index)])
        have.set(index)

    context = SwarmContext.from_torrent(torrent)
    peers = PeerManager(context, peer_id=generate_peer_id(), config=NetworkConfig())
    upload = UploadManager(
        torrent,
        storage=storage,
        peers=peers,
        config=UploadConfig(slots=slots, max_upload_speed=rate, choke_interval=0.2),
        event_bus=EventBus(),
        have=have,
    )
    peers.on_request = upload.on_request
    # A cancel carries the same three fields as a request; only the type differs.
    peers.on_cancel = cast("Callable[[PeerConnection, Cancel], None]", upload.on_cancel)
    peers.on_disconnect = upload.on_disconnect

    listener = PeerListener(peers, host="127.0.0.1", port=0)
    port = await listener.start()
    swarm = [
        MockLeecher(info_hash=torrent.info_hash, piece_length=torrent.piece_length, payload=payload)
        for _ in range(max(1, leechers))
    ]

    started = time.monotonic()
    try:
        for leecher in swarm:
            await leecher.connect("127.0.0.1", port)
        await upload.start()
        for leecher in swarm:
            await leecher.interested()

        deadline = time.monotonic() + 30.0
        for leecher in swarm:
            await leecher.wait_for_unchoke(timeout=30.0)
            await leecher.request_piece(0)
        if hostile:
            await _ask_for_things_we_do_not_have(swarm[0], torrent)

        while time.monotonic() < deadline and any(not leecher.blocks for leecher in swarm):
            await asyncio.sleep(0.02)
        await upload.flush()
        await asyncio.sleep(0.05)
    finally:
        await upload.stop()
        await listener.stop()
        await peers.stop()
        for leecher in swarm:
            await leecher.aclose()
    elapsed = time.monotonic() - started

    blocks_checked = sum(len(leecher.blocks) for leecher in swarm)
    blocks_correct = sum(
        1 for leecher in swarm for block in leecher.blocks if _matches(payload, torrent, block)
    )
    stats = upload.stats
    check = UploadCheck(
        name=torrent.name,
        hex_info_hash=torrent.hex_info_hash,
        total_length=torrent.total_length,
        piece_count=torrent.piece_count,
        leechers=len(swarm),
        blocks_served=stats.blocks_served,
        bytes_uploaded=stats.bytes_uploaded,
        requests_received=stats.requests_received,
        rejected=dict(stats.requests_rejected),
        blocks_checked=blocks_checked,
        blocks_correct=blocks_correct,
        peers_unchoked=len(upload.unchoked_peers()),
        elapsed=elapsed,
        hostile_refused=sum(
            count
            for reason, count in stats.requests_rejected.items()
            if reason in {"bad_index", "bad_offset", "bad_length", "missing_piece"}
        ),
    )
    await storage.aclose()
    return check


async def _ask_for_things_we_do_not_have(leecher: MockLeecher, torrent: Torrent) -> None:
    """Send requests a well-behaved peer would never send."""
    from app.peer.messages import Request

    await leecher.send(Request(index=torrent.piece_count + 10, begin=0, length=BLOCK))
    await leecher.send(
        Request(index=0, begin=torrent.piece_size(0) - 8, length=BLOCK)  # runs off the end
    )


def _matches(payload: bytes, torrent: Torrent, block: object) -> bool:
    """Whether one served block is what the torrent says it should be."""
    start = block.index * torrent.piece_length + block.begin  # type: ignore[attr-defined]
    return bool(payload[start : start + len(block.data)] == block.data)  # type: ignore[attr-defined]


def render(check: UploadCheck, *, directory: Path) -> None:
    """Print an upload check report."""
    print(f"Upload check: {check.name}")
    print(f"  info hash    {check.hex_info_hash}")
    print(f"  size         {_human_bytes(check.total_length)} in {check.piece_count} piece(s)")
    print(f"  directory    {directory}")
    print(f"  swarm        {check.leechers} leecher(s), {check.peers_unchoked} unchoked")
    print()
    print(f"  served       {check.blocks_served} block(s), {_human_bytes(check.bytes_uploaded)}")
    print(f"  rate         {_human_bytes(int(check.bytes_per_second))}/s over {check.elapsed:.2f}s")
    refused = sum(check.rejected.values())
    print(f"  requests     {check.requests_received} received, {refused} refused")
    print(
        f"  checked      {check.blocks_correct}/{check.blocks_checked} block(s) match the payload"
    )
    if check.rejected:
        reasons = ", ".join(
            f"{reason} x{count}" for reason, count in sorted(check.rejected.items())
        )
        print(f"  refusals     {reasons}")
    print()
    if check.successful:
        print("  RESULT ok: everything we served was real, and the leechers can prove it")
    else:
        print("  RESULT failed: we served nothing, or served something wrong")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="upload_check",
        description="Seed a generated torrent to real leechers over TCP.",
    )
    parser.add_argument(
        "--size", default=DEFAULT_SIZE, help=f"payload size (default: {DEFAULT_SIZE})"
    )
    parser.add_argument("--piece-length", type=int, default=DEFAULT_PIECE_LENGTH)
    parser.add_argument(
        "--leechers", type=int, default=DEFAULT_LEECHERS, help="how many peers dial in"
    )
    parser.add_argument("--slots", type=int, default=4, help="how many may download at once")
    parser.add_argument("--rate", type=parse_size, default=0, help="upload ceiling, e.g. 100k")
    parser.add_argument(
        "--hostile", action="store_true", help="also send requests that must be refused"
    )
    parser.add_argument("--out", type=Path, default=Path("data/upload-check"))
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--keep", action="store_true", help="keep the download directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns 0 when everything served was correct."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    payload = generate_payload(parse_size(args.size), seed=DEFAULT_SEED)
    torrent = parse_torrent(
        build_torrent_bytes(payload, name="seed me", piece_length=args.piece_length, file_count=1)
    )
    directory: Path = args.out
    state_directory = args.state_dir or directory.parent / "state"
    if directory.exists():
        shutil.rmtree(directory)

    try:
        check = asyncio.run(
            run_upload_check(
                torrent,
                payload,
                directory=directory,
                state_directory=state_directory,
                leechers=args.leechers,
                slots=args.slots,
                rate=args.rate,
                hostile=args.hostile,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a diagnostic tool reports, never crashes
        print(f"upload check failed: {exc}")
        return 1

    render(check, directory=directory)
    if not args.keep:
        shutil.rmtree(directory, ignore_errors=True)
    return 0 if check.successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
