"""Opt-in integration test against real peers on the public internet.

Skipped unless pytest is run with ``--network``. It answers the question the
loopback fixtures cannot: does our handshake and framing survive contact with
clients we did not write?

Home connections are unreliable — peers time out, refuse, or hang up
mid-handshake depending on their NAT and their peer limits — so a run where
nobody answers is a skip, not a failure. When somebody does answer, the
assertions are about *real* data: a measured handshake time, a client name
read from the peer id, and piece counts decoded from the peer's bitfield.
"""

from __future__ import annotations

import pytest
from app.core.peer_id import generate_peer_id
from app.peer.handshake import outgoing_handshake
from app.peer.protocol import PeerStream
from app.tracker import AnnounceRequest, HttpTracker, TrackerEvent
from app.tracker.errors import TrackerConnectionError, TrackerProtocolError, TrackerTimeoutError
from tools.peer_probe import probe_peers

# Debian 13.6.0 amd64 netinst: its tracker is long-lived and its swarm is large.
PUBLIC_TRACKER = "http://bttracker.debian.org:6969/announce"
INFO_HASH = bytes.fromhex("481b6e3617be4c88f96cb25e47c9d8272130071e")
PIECE_COUNT = 3020
PEERS_TO_PROBE = 8

pytestmark = pytest.mark.network


async def _discover(limit: int = PEERS_TO_PROBE) -> list[tuple[str, int]]:
    """Ask the tracker for peers, skipping the test when it is unreachable."""
    request = AnnounceRequest(
        info_hash=INFO_HASH,
        peer_id=generate_peer_id(),
        port=6881,
        left=791674880,
        event=TrackerEvent.STARTED,
        num_want=limit,
    )
    tracker = HttpTracker(PUBLIC_TRACKER, timeout=15.0)
    try:
        response = await tracker.announce(request)
    except (TrackerConnectionError, TrackerTimeoutError, TrackerProtocolError) as exc:
        pytest.skip(f"{PUBLIC_TRACKER} is unreachable from here: {exc}")
    finally:
        await tracker.aclose()
    return [(peer.host, peer.port) for peer in response.peers][:limit]


async def test_real_peers_complete_a_handshake() -> None:
    """Peers we did not write must accept our handshake and answer it."""
    peers = await _discover()
    if not peers:
        pytest.skip("the tracker returned no peers")

    summary = await probe_peers(peers, INFO_HASH, limit=PEERS_TO_PROBE, piece_count=PIECE_COUNT)
    if not summary.connected:
        pytest.skip(f"none of the {len(peers)} peers answered from here")

    for report in summary.connected:
        assert report.handshake_ms is not None and report.handshake_ms >= 0
        assert report.client is not None
        assert report.error is None

    # At least one peer told us about its pieces, and those counts are real.
    assert any(
        report.pieces_held is not None and 0 <= report.pieces_held <= PIECE_COUNT
        for report in summary.connected
    )


async def test_sending_interested_makes_a_peer_unchoke_us() -> None:
    """A peer that unchokes us proves the whole opening exchange worked.

    A peer only unchokes a client that asked for something, so this exercises
    handshake → interested → unchoke against software we did not write.
    """
    peers = await _discover()
    if not peers:
        pytest.skip("the tracker returned no peers")

    summary = await probe_peers(peers, INFO_HASH, limit=PEERS_TO_PROBE, piece_count=PIECE_COUNT)
    if not summary.connected:
        pytest.skip("no peer completed a handshake from here")
    if not summary.unchoked:
        pytest.skip("no peer unchoked us this run (they are under no obligation to)")

    assert summary.unchoked[0].connected


async def test_our_handshake_is_accepted_by_a_peer_we_dial_directly() -> None:
    """One peer, one connection, no tracker: the smallest possible proof."""
    peers = await _discover(limit=4)
    if not peers:
        pytest.skip("the tracker returned no peers")

    answered = False
    for host, port in peers:
        try:
            stream = await PeerStream.connect(host, port, timeout=5.0)
        except Exception:  # noqa: BLE001 - probing: any failure just means "next peer"
            continue
        try:
            async with stream:
                peer = await stream.perform_handshake(
                    outgoing_handshake(INFO_HASH, generate_peer_id()), timeout=5.0
                )
        except Exception:  # noqa: BLE001 - a peer that hangs up is not a failure
            continue
        answered = True
        assert peer.info_hash == INFO_HASH
        break

    if not answered:
        pytest.skip("none of the peers answered from here")
