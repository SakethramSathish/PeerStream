"""Integration tests for the HTTP tracker client.

These run against a real HTTP server on loopback speaking the real protocol, so
URL encoding, connection handling, timeouts and response parsing are all
exercised end to end.
"""

from __future__ import annotations

import logging

import aiohttp
import pytest
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.core.peer_id import generate_peer_id, user_agent
from app.tracker import AnnounceRequest, HttpTracker, TrackerEvent
from app.tracker.errors import (
    TrackerConnectionError,
    TrackerProtocolError,
    TrackerTimeoutError,
)
from tools.mock_tracker import MockTracker

INFO_HASH = bytes(range(20))
PEER_ID = generate_peer_id(rng=__import__("random").Random(7))


def request(**overrides: object) -> AnnounceRequest:
    """A default announce request with overrides applied."""
    fields = {
        "info_hash": INFO_HASH,
        "peer_id": PEER_ID,
        "port": 6881,
        "uploaded": 0,
        "downloaded": 0,
        "left": 1000,
    }
    fields.update(overrides)  # type: ignore[arg-type]
    return AnnounceRequest(**fields)  # type: ignore[arg-type]


class TestAnnounce:
    async def test_returns_peers_registered_on_the_tracker(self, mock_tracker: MockTracker) -> None:
        mock_tracker.add_peer(INFO_HASH, "10.0.0.5", 6882, left=0)
        mock_tracker.add_peer(INFO_HASH, "10.0.0.6", 6883, left=500)

        async with HttpTracker(mock_tracker.announce_url) as tracker:
            response = await tracker.announce(request())

        addresses = [peer.address for peer in response.peers]
        assert ("10.0.0.5", 6882) in addresses
        assert ("10.0.0.6", 6883) in addresses
        assert response.interval == mock_tracker.interval

    async def test_reports_seeder_and_leecher_counts(self, mock_tracker: MockTracker) -> None:
        mock_tracker.add_peer(INFO_HASH, "10.0.0.5", 6882, left=0)
        mock_tracker.add_peer(INFO_HASH, "10.0.0.6", 6883, left=500)

        async with HttpTracker(mock_tracker.announce_url) as tracker:
            response = await tracker.announce(request())

        # The announcing client is itself a leecher: "incomplete" counts us too.
        assert response.seeders == 1
        assert response.leechers == 2

    async def test_does_not_return_the_requesting_peer(self, mock_tracker: MockTracker) -> None:
        """A tracker that echoes us back would waste a connection slot."""
        async with HttpTracker(mock_tracker.announce_url) as tracker:
            first = await tracker.announce(request(port=6881))
            second = await tracker.announce(request(port=6881))

        assert first.peers == ()
        assert second.peers == ()

    async def test_transmits_progress_and_event(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(mock_tracker.announce_url) as tracker:
            await tracker.announce(
                request(uploaded=10, downloaded=20, left=30, event=TrackerEvent.STARTED)
            )

        sent = mock_tracker.last_request or {}
        assert sent["uploaded"] == "10"
        assert sent["downloaded"] == "20"
        assert sent["left"] == "30"
        assert sent["event"] == "started"
        assert sent["port"] == "6881"

    async def test_periodic_announce_omits_the_event(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(mock_tracker.announce_url) as tracker:
            await tracker.announce(request())

        assert "event" not in (mock_tracker.last_request or {})

    async def test_sends_the_raw_info_hash_bytes(self, mock_tracker: MockTracker) -> None:
        """The mock validates the length, so a broken encoding shows up as a 400."""
        async with HttpTracker(mock_tracker.announce_url) as tracker:
            response = await tracker.announce(request())

        assert isinstance(response, object)
        assert mock_tracker.last_request is not None
        assert len(mock_tracker.swarms[INFO_HASH]) == 1

    async def test_sends_a_user_agent(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(mock_tracker.announce_url) as tracker:
            await tracker.announce(request())

        headers = mock_tracker.last_headers or {}
        assert headers.get("User-Agent") == user_agent()

    async def test_supports_the_dictionary_peer_format(self, mock_tracker: MockTracker) -> None:
        mock_tracker.add_peer(INFO_HASH, "10.0.0.9", 6999, peer_id=b"z" * 20)

        async with HttpTracker(mock_tracker.announce_url) as tracker:
            response = await tracker.announce(request(compact=False))

        assert [peer.address for peer in response.peers] == [("10.0.0.9", 6999)]
        assert response.peers[0].peer_id == b"z" * 20

    async def test_numwant_limits_returned_peers(self, mock_tracker: MockTracker) -> None:
        for index in range(10):
            mock_tracker.add_peer(INFO_HASH, f"10.0.1.{index}", 7000 + index)

        async with HttpTracker(mock_tracker.announce_url) as tracker:
            response = await tracker.announce(request(num_want=3))

        assert len(response.peers) == 3

    async def test_stopped_event_deregisters_the_peer(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(mock_tracker.announce_url) as tracker:
            await tracker.announce(request(event=TrackerEvent.STARTED))
            assert mock_tracker.peers_for(INFO_HASH)
            await tracker.announce(request(event=TrackerEvent.STOPPED))

        assert mock_tracker.peers_for(INFO_HASH) == ()


class TestFailures:
    async def test_tracker_failure_reason(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(f"{mock_tracker.url}/fail") as tracker:
            with pytest.raises(TrackerProtocolError, match="not registered"):
                await tracker.announce(request())

    async def test_http_error_status(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(f"{mock_tracker.url}/error") as tracker:
            with pytest.raises(TrackerConnectionError, match="HTTP 500"):
                await tracker.announce(request())

    async def test_non_bencode_response(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(f"{mock_tracker.url}/garbage") as tracker:
            with pytest.raises(TrackerProtocolError, match="not valid bencode"):
                await tracker.announce(request())

    async def test_malformed_peer_list(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(f"{mock_tracker.url}/malformed-peers") as tracker:
            with pytest.raises(TrackerProtocolError, match="multiple of 6"):
                await tracker.announce(request())

    async def test_oversized_response_is_rejected(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(f"{mock_tracker.url}/big", max_response_bytes=1024) as tracker:
            with pytest.raises(TrackerProtocolError, match="exceeds"):
                await tracker.announce(request())

    async def test_unreachable_tracker(self) -> None:
        # Port 1 is privileged: nothing is listening there.
        async with HttpTracker("http://127.0.0.1:1/announce") as tracker:
            with pytest.raises(TrackerConnectionError, match="unreachable"):
                await tracker.announce(request())

    async def test_timeout(self, tracker_server: object) -> None:

        factory = tracker_server  # type: ignore[assignment]
        async with (
            factory(delay=0.5) as slow,  # type: ignore[operator]
            HttpTracker(slow.announce_url, timeout=0.05) as tracker,
        ):
            with pytest.raises(TrackerTimeoutError, match="timed out"):
                await tracker.announce(request())


class TestScrape:
    async def test_scrape_reports_swarm_counters(self, mock_tracker: MockTracker) -> None:
        mock_tracker.add_peer(INFO_HASH, "10.0.0.5", 6882, left=0)

        async with HttpTracker(mock_tracker.announce_url) as tracker:
            results = await tracker.scrape([INFO_HASH])

        assert results[INFO_HASH].complete == 1
        assert results[INFO_HASH].incomplete == 0

    async def test_scrape_multiple_torrents(self, mock_tracker: MockTracker) -> None:
        other = bytes(range(100, 120))
        mock_tracker.add_peer(INFO_HASH, "10.0.0.5", 6882, left=0)
        mock_tracker.add_peer(other, "10.0.0.6", 6883, left=10)

        async with HttpTracker(mock_tracker.announce_url) as tracker:
            results = await tracker.scrape([INFO_HASH, other])

        assert set(results) == {INFO_HASH, other}

    async def test_scrape_http_error(self, tracker_server: object) -> None:
        async with (
            tracker_server(http_error=500) as failing,  # type: ignore[operator]
            HttpTracker(failing.announce_url) as tracker,
        ):
            with pytest.raises(TrackerConnectionError, match="HTTP 500 for scrape"):
                await tracker.scrape([INFO_HASH])

    async def test_scrape_needs_a_derivable_url(self) -> None:
        async with HttpTracker("http://127.0.0.1:1/other") as tracker:
            with pytest.raises(TrackerProtocolError, match="cannot derive a scrape URL"):
                await tracker.scrape([INFO_HASH])


class TestEvents:
    async def test_publishes_request_and_response(self, mock_tracker: MockTracker) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe_all(lambda event: seen.append(event))

        async with HttpTracker(mock_tracker.announce_url, event_bus=bus) as tracker:
            await tracker.announce(request())
            await bus.drain()

        types = [event.type for event in seen]
        assert EventType.TRACKER_REQUEST in types
        assert EventType.TRACKER_RESPONSE in types
        response_event = next(event for event in seen if event.type is EventType.TRACKER_RESPONSE)
        assert "peers" in response_event.data
        assert "latency_ms" in response_event.data

    async def test_publishes_failure(self, mock_tracker: MockTracker) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe_all(lambda event: seen.append(event))

        async with HttpTracker(f"{mock_tracker.url}/error", event_bus=bus) as tracker:
            with pytest.raises(TrackerConnectionError):
                await tracker.announce(request())
            await bus.drain()

        failures = [event for event in seen if event.type is EventType.TRACKER_FAILED]
        assert failures
        assert failures[0].level == logging.WARNING


class TestLifecycle:
    async def test_closes_an_owned_session(self, mock_tracker: MockTracker) -> None:
        tracker = HttpTracker(mock_tracker.announce_url)
        await tracker.announce(request())
        assert tracker._session is not None and not tracker._session.closed
        await tracker.aclose()
        assert tracker._session is None or tracker._session.closed

    async def test_does_not_close_a_shared_session(self, mock_tracker: MockTracker) -> None:
        async with aiohttp.ClientSession() as session:
            tracker = HttpTracker(mock_tracker.announce_url, session=session)
            await tracker.announce(request())
            await tracker.aclose()
            assert not session.closed

    async def test_aclose_is_idempotent(self, mock_tracker: MockTracker) -> None:
        tracker = HttpTracker(mock_tracker.announce_url)
        await tracker.aclose()
        await tracker.aclose()

    async def test_reuses_a_closed_session(self, mock_tracker: MockTracker) -> None:
        tracker = HttpTracker(mock_tracker.announce_url)
        await tracker.announce(request())
        await tracker.aclose()
        await tracker.announce(request())  # a new session is created lazily
        await tracker.aclose()


class TestMoreScrapeBehaviour:
    async def test_scrape_without_info_hashes(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(mock_tracker.announce_url) as tracker:
            assert await tracker.scrape([]) == {}

    async def test_scrape_timeout(self, tracker_server: object) -> None:
        async with (
            tracker_server(delay=0.5) as slow,  # type: ignore[operator]
            HttpTracker(slow.announce_url, timeout=0.05) as tracker,
        ):
            with pytest.raises(TrackerTimeoutError, match="timed out while scraping"):
                await tracker.scrape([INFO_HASH])

    async def test_scrape_unreachable(self) -> None:
        async with HttpTracker("http://127.0.0.1:1/announce") as tracker:
            with pytest.raises(TrackerConnectionError, match="unreachable"):
                await tracker.scrape([INFO_HASH])


class TestWarningEvent:
    async def test_tracker_warning_is_published(self, tracker_server: object) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe_all(lambda event: seen.append(event))

        async with (
            tracker_server(warning="please slow down") as tracker_server_instance,  # type: ignore[operator]
            HttpTracker(tracker_server_instance.announce_url, event_bus=bus) as tracker,  # type: ignore[operator]
        ):
            response = await tracker.announce(request())
            await bus.drain()

        assert response.warning_message == "please slow down"
        warnings = [event for event in seen if event.type is EventType.TRACKER_WARNING]
        assert warnings
        assert warnings[0].message == "please slow down"
