"""Unit and integration tests for the tracker manager."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest
from app.core.config import TrackerConfig
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.torrent import Torrent
from app.tracker import (
    AnnounceResponse,
    PeerAddress,
    TrackerEvent,
    TrackerManager,
    build_tracker_tiers,
)
from app.tracker.errors import TrackerError
from app.tracker.http_tracker import HttpTracker
from app.tracker.udp_tracker import UdpTracker
from tools.mock_tracker import MockTracker


@pytest.fixture
def config() -> TrackerConfig:
    return TrackerConfig(min_announce_interval=60, max_announce_interval=1800)


def manager_for(
    torrent: Torrent,
    *,
    config: TrackerConfig | None = None,
    event_bus: EventBus | None = None,
    port: int = 6881,
) -> TrackerManager:
    return TrackerManager(torrent, config=config or TrackerConfig(), port=port, event_bus=event_bus)


class TestAnnouncing:
    async def test_announces_and_returns_peers(
        self, mock_tracker: MockTracker, torrent_factory: object, config: TrackerConfig
    ) -> None:
        factory = torrent_factory  # type: ignore[assignment]
        torrent = factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.5", 6882, left=0)

        manager = manager_for(torrent, config=config)
        try:
            outcome = await manager.announce(event=TrackerEvent.STARTED)
        finally:
            await manager.aclose()

        assert ("10.0.0.5", 6882) in [peer.address for peer in outcome.peers]
        assert outcome.new_peers == outcome.peers
        assert outcome.response.seeders == 1

    async def test_falls_back_to_the_next_tier(
        self, tracker_server: object, torrent_factory: object, config: TrackerConfig
    ) -> None:
        factory = torrent_factory  # type: ignore[assignment]
        async with tracker_server(interval=30) as working:  # type: ignore[operator]
            torrent = factory(  # type: ignore[operator]
                announce="http://127.0.0.1:1/announce",
                announce_list=[["http://127.0.0.1:1/announce"], [working.announce_url]],
            )
            mock_tracker: MockTracker = working
            mock_tracker.add_peer(torrent.info_hash, "10.0.0.7", 6884)

            manager = manager_for(torrent, config=config)
            try:
                outcome = await manager.announce()
            finally:
                await manager.aclose()

        assert outcome.tracker.url == working.announce_url
        assert [peer.address for peer in outcome.peers] == [("10.0.0.7", 6884)]

        first_status = manager.status_for("http://127.0.0.1:1/announce")
        assert first_status is not None
        assert first_status.consecutive_failures == 1

    async def test_raises_when_every_tracker_fails(
        self, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce="http://127.0.0.1:1/announce")  # type: ignore[operator]
        manager = manager_for(torrent, config=config)
        try:
            with pytest.raises(TrackerError, match="all trackers failed"):
                await manager.announce()
        finally:
            await manager.aclose()

    async def test_raises_when_there_are_no_trackers(
        self, sample_torrent: Torrent, config: TrackerConfig
    ) -> None:
        torrent = replace(sample_torrent, announce=None, announce_list=())
        manager = manager_for(torrent, config=config)
        try:
            with pytest.raises(TrackerError, match="no usable trackers"):
                await manager.announce()
        finally:
            await manager.aclose()

    async def test_peers_are_only_new_once(
        self, mock_tracker: MockTracker, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.5", 6882)

        manager = manager_for(torrent, config=config)
        try:
            first = await manager.announce()
            second = await manager.announce()
        finally:
            await manager.aclose()

        assert first.new_peers
        assert second.new_peers == ()

    async def test_left_is_derived_from_progress(
        self, mock_tracker: MockTracker, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        manager = manager_for(torrent, config=config)
        try:
            await manager.announce(downloaded=1024)
        finally:
            await manager.aclose()

        sent = mock_tracker.last_request or {}
        assert int(sent["downloaded"]) == 1024
        assert int(sent["left"]) == torrent.total_length - 1024

    async def test_uses_our_peer_id_and_port(
        self, mock_tracker: MockTracker, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        manager = manager_for(torrent, config=config, port=51413)
        try:
            await manager.announce()
        finally:
            await manager.aclose()

        sent = mock_tracker.last_request or {}
        assert sent["port"] == "51413"
        assert sent["peer_id"]


class TestScheduling:
    def test_interval_is_clamped_to_the_minimum(self, config: TrackerConfig) -> None:
        manager = manager_for(_bare_torrent(), config=config)
        assert manager.next_delay(AnnounceResponse(interval=1)) == 60

    def test_interval_is_clamped_to_the_maximum(self, config: TrackerConfig) -> None:
        manager = manager_for(_bare_torrent(), config=config)
        assert manager.next_delay(AnnounceResponse(interval=99_999)) == 1800

    def test_multiplier_is_applied(self) -> None:
        manager = manager_for(
            _bare_torrent(),
            config=TrackerConfig(
                min_announce_interval=60,
                max_announce_interval=1800,
                announce_interval_multiplier=2.0,
            ),
        )
        assert manager.next_delay(AnnounceResponse(interval=100)) == 200

    def test_min_interval_wins_over_interval(self, config: TrackerConfig) -> None:
        manager = manager_for(_bare_torrent(), config=config)
        assert manager.next_delay(AnnounceResponse(interval=10, min_interval=600)) == 600

    def test_backoff_grows_and_is_capped(self) -> None:
        from app.torrent import parse_torrent

        torrent = parse_torrent(_torrent_bytes())
        manager = TrackerManager(
            torrent,
            trackers=((HttpTracker("http://127.0.0.1:1/announce"),),),
            config=TrackerConfig(min_announce_interval=60, max_announce_interval=600),
        )
        url = "http://127.0.0.1:1/announce"
        assert manager.backoff_delay(url) == 60
        manager.status_for(url).record_failure("one")
        assert manager.backoff_delay(url) == 120
        manager.status_for(url).record_failure("two")
        assert manager.backoff_delay(url) == 240
        for _ in range(10):
            manager.status_for(url).record_failure("more")
        assert manager.backoff_delay(url) == 600

    async def test_periodic_loop_announces_repeatedly(
        self, tracker_server: object, torrent_factory: object
    ) -> None:
        async with tracker_server(interval=1) as server:  # type: ignore[operator]
            torrent = torrent_factory(announce=server.announce_url)  # type: ignore[operator]
            manager = TrackerManager(
                torrent,
                config=TrackerConfig(min_announce_interval=1, max_announce_interval=1800),
            )
            try:
                task, stop = manager.start_periodic(lambda: {"downloaded": 0, "uploaded": 0})
                await asyncio.sleep(0.15)
                stop.set()
                await manager.stop_periodic()
            finally:
                await manager.aclose()

        assert server.announce_count >= 1
        assert task.done()

    async def test_a_subscriber_that_raises_cannot_break_announcing(
        self,
        mock_tracker: MockTracker,
        torrent_factory: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The peer callback is a convenience; a bug in it is not our problem."""
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        manager = TrackerManager(torrent, config=TrackerConfig(min_announce_interval=3600))
        seen: list[int] = []

        def explode(outcome: object) -> None:
            raise RuntimeError("subscriber is broken")

        with caplog.at_level(logging.WARNING):
            periodic, _stop = manager.start_periodic(
                lambda: {"downloaded": 0, "uploaded": 0}, on_peers=explode
            )
            await asyncio.sleep(0.05)
            await manager.stop_periodic()
            await manager.aclose()

        assert periodic.cancelled() or periodic.done()
        assert "peer callback failed" in caplog.text
        assert seen == []

    async def test_the_periodic_callback_hands_peers_back(
        self,
        mock_tracker: MockTracker,
        torrent_factory: object,
    ) -> None:
        """Re-announcing is how we hear about peers that joined later."""
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        mock_tracker.add_peer(torrent.info_hash, "10.9.9.9", 6881)
        outcomes: list[object] = []
        manager = TrackerManager(torrent, config=TrackerConfig(min_announce_interval=3600))

        task, _stop = manager.start_periodic(
            lambda: {"downloaded": 0, "uploaded": 0}, on_peers=outcomes.append
        )
        assert task is not None
        await asyncio.sleep(0.05)
        await manager.stop_periodic()
        await manager.aclose()

        assert outcomes, "the callback was never given an outcome"
        assert ("10.9.9.9", 6881) in {peer.address for peer in outcomes[0].peers}  # type: ignore[attr-defined]

    async def test_periodic_loop_stops_promptly(
        self, mock_tracker: MockTracker, torrent_factory: object
    ) -> None:
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        manager = TrackerManager(torrent, config=TrackerConfig(min_announce_interval=3600))
        try:
            task, _stop = manager.start_periodic(lambda: {})
            await asyncio.sleep(0.05)
            await manager.stop_periodic()
        finally:
            await manager.aclose()

        assert task.cancelled() or task.done()


class TestTrackerTiers:
    def test_builds_tiers_from_announce_list(self, torrent_factory: object) -> None:
        torrent = torrent_factory(  # type: ignore[operator]
            announce="http://a.example/announce",
            announce_list=[["http://b.example/announce"], ["http://c.example/announce"]],
        )
        tiers = build_tracker_tiers(torrent)

        assert [tracker.url for tier in tiers for tracker in tier] == [
            "http://a.example/announce",
            "http://b.example/announce",
            "http://c.example/announce",
        ]

    def test_primary_announce_is_not_duplicated(self, torrent_factory: object) -> None:
        torrent = torrent_factory(  # type: ignore[operator]
            announce="http://a.example/announce",
            announce_list=[["http://a.example/announce"], ["http://c.example/announce"]],
        )
        urls = [tracker.url for tier in build_tracker_tiers(torrent) for tracker in tier]
        assert urls.count("http://a.example/announce") == 1

    def test_builds_a_udp_tracker_when_the_scheme_says_udp(self, torrent_factory: object) -> None:
        # UDP stopped being a reason to skip a tracker in M14.
        torrent = replace(
            torrent_factory(announce="http://a.example/announce"),  # type: ignore[operator]
            announce_list=[["udp://tracker.example:6969/announce"]],
        )
        tiers = build_tracker_tiers(torrent)
        trackers = [tracker for tier in tiers for tracker in tier]
        assert any(isinstance(tracker, UdpTracker) for tracker in trackers)
        assert next(t for t in trackers if isinstance(t, UdpTracker)).port == 6969

    def test_skips_unsupported_schemes(
        self, torrent_factory: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        torrent = replace(
            torrent_factory(announce="http://a.example/announce"),  # type: ignore[operator]
            announce_list=[["wss://tracker.example/announce"]],
        )
        with caplog.at_level(logging.WARNING):
            tiers = build_tracker_tiers(torrent)

        urls = [tracker.url for tier in tiers for tracker in tier]
        assert "wss://tracker.example/announce" not in urls
        assert "skipping tracker" in caplog.text

    def test_torrent_without_trackers_has_no_tiers(self, sample_torrent: Torrent) -> None:
        assert build_tracker_tiers(replace(sample_torrent, announce=None, announce_list=())) == ()


class TestStatus:
    async def test_status_reflects_success(
        self, mock_tracker: MockTracker, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.5", 6882, left=0)
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.6", 6883, left=99)

        manager = manager_for(torrent, config=config)
        try:
            await manager.announce()
        finally:
            await manager.aclose()

        status = manager.status_for(mock_tracker.announce_url)
        assert status is not None
        assert status.consecutive_failures == 0
        # We announce as a leecher ourselves, so "incomplete" counts both.
        assert status.seeders == 1
        assert status.leechers == 2
        assert status.peers_returned == 2
        assert status.latency_ms is not None
        assert status.next_announce_at is not None

    async def test_emits_failure_event_when_all_trackers_fail(
        self, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce="http://127.0.0.1:1/announce")  # type: ignore[operator]
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(EventType.TRACKER_FAILED, lambda event: seen.append(event))

        manager = manager_for(torrent, config=config, event_bus=bus)
        try:
            with pytest.raises(TrackerError):
                await manager.announce()
            await bus.drain()
        finally:
            await manager.aclose()

        assert seen
        assert seen[-1].level == logging.ERROR

    async def test_seeders_and_leechers_aggregate(
        self, mock_tracker: MockTracker, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.5", 6882, left=0)
        manager = manager_for(torrent, config=config)
        try:
            await manager.announce()
            assert manager.seeders == 1
        finally:
            await manager.aclose()


def _bare_torrent() -> Torrent:
    from app.torrent import parse_torrent

    return parse_torrent(_torrent_bytes())


def _torrent_bytes() -> bytes:
    from tools.make_test_torrent import build_torrent_bytes, generate_payload

    return build_torrent_bytes(generate_payload(1024, seed=3), name="tiny.bin", announce=None)


class TestIntrospection:
    async def test_statuses_cover_every_tracker(
        self, mock_tracker: MockTracker, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        manager = manager_for(torrent, config=config)
        try:
            assert [status.url for status in manager.statuses] == [mock_tracker.announce_url]
            assert manager.status_for("http://nope/announce") is None
        finally:
            await manager.aclose()

    async def test_known_peers_and_leechers(
        self, mock_tracker: MockTracker, torrent_factory: object, config: TrackerConfig
    ) -> None:
        torrent = torrent_factory(announce=mock_tracker.announce_url)  # type: ignore[operator]
        mock_tracker.add_peer(torrent.info_hash, "10.0.0.5", 6882, left=99)
        manager = manager_for(torrent, config=config)
        try:
            await manager.announce()
            assert manager.known_peers == (PeerAddress(host="10.0.0.5", port=6882),)
            assert manager.leechers == 2  # the peer plus ourselves
        finally:
            await manager.aclose()


class TestPeriodicFailure:
    async def test_survives_a_dead_tracker(self, torrent_factory: object) -> None:
        torrent = torrent_factory(announce="http://127.0.0.1:1/announce")  # type: ignore[operator]
        manager = TrackerManager(
            torrent, config=TrackerConfig(min_announce_interval=1, max_announce_interval=1800)
        )
        try:
            _task, stop = manager.start_periodic(lambda: {})
            await asyncio.sleep(0.05)
            stop.set()
            await manager.stop_periodic()
        finally:
            await manager.aclose()

        status = manager.status_for("http://127.0.0.1:1/announce")
        assert status is not None
        assert status.consecutive_failures >= 1

    async def test_a_zero_delay_does_not_sleep(self) -> None:
        stop = asyncio.Event()
        await TrackerManager._wait(0.0, stop)  # returns immediately
