"""Resolve a magnet link end to end, against the real network, and say what happened.

This is the tool to reach for when the question is "does magnet resolution
actually work?" rather than "do the tests pass?". It does the two searches in
order, printing each one as it goes:

1. **Find peers** — from the link's ``x.pe`` hints, its ``tr`` trackers, and the
   DHT, asked concurrently because they fail independently.
2. **Fetch the metadata** — connect to those peers, ask for the info dictionary
   (BEP 9), and check it against the link's info hash.

Then it prints the torrent the metadata describes. The check at the end is the
only one that matters and the only one that can fail silently elsewhere: the
bytes arrived from a stranger, and they are only worth believing because they
hash to the hash in the link.

Usage::

    python -m tools.magnet_check "magnet:?xt=urn:btih:…"
    python -m tools.magnet_check --no-dht "magnet:?xt=urn:btih:…"
    python -m tools.magnet_check --info-hash 481b6e3617be4c88f96cb25e47c9d8272130071e \\
        --tracker http://bttracker.debian.org:6969/announce

Nothing is written to disk: this tool resolves, it does not download.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Sequence
from typing import Final

from app.core.config import Config
from app.core.peer_id import generate_peer_id
from app.discovery.dht.node import DhtNode
from app.discovery.magnet_resolver import MagnetResolver
from app.peer.errors import MetadataError
from app.torrent import parse_info_hash
from app.torrent.magnet import MagnetUri, magnet_for
from app.torrent.metadata import Torrent
from app.ui.format import human_bytes

PROGRAM_NAME: Final[str] = "tools.magnet_check"
DEFAULT_TIMEOUT: Final[float] = 30.0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Resolve a magnet link: find peers, fetch the metadata, verify it.",
    )
    parser.add_argument("magnet", nargs="?", help="the magnet link to resolve")
    parser.add_argument(
        "--info-hash",
        default=None,
        help="resolve by info hash instead, with --tracker supplying the swarm",
    )
    parser.add_argument(
        "--tracker",
        action="append",
        default=[],
        dest="trackers",
        help="a tracker URL to ask for peers (repeatable)",
    )
    parser.add_argument(
        "--peer",
        action="append",
        default=[],
        dest="peers",
        help="a peer to ask directly, as host:port (repeatable)",
    )
    parser.add_argument(
        "--no-dht", action="store_true", help="do not start the DHT (trackers and hints only)"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="deadline per search, in seconds"
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def _magnet_from(args: argparse.Namespace) -> MagnetUri:
    """Build the magnet from either the link or the info hash plus hints."""
    if args.magnet:
        from app.torrent import parse_magnet

        magnet = parse_magnet(args.magnet)
        if args.trackers:
            magnet = magnet.with_trackers(args.trackers)
        return magnet
    if args.info_hash:
        return magnet_for(
            parse_info_hash(args.info_hash),
            trackers=tuple(args.trackers),
            peers=tuple(_split_peer(peer) for peer in args.peers),
        )
    raise SystemExit("give a magnet link, or --info-hash with --tracker")


def _split_peer(text: str) -> tuple[str, int]:
    """``host:port`` as a tuple, with the error the user can act on."""
    host, separator, port = text.rpartition(":")
    if not separator or not port.isdigit():
        raise SystemExit(f"peers look like host:port, got {text!r}")
    return host, int(port)


async def resolve(args: argparse.Namespace) -> int:
    """Resolve the magnet and print what arrived. Returns a process exit code."""
    magnet = _magnet_from(args)
    started = time.monotonic()

    print(f"{PROGRAM_NAME}: resolving {magnet.name}")
    print(f"  info hash  {magnet.hex_info_hash}")
    print(f"  trackers   {len(magnet.trackers)}")
    print(f"  peers      {len(magnet.peers)}")

    dht: DhtNode | None = None
    if not args.no_dht:
        node = DhtNode(host="0.0.0.0", port=0, bootstrap_nodes=Config().dht.bootstrap_nodes)
        try:
            await node.start()
            dht = node
            print(f"  dht        listening on UDP {node.address[1]}, {node.size} node(s) known")
        except Exception as error:  # noqa: BLE001 - a tool reports, it does not raise
            print(f"  dht        did not start: {error}")
            await node.aclose()

    resolver = MagnetResolver(
        peer_id=generate_peer_id(),
        dht=dht,
        config=Config(),
        timeout=args.timeout,
    )
    try:
        resolution = await resolver.resolve(
            magnet,
            peers=[_split_peer(peer) for peer in args.peers],
        )
    except MetadataError as error:
        print(f"{PROGRAM_NAME}: no metadata: {error}")
        return 1

    torrent: Torrent = resolution.torrent
    print(f"{PROGRAM_NAME}: metadata arrived from {resolution.metadata.address}")
    print(f"  name       {torrent.name}")
    print(f"  size       {human_bytes(float(torrent.total_length))}")
    print(f"  files      {len(torrent.files)}")
    print(f"  pieces     {torrent.piece_count} x {human_bytes(float(torrent.piece_length))}")
    print(f"  private    {'yes' if torrent.private else 'no'}")
    print(f"  sources    {', '.join(resolution.sources) or 'none'}")
    print(f"  peers      {len(resolution.peers)}")
    print(f"  elapsed    {time.monotonic() - started:.2f}s")
    print(
        f"{PROGRAM_NAME}: verified — {resolution.metadata.size} bytes hash to "
        f"{magnet.hex_info_hash}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(resolve(args))
    except KeyboardInterrupt:  # pragma: no cover - depends on delivery timing
        print(f"\n{PROGRAM_NAME}: interrupted")
        return 130
    except Exception as error:  # noqa: BLE001 - a tool reports the failure
        print(f"{PROGRAM_NAME}: failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
