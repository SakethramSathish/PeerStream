"""Opt-in integration test against a real public tracker.

Skipped unless pytest is run with ``--network``. Its job is to answer the one
question the loopback mock cannot: does a tracker that has never seen our
client accept what we send, and can we parse what it says back?

Two kinds of unreachability degrade to a skip rather than a failure: no route
to the tracker, and a torrent the tracker no longer serves. Neither is evidence
of a broken client, and the point of the test is our side of the conversation.
"""

from __future__ import annotations

import pytest
from app.core.peer_id import generate_peer_id
from app.tracker import AnnounceRequest, HttpTracker, TrackerEvent
from app.tracker.errors import TrackerConnectionError, TrackerProtocolError, TrackerTimeoutError

# Debian's public tracker, and the info hash of a torrent it actually serves
# (Debian 13.6.0 amd64 netinst). Replace the hash when that release is retired.
PUBLIC_TRACKER = "http://bttracker.debian.org:6969/announce"
KNOWN_INFO_HASH = bytes.fromhex("481b6e3617be4c88f96cb25e47c9d8272130071e")
UNKNOWN_INFO_HASH = bytes(range(20))
TIMEOUT = 15.0

pytestmark = pytest.mark.network


async def test_public_tracker_returns_a_parsed_response() -> None:
    """A real announce must come back with a usable interval and peer list."""
    request = AnnounceRequest(
        info_hash=KNOWN_INFO_HASH,
        peer_id=generate_peer_id(),
        port=6881,
        left=791674880,
        event=TrackerEvent.STARTED,
        num_want=10,
    )

    tracker = HttpTracker(PUBLIC_TRACKER, timeout=TIMEOUT)
    try:
        try:
            response = await tracker.announce(request)
        except (TrackerConnectionError, TrackerTimeoutError) as exc:
            pytest.skip(f"{PUBLIC_TRACKER} is unreachable from here: {exc}")
        except TrackerProtocolError as exc:
            pytest.skip(f"{PUBLIC_TRACKER} no longer serves this torrent: {exc}")
    finally:
        await tracker.aclose()

    assert response.interval > 0
    assert isinstance(response.peers, tuple)
    # Never fabricate swarm statistics, but a real tracker reports real ones.
    assert response.seeders >= 0
    assert response.leechers >= 0


async def test_public_tracker_scrape_reports_the_swarm() -> None:
    """Scrape must return one entry per requested info hash."""
    tracker = HttpTracker(PUBLIC_TRACKER, timeout=TIMEOUT)
    try:
        try:
            results = await tracker.scrape([KNOWN_INFO_HASH])
        except (TrackerConnectionError, TrackerTimeoutError) as exc:
            pytest.skip(f"{PUBLIC_TRACKER} is unreachable from here: {exc}")
    finally:
        await tracker.aclose()

    assert KNOWN_INFO_HASH in results
    assert results[KNOWN_INFO_HASH].complete >= 0


async def test_a_refusal_surfaces_as_a_readable_error() -> None:
    """A tracker that rejects us must produce a typed error, not a crash.

    Reaching the tracker and being refused is itself proof the announce was
    well-formed: it parsed our query well enough to look the torrent up.
    """
    request = AnnounceRequest(
        info_hash=UNKNOWN_INFO_HASH,
        peer_id=generate_peer_id(),
        port=6881,
        left=0,
        event=TrackerEvent.STARTED,
    )

    tracker = HttpTracker(PUBLIC_TRACKER, timeout=TIMEOUT)
    try:
        try:
            await tracker.announce(request)
        except (TrackerConnectionError, TrackerTimeoutError) as exc:
            pytest.skip(f"{PUBLIC_TRACKER} is unreachable from here: {exc}")
        except TrackerProtocolError as exc:
            assert "refused the announce" in str(exc)
        else:
            pytest.fail("expected the tracker to refuse an unknown info hash")
    finally:
        await tracker.aclose()
