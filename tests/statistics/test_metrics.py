"""Tests for the metrics collector and its snapshot.

The collector is the layer the UI will read from, so these tests are mostly
about honesty: a number is either measured or it is absent. An ETA with no
rate is ``None`` rather than a guess; a subsystem that is not wired up reads
as zero rather than as a number somebody made up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import ClassVar

import pytest
from app.core.config import StatsConfig
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.statistics.metrics import (
    SERIES_DOWNLOAD_RATE,
    SERIES_ETA,
    SERIES_PEERS,
    SERIES_PROGRESS,
    SERIES_UPLOAD_RATE,
    MetricsCollector,
    MetricsSnapshot,
)
from app.torrent import Torrent

from tests.statistics.conftest import (
    FakeDownload,
    FakeDownloadStats,
    FakePeers,
    FakePeerStats,
    FakeSession,
    FakeUpload,
    FakeUploadStats,
    connection,
)


class TestAnIdleCollector:
    def test_a_fresh_snapshot_is_all_zeroes(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)

        snapshot = collector.sample(now=0.0)

        assert snapshot.download.total == 0
        assert snapshot.upload.total == 0
        assert snapshot.progress == 0.0
        assert snapshot.peers_connected == 0

    def test_the_torrent_supplies_the_sizes(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)

        snapshot = collector.sample(now=0.0)

        assert snapshot.total_bytes == sample_torrent.total_length
        assert snapshot.pieces_total == sample_torrent.piece_count
        assert snapshot.remaining_bytes == sample_torrent.total_length

    def test_without_a_rate_there_is_no_eta(self, sample_torrent: Torrent) -> None:
        """The honest answer to 'when will it finish?' is sometimes 'unknown'."""
        collector = MetricsCollector(sample_torrent)

        snapshot = collector.sample(now=0.0)

        assert snapshot.eta_seconds is None
        assert snapshot.share_ratio is None

    def test_nothing_is_wired_means_nothing_is_claimed(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)

        snapshot = collector.sample(now=0.0)

        assert snapshot.state == "waiting"
        assert snapshot.verified_bytes == 0
        assert snapshot.pieces_verified == 0
        assert snapshot.pieces_missing == sample_torrent.piece_count


class TestCountingBytes:
    def test_bytes_can_be_counted_directly(self, sample_torrent: Torrent) -> None:
        """Not every byte arrives as an event; the meters take them either way."""
        collector = MetricsCollector(sample_torrent)

        collector.note_download(16_384, now=1.0)
        collector.note_upload(4_096, now=1.0)

        snapshot = collector.sample(now=1.0)

        assert snapshot.download.total == 16_384
        assert snapshot.upload.total == 4_096

    def test_bytes_arrive_as_events(self, sample_torrent: Torrent) -> None:
        """The engines already announce every block; counting them is enough."""
        bus = EventBus()
        collector = MetricsCollector(sample_torrent, event_bus=bus)

        for _ in range(4):
            bus.emit(
                make_event(
                    EventType.PIECE_BLOCK_RECEIVED,
                    data={"index": 0, "begin": 0, "length": 16_384},
                )
            )

        assert collector.download_speed.total == 65_536

    def test_upload_events_are_counted_on_the_upload_side(self, sample_torrent: Torrent) -> None:
        bus = EventBus()
        collector = MetricsCollector(sample_torrent, event_bus=bus)

        bus.emit(make_event(EventType.PIECE_UPLOADED, data={"length": 16_384}))

        assert collector.upload_speed.total == 16_384
        assert collector.download_speed.total == 0

    def test_an_event_without_a_length_is_not_counted(self, sample_torrent: Torrent) -> None:
        bus = EventBus()
        collector = MetricsCollector(sample_torrent, event_bus=bus)

        bus.emit(make_event(EventType.PIECE_BLOCK_RECEIVED, data={"index": 0}))
        bus.emit(make_event(EventType.PIECE_BLOCK_RECEIVED, data={"length": 0}))
        bus.emit(make_event(EventType.PEER_HANDSHAKE))

        assert collector.download_speed.total == 0

    def test_closing_stops_the_counting(self, sample_torrent: Torrent) -> None:
        bus = EventBus()
        collector = MetricsCollector(sample_torrent, event_bus=bus)

        collector.close()
        bus.emit(make_event(EventType.PIECE_BLOCK_RECEIVED, data={"length": 16_384}))

        assert collector.download_speed.total == 0
        collector.close()  # twice is harmless


class TestReadingProgress:
    def test_progress_comes_from_verified_pieces(self, sample_torrent: Torrent) -> None:
        download = FakeDownload(verified=list(range(sample_torrent.piece_count // 2)))
        collector = MetricsCollector(sample_torrent, download=download)

        snapshot = collector.sample(now=0.0)

        assert snapshot.pieces_verified == sample_torrent.piece_count // 2
        assert 0.0 < snapshot.progress < 1.0
        assert snapshot.verified_bytes + snapshot.remaining_bytes == snapshot.total_bytes

    def test_a_finished_torrent_is_complete_and_has_no_eta(self, sample_torrent: Torrent) -> None:
        download = FakeDownload(verified=list(range(sample_torrent.piece_count)))
        collector = MetricsCollector(sample_torrent, download=download)

        snapshot = collector.sample(now=0.0)

        assert snapshot.complete is True
        assert snapshot.eta_seconds == 0.0
        assert snapshot.progress == 1.0
        assert snapshot.state == "seeding"

    def test_a_seeding_only_setup_reads_the_have_bitfield(self, sample_torrent: Torrent) -> None:
        """No download engine in sight: the upload side still knows the pieces."""
        upload = FakeUpload(pieces=sample_torrent.piece_count)
        for index in range(sample_torrent.piece_count):
            upload.have.set(index)
        collector = MetricsCollector(sample_torrent, upload=upload)

        snapshot = collector.sample(now=0.0)

        assert snapshot.complete is True
        assert snapshot.verified_bytes == sample_torrent.total_length

    def test_the_download_counters_are_passed_through(self, sample_torrent: Torrent) -> None:
        download = FakeDownload(
            stats=FakeDownloadStats(blocks_received=100, blocks_duplicate=7, wasted_bytes=114_688)
        )
        collector = MetricsCollector(sample_torrent, download=download)

        snapshot = collector.sample(now=0.0)

        assert snapshot.blocks_received == 100
        assert snapshot.blocks_duplicate == 7
        assert snapshot.wasted_bytes == 114_688

    def test_a_source_that_is_missing_a_counter_reads_zero(self, sample_torrent: Torrent) -> None:
        class Partial:
            complete = False

            @property
            def verified_pieces(self) -> tuple[int, ...]:
                return ()

        collector = MetricsCollector(sample_torrent, download=Partial())

        assert collector.sample(now=0.0).wasted_bytes == 0


class TestReadingPeers:
    def test_connected_peers_are_the_live_connections(self, sample_torrent: Torrent) -> None:
        peers = FakePeers(
            connections=[
                connection("a:1"),
                connection("b:2", connected=False),  # gone, not yet reaped
                connection("c:3"),
            ]
        )
        collector = MetricsCollector(sample_torrent, peers=peers)

        assert collector.sample(now=0.0).peers_connected == 2

    def test_unchoked_peers_are_the_ones_that_can_serve_us(self, sample_torrent: Torrent) -> None:
        peers = FakePeers(
            connections=[connection("a:1", choked=False), connection("b:2", choked=True)]
        )
        collector = MetricsCollector(sample_torrent, peers=peers)

        assert collector.sample(now=0.0).peers_unchoked == 1

    def test_interesting_peers_are_the_ones_that_want_our_pieces(
        self, sample_torrent: Torrent
    ) -> None:
        peers = FakePeers(
            connections=[
                connection("a:1", interested=True),
                connection("b:2", interested=False),
            ]
        )
        collector = MetricsCollector(sample_torrent, peers=peers)

        assert collector.sample(now=0.0).peers_interesting == 1

    def test_candidates_are_peers_we_know_but_are_not_talking_to(
        self, sample_torrent: Torrent
    ) -> None:
        peers = FakePeers(
            connections=[connection("a:1")],
            candidates=["c:3", "d:4"],
            stats=FakePeerStats(candidates=2),
        )
        collector = MetricsCollector(sample_torrent, peers=peers)

        assert collector.sample(now=0.0).peers_candidates == 2

    def test_a_peer_manager_without_stats_still_counts_candidates(
        self, sample_torrent: Torrent
    ) -> None:
        class Bare:
            connections: ClassVar[list[object]] = []
            candidates: ClassVar[list[str]] = ["a:1", "b:2"]

        collector = MetricsCollector(sample_torrent, peers=Bare())

        assert collector.sample(now=0.0).peers_candidates == 2

    def test_upload_state_is_read_from_the_upload_side(self, sample_torrent: Torrent) -> None:
        upload = FakeUpload(
            pieces=sample_torrent.piece_count,
            stats=FakeUploadStats(queue_depth=5, requests_rejected={"choked": 2, "bad_index": 1}),
        )
        collector = MetricsCollector(sample_torrent, upload=upload)

        snapshot = collector.sample(now=0.0)

        assert snapshot.upload_queue_depth == 5
        assert snapshot.requests_refused == 3


class TestEtaAndShareRatio:
    def test_an_eta_is_the_remaining_bytes_over_a_measured_rate(
        self, sample_torrent: Torrent
    ) -> None:
        collector = MetricsCollector(sample_torrent)
        collector.note_download(500_000, now=0.0)

        snapshot = collector.sample(now=0.0)

        assert snapshot.eta_seconds == pytest.approx(
            sample_torrent.total_length / snapshot.download.instant
        )

    def test_the_eta_uses_the_steady_window_once_it_has_filled(
        self, sample_torrent: Torrent
    ) -> None:
        """Half a second of history cannot fill a five-second window."""
        collector = MetricsCollector(sample_torrent)
        collector.note_download(500_000, now=0.0)

        before = collector.sample(now=0.5)  # half a second of history
        collector.note_download(500_000, now=6.0)
        after = collector.sample(now=6.0)  # six seconds: the window has filled

        assert before.eta_seconds == pytest.approx(
            (sample_torrent.total_length - before.verified_bytes) / before.download.instant
        )
        assert after.eta_seconds == pytest.approx(
            (sample_torrent.total_length - after.verified_bytes) / after.download.short
        )

    def test_the_eta_shrinks_as_the_torrent_fills(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)
        collector.note_download(500_000, now=0.0)
        first = collector.sample(now=0.0)

        download = FakeDownload(verified=list(range(sample_torrent.piece_count // 2)))
        collector._download = download
        collector.note_download(500_000, now=1.0)
        second = collector.sample(now=1.0)

        assert second.eta_seconds is not None and first.eta_seconds is not None
        assert second.eta_seconds < first.eta_seconds

    def test_a_share_ratio_needs_something_downloaded(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)
        collector.note_upload(1_000, now=0.0)

        assert collector.sample(now=0.0).share_ratio is None

        collector.note_download(2_000, now=0.0)

        assert collector.sample(now=0.0).share_ratio == pytest.approx(0.5)

    def test_a_stalled_download_has_no_eta(self, sample_torrent: Torrent) -> None:
        """Bytes stopped a minute ago: the rate is zero, so the ETA is unknown."""
        collector = MetricsCollector(sample_torrent)
        collector.note_download(100_000, now=0.0)

        snapshot = collector.sample(now=120.0)

        assert snapshot.download.short == 0.0
        assert snapshot.eta_seconds is None
        assert snapshot.state == "waiting"


class TestSampling:
    def test_a_sample_records_the_series_the_graphs_draw(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)
        collector.note_download(20_000, now=0.0)

        collector.sample(now=0.0)

        assert collector.history.series(SERIES_DOWNLOAD_RATE) != ()
        assert collector.history.series(SERIES_UPLOAD_RATE) != ()
        assert collector.history.series(SERIES_PEERS) != ()
        assert collector.history.series(SERIES_PROGRESS) != ()

    def test_an_eta_only_enters_the_history_when_there_is_one(
        self, sample_torrent: Torrent
    ) -> None:
        collector = MetricsCollector(sample_torrent)

        collector.sample(now=0.0)
        assert collector.history.series(SERIES_ETA) == ()

        collector.note_download(20_000, now=1.0)
        collector.sample(now=1.0)
        assert collector.history.series(SERIES_ETA) != ()

    def test_reading_a_snapshot_does_not_record_it(self, sample_torrent: Torrent) -> None:
        """Looking at the numbers must not add points to the graph."""
        collector = MetricsCollector(sample_torrent)

        collector.snapshot(now=0.0)
        collector.snapshot(now=0.0)

        assert collector.history.series(SERIES_PROGRESS) == ()

        collector.sample(now=0.0)
        assert len(collector.history.series(SERIES_PROGRESS)) == 1

    def test_the_history_stays_bounded_however_long_it_runs(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(
            sample_torrent, config=StatsConfig(history_samples=5, sample_interval=0.01)
        )

        for stamp in range(100):
            collector.sample(now=float(stamp))

        assert len(collector.history.series(SERIES_PROGRESS)) == 5

    def test_samples_are_announced_on_the_bus(self, sample_torrent: Torrent) -> None:
        bus = EventBus()
        seen: list[dict[str, object]] = []
        bus.subscribe(EventType.STATS_SAMPLE, lambda event: seen.append(event.data))
        collector = MetricsCollector(sample_torrent, event_bus=bus)

        collector.sample(now=0.0)

        assert len(seen) == 1
        assert seen[0]["complete"] is False

    def test_a_snapshot_can_be_asked_not_to_announce_itself(self, sample_torrent: Torrent) -> None:
        bus = EventBus()
        seen: list[object] = []
        bus.subscribe(EventType.STATS_SAMPLE, seen.append)
        collector = MetricsCollector(sample_torrent, event_bus=bus)

        collector.sample(now=0.0, emit=False)

        assert seen == []

    def test_the_last_sample_is_remembered(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)

        assert collector.last is None
        snapshot = collector.sample(now=0.0)

        assert collector.last is snapshot


class TestTheSnapshotShape:
    def test_a_snapshot_becomes_plain_values(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)
        collector.note_download(1_000, now=0.0)

        exported = collector.sample(now=0.0).as_dict()

        assert exported["downloaded_bytes"] == 1_000
        assert exported["pieces_total"] == sample_torrent.piece_count
        assert exported["state"] == "waiting"
        assert isinstance(exported["progress"], float)

    def test_a_snapshot_without_an_eta_exports_none(self, sample_torrent: Torrent) -> None:
        snapshot = MetricsCollector(sample_torrent).sample(now=0.0)

        assert snapshot.as_dict()["eta_seconds"] is None
        assert snapshot.as_dict()["share_ratio"] is None

    def test_a_stalled_torrent_has_a_word_for_it(self, sample_torrent: Torrent) -> None:
        peers = FakePeers(connections=[connection("a:1", choked=True)])

        snapshot = MetricsCollector(sample_torrent, peers=peers).sample(now=0.0)

        assert snapshot.state == "stalled"

    def test_an_active_torrent_says_so(self, sample_torrent: Torrent) -> None:
        peers = FakePeers(connections=[connection("a:1", choked=False)])
        collector = MetricsCollector(sample_torrent, peers=peers)
        collector.note_download(1_000, now=0.0)

        snapshot = collector.sample(now=0.0)

        assert snapshot.state == "downloading"
        assert snapshot.active is True

    def test_waste_is_a_fraction_of_what_arrived(self, sample_torrent: Torrent) -> None:
        download = FakeDownload(
            stats=FakeDownloadStats(blocks_received=90, blocks_duplicate=10, wasted_bytes=10)
        )
        collector = MetricsCollector(sample_torrent, download=download)

        snapshot = collector.sample(now=0.0)

        assert snapshot.waste_ratio == pytest.approx(0.1)


class TestTheSamplingLoop:
    async def test_the_loop_samples_on_its_schedule(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(
            sample_torrent, config=StatsConfig(sample_interval=0.01, history_samples=50)
        )

        await collector.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await collector.stop()

        assert len(collector.history.series(SERIES_PROGRESS)) > 1
        assert collector.running is False

    async def test_stopping_does_not_wait_for_the_interval(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent, config=StatsConfig(sample_interval=30.0))

        await collector.start()
        await asyncio.wait_for(collector.stop(), timeout=1.0)

        assert collector.running is False

    async def test_stopping_when_never_started_is_harmless(self, sample_torrent: Torrent) -> None:
        await MetricsCollector(sample_torrent).stop()

    async def test_starting_twice_is_one_loop(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent, config=StatsConfig(sample_interval=0.01))
        await collector.start()
        try:
            await collector.start()
            assert collector.running is True
        finally:
            await collector.stop()

    async def test_elapsed_grows_with_the_collector(self, sample_torrent: Torrent) -> None:
        stamp = 0.0
        collector = MetricsCollector(sample_torrent, clock=lambda: stamp)
        first = collector.sample(now=stamp)

        stamp = 5.0
        later = collector.sample(now=stamp)

        assert first.elapsed == 0.0
        assert later.elapsed == pytest.approx(5.0)


class TestSnapshotFields:
    def test_a_bare_snapshot_has_defaults(self) -> None:
        snapshot = MetricsSnapshot()

        assert snapshot.total_bytes == 0
        assert snapshot.state == "waiting"
        assert snapshot.as_dict()["state"] == "waiting"

    def test_logging_a_sample_does_not_need_a_bus(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)

        # No bus, no exception: the collector is usable as a plain recorder.
        assert collector.sample(now=0.0, emit=True).timestamp == 0.0

    def test_the_config_in_force_is_visible(self, sample_torrent: Torrent) -> None:
        config = StatsConfig(sample_interval=2.5)

        assert MetricsCollector(sample_torrent, config=config).config is config

    def test_the_torrent_is_visible(self, sample_torrent: Torrent) -> None:
        assert MetricsCollector(sample_torrent).torrent is sample_torrent

    def test_bytes_that_are_not_positive_are_ignored(self, sample_torrent: Torrent) -> None:
        collector = MetricsCollector(sample_torrent)

        collector.note_download(0, now=0.0)
        collector.note_upload(-5, now=0.0)

        snapshot = collector.sample(now=0.0)
        assert snapshot.download.total == 0
        assert snapshot.upload.total == 0


def test_the_module_logs_in_debug_not_errors(sample_torrent: Torrent, caplog) -> None:
    """Sampling is routine; it must not shout."""
    bus = EventBus()
    collector = MetricsCollector(sample_torrent, event_bus=bus, config=StatsConfig())

    with caplog.at_level(logging.DEBUG):
        collector.sample(now=0.0)

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


class TestAwkwardSources:
    """The shapes a real engine hands us are not always the tidy ones."""

    def test_verified_pieces_may_be_a_method(self, sample_torrent: Torrent) -> None:
        class Download:
            complete = False

            def verified_pieces(self) -> tuple[int, ...]:
                return (0, 1)

        collector = MetricsCollector(sample_torrent, download=Download())

        assert collector.sample(now=0.0).pieces_verified == 2

    def test_a_peer_manager_without_candidate_counts(self, sample_torrent: Torrent) -> None:
        class Odd:
            """``candidates`` is a number, not a collection of peers."""

            connections: ClassVar[list[object]] = []
            candidates = 5
            stats = None

        collector = MetricsCollector(sample_torrent, peers=Odd())

        assert collector.sample(now=0.0).peers_candidates == 0

    def test_an_unchoked_list_that_cannot_be_called(self, sample_torrent: Torrent) -> None:
        class Odd:
            """``unchoked_peers`` wants an argument we do not have."""

            connections: ClassVar[list[object]] = [connection("a:1")]

            def unchoked_peers(self, missing: int) -> list[object]:
                return []

        collector = MetricsCollector(sample_torrent, peers=Odd())

        assert collector.sample(now=0.0).peers_unchoked == 0

    def test_a_connection_that_only_has_a_session(self, sample_torrent: Torrent) -> None:
        """No ``choked`` attribute: the session is the one that knows."""

        @dataclass(slots=True)
        class Bare:
            session: FakeSession = field(default_factory=FakeSession)
            connected: bool = True

        class Peers:
            connections: ClassVar[list[object]] = [Bare(session=FakeSession(peer_choking=False))]

        collector = MetricsCollector(sample_torrent, peers=Peers())

        assert collector.sample(now=0.0).peers_unchoked == 1

    def test_no_blocks_received_means_no_waste(self, sample_torrent: Torrent) -> None:
        download = FakeDownload(stats=FakeDownloadStats(wasted_bytes=100))
        collector = MetricsCollector(sample_torrent, download=download)

        assert collector.sample(now=0.0).waste_ratio == 0.0


class TestWhenThingsGoWrong:
    async def test_a_failed_sample_does_not_stop_the_loop(
        self, sample_torrent: Torrent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One unreadable source must not silence every graph."""
        collector = MetricsCollector(
            sample_torrent, config=StatsConfig(sample_interval=0.01, history_samples=50)
        )
        real_sample = collector.sample
        calls: list[int] = []

        def failing(*args: object, **kwargs: object) -> MetricsSnapshot:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("a source was briefly unreadable")
            return real_sample(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(collector, "sample", failing)
        await collector.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await collector.stop()

        assert len(calls) > 1
        assert collector.running is False

    async def test_stopping_wakes_a_loop_that_was_waiting(self, sample_torrent: Torrent) -> None:
        """A long interval must not become a long wait to shut down."""
        collector = MetricsCollector(sample_torrent, config=StatsConfig(sample_interval=30.0))

        await collector.start()
        await asyncio.sleep(0.05)
        await asyncio.wait_for(collector.stop(), timeout=1.0)

        assert collector.running is False


class TestStopping:
    async def test_a_loop_that_was_failing_is_reported_not_raised(
        self, sample_torrent: Torrent
    ) -> None:
        """Shutting down must be the one thing that never throws."""
        collector = MetricsCollector(sample_torrent)

        async def boom() -> None:
            raise ValueError("broken before it began")

        collector._task = asyncio.create_task(boom())
        await collector.stop()

        assert collector.running is False

    async def test_being_cancelled_mid_sample_stops_the_loop(
        self, sample_torrent: Torrent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancellation is not an error to be survived, it is an instruction."""
        collector = MetricsCollector(sample_torrent, config=StatsConfig(sample_interval=0.01))

        def cancelled(*args: object, **kwargs: object) -> MetricsSnapshot:
            raise asyncio.CancelledError

        monkeypatch.setattr(collector, "sample", cancelled)
        await collector.start()
        await asyncio.sleep(0.05)  # let the loop reach a sample
        await asyncio.wait_for(collector.stop(), timeout=1.0)

        assert collector.running is False

    async def test_closing_a_collector_that_never_listened(self, sample_torrent: Torrent) -> None:
        """No bus, nothing to unsubscribe from, nothing to complain about."""
        MetricsCollector(sample_torrent).close()

    def test_the_event_handler_ignores_events_that_carry_no_bytes(
        self, sample_torrent: Torrent
    ) -> None:
        """Defensive: the bus filters by type, but the handler is a callback."""
        collector = MetricsCollector(sample_torrent)

        collector._on_event(make_event(EventType.PEER_HANDSHAKE))

        assert collector.download_speed.total == 0

    def test_a_connection_that_says_whether_it_is_choking(self, sample_torrent: Torrent) -> None:
        class Peers:
            """No ``unchoked_peers`` helper: the connections are asked."""

            connections: ClassVar[list[object]] = [
                connection("a:1", choked=False),
                connection("b:2", choked=True),
            ]

        collector = MetricsCollector(sample_torrent, peers=Peers())

        assert collector.sample(now=0.0).peers_unchoked == 1
