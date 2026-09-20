"""Probe real peers over the wire: handshake, listen, report (TRD §30).

This is the tool that answers "does our protocol code actually work?" against
the internet rather than against our own mocks. It finds peers through a
tracker (M3), opens a TCP connection to each, performs a real handshake (M4),
optionally says we are interested, and prints exactly what each peer sent back.

Nothing here is simulated: the client names, bitfields and choke states in the
output are what the remote peers actually said.

Usage::

    # discover peers from a torrent's own trackers
    python tools/peer_probe.py debian.torrent --peers 12

    # or probe a peer directly
    python tools/peer_probe.py --host 82.29.94.69 --port 31337 \\
        --info-hash 481b6e3617be4c88f96cb25e47c9d8272130071e

Peers are ordinary home connections: a large share will time out, refuse, or
hang up mid-handshake. That is not a bug in this client, so the report counts
every outcome instead of hiding the failures.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import NetworkConfig
from app.core.peer_id import generate_peer_id
from app.peer.connection import SwarmContext
from app.peer.discovery.peer_manager import PeerManager
from app.peer.errors import PeerError
from app.peer.handshake import outgoing_handshake
from app.peer.messages import Interested, Unchoke
from app.peer.protocol import PeerStream
from app.peer.state import PeerSession
from app.torrent import Torrent, parse_torrent_file
from app.tracker import TrackerManager
from app.tracker.base import PeerAddress

DEFAULT_CONNECT_TIMEOUT: float = 5.0
DEFAULT_READ_TIMEOUT: float = 4.0
DEFAULT_PEER_LIMIT: int = 12
DEFAULT_MESSAGE_LIMIT: int = 6
# Seconds to wait after connecting in swarm mode, so unchokes can arrive.
DEFAULT_SETTLE_SECONDS: float = 2.0


@dataclass(frozen=True, slots=True)
class PeerReport:
    """What one peer did when we talked to it.

    Attributes:
        address: ``host:port`` we connected to.
        outcome: ``connected``, ``timeout``, ``refused``, ``disconnected``,
            ``handshake_failed`` or ``error``.
        client: Client software reported by the peer id, when we got one.
        handshake_ms: Round-trip time for the handshake.
        messages: Messages the peer sent after the handshake, in order.
        pieces_held: Pieces the peer reports having, when we know the count.
        unchoked: Whether the peer unchoked us, or None if we never asked.
        error: Failure detail, when the peer did not complete a handshake.
    """

    address: str
    outcome: str
    client: str | None = None
    handshake_ms: float | None = None
    messages: tuple[str, ...] = ()
    pieces_held: int | None = None
    unchoked: bool | None = None
    error: str | None = None

    @property
    def connected(self) -> bool:
        """Whether a full handshake completed."""
        return self.outcome == "connected"


@dataclass(slots=True)
class ProbeSummary:
    """Aggregate outcome of a probe run."""

    reports: list[PeerReport] = field(default_factory=list)

    @property
    def connected(self) -> list[PeerReport]:
        return [report for report in self.reports if report.connected]

    @property
    def unchoked(self) -> list[PeerReport]:
        return [report for report in self.connected if report.unchoked is True]

    @property
    def clients(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for report in self.connected:
            if report.client:
                counts[report.client] = counts.get(report.client, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


async def probe_peer(
    host: str,
    port: int,
    info_hash: bytes,
    *,
    peer_id: bytes | None = None,
    piece_count: int | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    send_interested: bool = True,
) -> PeerReport:
    """Connect to one peer, handshake, and collect what it says.

    Args:
        host: Peer host.
        port: Peer port.
        info_hash: The torrent we are asking about.
        peer_id: Our peer id; generated when omitted.
        piece_count: Pieces in the torrent, used to read the peer's bitfield.
        connect_timeout: TCP connect deadline.
        read_timeout: Deadline per read after connecting.
        message_limit: How many messages to collect before disconnecting.
        send_interested: Whether to send ``interested`` after the handshake,
            which is what makes a peer decide to unchoke us.

    Returns:
        A report describing exactly what happened.
    """
    address = f"{host}:{port}"
    our_peer_id = peer_id or generate_peer_id()

    try:
        stream = await PeerStream.connect(host, port, timeout=connect_timeout)
    except PeerError as exc:
        outcome = "timeout" if "timed out" in str(exc) else "refused"
        return PeerReport(address=address, outcome=outcome, error=str(exc))

    session: PeerSession | None = None
    try:
        async with stream:
            peer = await stream.perform_handshake(
                outgoing_handshake(info_hash, our_peer_id), timeout=read_timeout
            )
            if piece_count is not None:
                session = PeerSession(piece_count=piece_count)
                session.note_handshake(peer.peer_id, peer.client)

            if send_interested:
                await stream.send(Interested())

            seen: list[str] = []
            unchoked = False
            while len(seen) < message_limit:
                try:
                    message = await stream.read_message(timeout=read_timeout)
                except PeerError:
                    break
                seen.append(str(message))
                if isinstance(message, Unchoke):
                    unchoked = True
                if session is not None:
                    session.apply(message)

        pieces_held = session.bitfield.count if session is not None else None
        return PeerReport(
            address=address,
            outcome="connected",
            client=peer.client,
            handshake_ms=stream.handshake_latency_ms,
            messages=tuple(seen),
            pieces_held=pieces_held,
            unchoked=unchoked if send_interested else None,
        )
    except PeerError as exc:
        return PeerReport(address=address, outcome="handshake_failed", error=str(exc))
    except OSError as exc:  # pragma: no cover - defensive: sockets surprise you
        return PeerReport(address=address, outcome="error", error=str(exc))


async def probe_peers(
    peers: Sequence[tuple[str, int]],
    info_hash: bytes,
    *,
    limit: int = DEFAULT_PEER_LIMIT,
    piece_count: int | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
) -> ProbeSummary:
    """Probe several peers concurrently and summarise the results.

    Concurrency matters: probing twelve peers serially at a five-second
    connect timeout could take a minute; in parallel it takes as long as the
    slowest peer.
    """
    selected = list(peers)[:limit]
    reports = await asyncio.gather(
        *(
            probe_peer(
                host,
                port,
                info_hash,
                piece_count=piece_count,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )
            for host, port in selected
        )
    )
    return ProbeSummary(reports=list(reports))


async def discover_peers(
    torrent_path: Path, *, tracker_url: str | None = None, limit: int = DEFAULT_PEER_LIMIT
) -> tuple[Torrent, list[tuple[str, int]]]:
    """Find peers for a torrent through its trackers.

    Returns:
        ``(torrent, [(host, port), ...])`` — the parsed torrent is handed back
        so swarm mode can build a :class:`SwarmContext` from it.
    """
    torrent = parse_torrent_file(torrent_path)

    from app.tracker.factory import build_tracker

    # An explicit tracker replaces the torrent's own; otherwise the manager
    # derives tiers from the metainfo. The scheme picks the client, so a
    # udp:// tracker works here as well.
    trackers = ((build_tracker(tracker_url),),) if tracker_url is not None else ()
    manager = TrackerManager(torrent, trackers=trackers)
    try:
        outcome = await manager.announce(num_want=limit)
    finally:
        await manager.aclose()

    peers = [(peer.host, peer.port) for peer in outcome.peers]
    return torrent, peers


async def probe_swarm(
    context: SwarmContext,
    peers: Sequence[tuple[str, int]],
    *,
    limit: int = DEFAULT_PEER_LIMIT,
    settle: float = DEFAULT_SETTLE_SECONDS,
    config: NetworkConfig | None = None,
) -> ProbeSummary:
    """Connect to several peers at once, the way the client actually will.

    This uses :class:`PeerManager`, so it exercises the whole M5 path: slot
    caps, concurrent connect attempts, bitfield handling and interest. After
    connecting it says "interested" to everyone — a peer only unchokes a client
    that asks for something — and then waits ``settle`` seconds for replies.

    Args:
        context: The torrent these peers belong to.
        peers: Addresses to try.
        limit: Maximum number of simultaneous connections.
        settle: Seconds to wait after connecting, so unchokes can arrive.
        config: Connection timeouts and limits.

    Returns:
        One report per connection attempt, in address order.
    """
    settings = config or NetworkConfig()
    manager = PeerManager(
        context,
        peer_id=generate_peer_id(),
        config=settings,
        max_connections=limit,
    )
    try:
        manager.add_peers([PeerAddress(host=host, port=port) for host, port in peers])
        await manager.fill()
        await asyncio.gather(
            *(connection.send_interested() for connection in manager.connections),
            return_exceptions=True,
        )
        await asyncio.sleep(settle)

        reports = [
            PeerReport(
                address=f"{connection.address.host}:{connection.address.port}",
                outcome="connected" if connection.connected else "disconnected",
                client=connection.client if connection.connected else None,
                handshake_ms=connection.latency_ms,
                pieces_held=connection.bitfield.count if connection.connected else None,
                unchoked=not connection.choked if connection.connected else None,
                error=None if connection.connected else connection.disconnect_reason,
            )
            for connection in manager.connections
        ]
        # Peers we never reached are reported too: a probe that only shows its
        # successes is a marketing brochure, not a diagnostic.
        reports.extend(
            PeerReport(
                address=f"{candidate.address.host}:{candidate.address.port}",
                outcome="unreachable",
                error=candidate.last_error,
            )
            for candidate in manager.candidates
            if candidate.failures > 0
        )
    finally:
        await manager.stop()
    return ProbeSummary(reports=sorted(reports, key=lambda report: report.address))


def render(summary: ProbeSummary, *, title: str) -> None:
    """Print a probe report."""
    print(title)
    for report in summary.reports:
        if report.connected:
            latency = f"{report.handshake_ms:.0f} ms" if report.handshake_ms else "?"
            pieces = "-" if report.pieces_held is None else f"{report.pieces_held} pieces"
            state = "" if report.unchoked is None else ("unchoked" if report.unchoked else "choked")
            if report.messages:
                messages = " ".join(report.messages)
            elif report.unchoked is not None:
                # Swarm mode reports choke state, not the raw message log.
                messages = ""
            else:
                messages = "(silent)"
            print(
                f"  {report.address:<24} ok   {latency:>8}  {report.client or 'Unknown':<22} "
                f"{pieces:<14} {state:<9} {messages}".rstrip()
            )
        else:
            detail = report.error or report.outcome
            print(f"  {report.address:<24} {report.outcome:<14} {detail}")

    print(
        f"\n  {len(summary.connected)}/{len(summary.reports)} completed a handshake, "
        f"{len(summary.unchoked)} unchoked us"
    )
    for client, count in summary.clients.items():
        print(f"    {count}x {client}")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Returns:
        0 when at least one peer completed a handshake, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description="Handshake with real BitTorrent peers.")
    parser.add_argument("torrent", type=Path, nargs="?", help="torrent to find peers for")
    parser.add_argument("--host", help="probe this peer directly (needs --info-hash)")
    parser.add_argument("--port", type=int, help="peer port, with --host")
    parser.add_argument("--info-hash", help="40-character hex info hash, with --host")
    parser.add_argument("--tracker", help="announce to this tracker instead")
    parser.add_argument(
        "--peers", type=int, default=DEFAULT_PEER_LIMIT, help="how many peers to probe"
    )
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    parser.add_argument(
        "--swarm",
        action="store_true",
        help="connect through the peer manager (M5) instead of probing one by one",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help=f"seconds to wait for unchokes in --swarm mode (default: {DEFAULT_SETTLE_SECONDS})",
    )
    parser.add_argument("--no-interested", action="store_true", help="do not send 'interested'")
    parser.add_argument("--quiet", action="store_true", help="only log errors")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.ERROR if args.quiet else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.host:
        if not args.info_hash:
            parser.error("--host requires --info-hash")
        peers = [(args.host, args.port or 6881)]
        try:
            info_hash = bytes.fromhex(args.info_hash)
        except ValueError:
            parser.error("--info-hash must be 40 hex characters")
        piece_count = None
        title = f"Probing {peers[0][0]}:{peers[0][1]} directly"
    elif args.torrent:
        try:
            torrent, peers = asyncio.run(
                discover_peers(args.torrent, tracker_url=args.tracker, limit=args.peers)
            )
        except Exception as exc:  # noqa: BLE001 - a diagnostic tool reports, never crashes
            print(f"cannot discover peers: {exc}", file=sys.stderr)
            return 1

        if args.swarm:
            context = SwarmContext.from_torrent(torrent)
            summary = asyncio.run(
                probe_swarm(
                    context,
                    peers,
                    limit=args.peers,
                    settle=args.settle,
                    config=NetworkConfig(
                        connection_timeout=args.connect_timeout,
                        handshake_timeout=max(args.read_timeout, 5.0),
                        idle_timeout=max(args.read_timeout * 5, 30.0),
                        max_peers_per_torrent=max(args.peers, 1),
                    ),
                )
            )
            render(
                summary,
                title=f"Swarm of up to {min(len(peers), args.peers)} peer(s) for {torrent.name}",
            )
            return 0 if summary.connected else 1

        info_hash = torrent.info_hash
        piece_count = torrent.piece_count
        probed = min(len(peers), args.peers)
        title = f"Probing {probed} of {len(peers)} peer(s) for {args.torrent.name}"
    else:
        parser.error("give a torrent file, or --host/--info-hash")

    if not peers:
        print("no peers to probe", file=sys.stderr)
        return 1

    summary = asyncio.run(
        probe_peers(
            peers,
            info_hash,
            limit=args.peers,
            piece_count=piece_count,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
        )
    )
    render(summary, title=title)
    return 0 if summary.connected else 1


if __name__ == "__main__":
    raise SystemExit(main())
