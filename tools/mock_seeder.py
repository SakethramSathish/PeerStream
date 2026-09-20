"""Standalone BitTorrent seeder serving a real payload on a real socket.

This is the other half of the local swarm: a process that behaves like
someone else's client, so the client under development can be tested against
a peer it did not write. It speaks the actual wire protocol — handshake,
bitfield, interested/unchoke, request/piece — over TCP, serves blocks out of a
real payload, and keeps announcing itself to a tracker so a client that
discovers peers the ordinary way finds it.

Nothing about it is simulated. The blocks it serves are the bytes the torrent's
piece hashes were computed from, which is what makes the check at the other end
("does the file hash to the torrent?") mean anything.

Usage::

    python tools/mock_seeder.py --torrent test.torrent --payload /tmp/payload.bin \\
        --port 6901 --tracker http://127.0.0.1:8000/announce

The peer-serving half is :class:`tests.mocks.mock_peer.MockPeer`, the same
seeder the test-suite uses, so this tool and the tests cannot drift apart.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from app.core.peer_id import generate_peer_id
from app.torrent import Torrent, parse_torrent_file
from app.torrent.errors import TorrentError
from app.tracker import AnnounceRequest, Tracker, TrackerEvent
from app.tracker.errors import TrackerError
from app.tracker.factory import build_tracker
from tests.mocks.mock_peer import MockPeer

PROGRAM_NAME: str = "mock-seeder"
# How often to re-announce while seeding. Trackers forget peers that go quiet.
DEFAULT_ANNOUNCE_INTERVAL: float = 300.0

logger = logging.getLogger(PROGRAM_NAME)


def load_payload(path: Path, torrent: Torrent) -> bytes:
    """Read the payload the torrent's hashes were computed from.

    Args:
        path: The payload file (as written by ``tools/make_test_torrent.py``).
        torrent: The torrent, whose total length is what must be served.

    Returns:
        Exactly ``torrent.total_length`` bytes.

    Raises:
        ValueError: If the file is shorter than the torrent.
    """
    data = path.read_bytes()
    if len(data) < torrent.total_length:
        raise ValueError(
            f"payload is {len(data)} bytes but {torrent.name} describes "
            f"{torrent.total_length} — a seeder cannot serve what it does not have"
        )
    return data[: torrent.total_length]


async def announce_loop(
    tracker: Tracker,
    torrent: Torrent,
    *,
    peer_id: bytes,
    port: int,
    interval: float,
    stop: asyncio.Event,
) -> None:
    """Announce now, then periodically, until told to stop.

    A seeder that announces once is a seeder that disappears from the swarm as
    soon as the tracker's peer TTL expires.
    """
    event: TrackerEvent | None = TrackerEvent.STARTED
    while not stop.is_set():
        request = AnnounceRequest(
            info_hash=torrent.info_hash,
            peer_id=peer_id,
            port=port,
            uploaded=0,
            downloaded=torrent.total_length,
            left=0,
            event=event,
        )
        try:
            response = await tracker.announce(request)
            logger.info(
                "announced to %s: interval %ss, %s seeder(s), %s leecher(s)",
                tracker.url,
                response.interval,
                response.seeders,
                response.leechers,
            )
            interval = max(60.0, float(response.interval))
            event = None  # only the first announce of a run is "started"
        except TrackerError as exc:
            logger.warning("announce to %s failed: %s", tracker.url, exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def run_seeder(args: argparse.Namespace) -> int:
    """Serve the payload until interrupted. Returns a process exit code."""
    torrent = parse_torrent_file(args.torrent)
    payload = load_payload(args.payload, torrent)
    peer_id = generate_peer_id()

    seeder = MockPeer(
        payload,
        info_hash=torrent.info_hash,
        piece_length=torrent.piece_length,
        host=args.host,
        port=args.port,
        peer_id=peer_id,
        choke=args.choke,
        serve_requests=not args.silent,
    )
    address = await seeder.start()
    print(f"{PROGRAM_NAME}: serving {torrent.name} ({len(payload):,} bytes)")
    print(f"  info hash   {torrent.hex_info_hash}")
    print(f"  pieces      {torrent.piece_count:,} x {torrent.piece_length:,}")
    print(f"  listening   {address.host}:{address.port}")

    stop = asyncio.Event()
    announcer: asyncio.Task[None] | None = None
    tracker: Tracker | None = None
    if args.tracker:
        tracker = build_tracker(args.tracker, timeout=args.timeout)
        announcer = asyncio.create_task(
            announce_loop(
                tracker,
                torrent,
                peer_id=peer_id,
                port=address.port,
                interval=args.announce_interval,
                stop=stop,
            )
        )

    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    print("  press Ctrl-C to stop")
    try:
        await stop.wait()
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        if announcer is not None:
            stop.set()
            announcer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await announcer
        if tracker is not None:
            await tracker.aclose()
        await seeder.stop()

    print(
        f"\n{PROGRAM_NAME}: served {seeder.requests_served} block(s) to "
        f"{seeder.connection_count} connection(s)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Seed a torrent to a local swarm over the real protocol.",
    )
    parser.add_argument("--torrent", type=Path, required=True, help="path to a .torrent file")
    parser.add_argument(
        "--payload",
        type=Path,
        required=True,
        help="file holding the torrent's bytes (from tools/make_test_torrent.py)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to listen on")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (0 = pick one)")
    parser.add_argument(
        "--tracker",
        help="tracker to announce to (repeat the torrent's own when omitted)",
    )
    parser.add_argument(
        "--announce-interval",
        type=float,
        default=DEFAULT_ANNOUNCE_INTERVAL,
        help=f"seconds between re-announces (default: {DEFAULT_ANNOUNCE_INTERVAL:.0f})",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="tracker timeout in seconds (default: 15)"
    )
    parser.add_argument(
        "--choke", action="store_true", help="answer interested with choke, and serve nothing"
    )
    parser.add_argument(
        "--silent", action="store_true", help="accept requests and never answer them"
    )
    parser.add_argument("--verbose", action="store_true", help="log announces and failures")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    if not args.tracker and args.torrent.exists():
        # Convenience: seed whatever tracker the torrent names, so the common
        # case is one flag fewer.
        try:
            args.tracker = parse_torrent_file(args.torrent).announce
        except Exception:  # noqa: BLE001 - a convenience must not be fatal
            args.tracker = None
    try:
        return asyncio.run(run_seeder(args))
    except (ValueError, OSError, TorrentError) as exc:
        print(f"{PROGRAM_NAME}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
