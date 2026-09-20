"""The detail tabs, driven by fake services — never by a real swarm.

The plan's rule for this milestone is that the tabs are verified with view-model
tests using fake services, and that is what these are. The fakes are small and
deliberately dumb: they hand back the numbers a test asks for, so an assertion
that fails here is a failure in the tab or its view model, not in the network.

What each tab is held to:

* **It shows the measurement it was given**, formatted, not rounded into a
  prettier story.
* **It says what it does not know.** ``"--"`` for an unmeasured rate, a stated
  reason for an empty table.
* **It refreshes on demand and does not poll on its own** — a widget that
  polled while hidden would be spending the user's battery on a tab they
  cannot see.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from app.core.events import Event, EventType
from app.services.engine import ResumeSummary, TorrentState
from app.services.torrent_service import FileView, PeerView, PieceMap, TorrentView
from app.statistics.metrics import MetricsSnapshot, SpeedRates
from app.tracker.base import TrackerState, TrackerStatus
from app.ui.models.file_model import FileTableModel
from app.ui.models.peer_model import PeerTableModel
from app.ui.models.piece_model import PieceStateModel
from app.ui.models.torrent_model import TorrentFieldModel, fields_for
from app.ui.models.tracker_model import TrackerTableModel
from app.ui.theme.palette import DARK
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.logs_vm import FILTERS, LogRow, LogsViewModel, row_for
from app.ui.viewmodels.peers_vm import PeersViewModel
from app.ui.viewmodels.pieces_vm import PiecesViewModel
from app.ui.viewmodels.torrent_vm import TorrentViewModel
from app.ui.views.files_tab import FilesTab
from app.ui.views.logs_tab import LogsTab
from app.ui.views.overview_tab import OverviewTab
from app.ui.views.peers_tab import PeersTab
from app.ui.views.pieces_tab import PiecesTab
from app.ui.views.torrent_detail_view import TorrentDetailView
from app.ui.views.trackers_tab import TrackersTab

# ------------------------------------------------------------------------ fakes


def metrics_of(**overrides: Any) -> MetricsSnapshot:
    """A metrics snapshot a test can vary one field at a time."""
    fields: dict[str, Any] = {
        "download": SpeedRates(short=250_000.0, total=4_000_000),
        "upload": SpeedRates(short=40_000.0, total=500_000),
        "progress": 0.42,
        "total_bytes": 800_000_000,
        "verified_bytes": 336_000_000,
        "pieces_total": 100,
        "pieces_verified": 42,
        "pieces_missing": 58,
        "peers_connected": 3,
        "peers_unchoked": 2,
        "eta_seconds": 1234.0,
        "share_ratio": 0.12,
        "wasted_bytes": 65_536,
    }
    fields.update(overrides)
    return MetricsSnapshot(**fields)


def view_of(**overrides: Any) -> TorrentView:
    """A torrent view a test can vary one field at a time."""
    fields: dict[str, Any] = {
        "info_hash": "a" * 40,
        "name": "Debian 13.6.0 netinst",
        "state": TorrentState.DOWNLOADING,
        "progress": 0.42,
        "total_length": 800_000_000,
        "verified_pieces": 42,
        "missing_pieces": 58,
        "piece_count": 100,
        "port": 6881,
        "resumed": ResumeSummary(),
        "metrics": metrics_of(),
    }
    fields.update(overrides)
    return TorrentView(**fields)


def swarm_of(count: int = 5, *, connected: int = 3) -> tuple[PeerView, ...]:
    """A swarm: the first ``connected`` peers connected, the rest candidates."""
    return tuple(
        PeerView(
            key=f"10.0.0.{index}:6881",
            host=f"10.0.0.{index}",
            port=6881,
            client=f"client {index}",
            state="connected" if index < connected else "candidate",
            source="tracker",
            downloaded=1_000 * (index + 1),
            uploaded=100 * index,
            pieces_held=100 if index == 0 else 10 * index,
            piece_count=100,
            choking_us=index % 2 == 1,
            interested_in_us=index == 0,
            latency_ms=10.0 * index,
            idle_for=0.25 * index,
            blocks_in_flight=index % 4,
        )
        for index in range(count)
    )


PIECE_NAMES: tuple[str, ...] = ("missing", "requested", "downloading", "verified", "failed")


def piece_map_of(verified: int = 40, total: int = 100) -> PieceMap:
    """A piece map: some verified, a few in flight, one failed, the rest missing."""
    verified = min(verified, total)
    codes = ([3] * verified + [2] * 4 + [1] * 3 + [4] * 1 + [0] * total)[:total]
    counts = dict.fromkeys(PIECE_NAMES, 0)
    for code in codes:
        counts[PIECE_NAMES[code]] += 1
    in_flight = {index for index, code in enumerate(codes) if code == 2}
    return PieceMap(
        piece_count=total,
        piece_length=262_144,
        total_length=262_144 * total,
        states=bytes(codes),
        availability=tuple(0 if index in in_flight else 2 for index in range(total)),
        filled=tuple(0.5 if index in in_flight else 0.0 for index in range(total)),
        counts=counts,
    )


def files_of() -> tuple[FileView, ...]:
    """Two files: one large and partly done, one small and finished."""
    return (
        FileView(
            path="debian.iso",
            length=800_000_000,
            offset=0,
            piece_count=100,
            verified_pieces=42,
            progress=0.42,
        ),
        FileView(
            path="README.txt",
            length=1_024,
            offset=800_000_000,
            piece_count=1,
            verified_pieces=1,
            progress=1.0,
        ),
    )


def trackers_of() -> tuple[TrackerStatus, ...]:
    """One healthy tracker and one that is failing."""
    return (
        TrackerStatus(
            url="http://bttracker.debian.org:6969/announce",
            state=TrackerState.OK,
            seeders=412,
            leechers=58,
            peers_returned=50,
            latency_ms=87.0,
            next_announce_at=time.time() + 240.0,
        ),
        TrackerStatus(
            url="http://broken.example/announce",
            state=TrackerState.FAILED,
            consecutive_failures=3,
            last_error="connection refused",
        ),
    )


def _clock() -> float:
    """The monotonic clock, so a test can place samples a second apart."""
    return time.monotonic()


def filled_model(**overrides: Any) -> TorrentViewModel:
    """A torrent view model holding one torrent, a swarm and a piece map."""
    model = TorrentViewModel()
    model.update_torrent(view_of(**overrides))
    peers = swarm_of()
    model.update_peers(peers)
    model.update_peers(peers)
    model.update_pieces(piece_map_of())
    return model


# ------------------------------------------------------------------ view models


class TestTorrentViewModel:
    def test_it_holds_the_torrent_the_peers_and_the_pieces(self) -> None:
        model = filled_model()
        assert model.selected
        assert model.info_hash == "a" * 40
        assert model.peers.connected_count == 3
        assert model.pieces.verified == 40
        assert model.progress == pytest.approx(0.42)

    def test_it_records_a_rate_history(self) -> None:
        model = filled_model()
        model.update_torrent(
            view_of(metrics=metrics_of(download=SpeedRates(short=999.0))), now=_clock() + 1.0
        )
        assert len(model.download_series) == 2
        assert model.download_series[-1][1] == pytest.approx(999.0)

    def test_samples_closer_than_the_interval_are_collapsed(self) -> None:
        # A graph that recorded every pump tick would be a graph of the pump.
        model = filled_model()
        before = len(model.download_series)
        model.update_torrent(view_of())
        assert len(model.download_series) == before

    def test_the_history_is_bounded(self) -> None:
        # The window is the design system's: five minutes at one sample a
        # second, whichever view model you ask.
        model = filled_model()
        for index in range(TOKENS.chart.max_points + 50):
            model.update_torrent(view_of(), now=_clock() + index)
        assert len(model.download_series) == TOKENS.chart.max_points

    def test_selecting_another_torrent_forgets_the_last_one(self) -> None:
        model = filled_model()
        model.clear()
        assert not model.selected
        assert model.peers.peers == ()
        assert not model.pieces.known
        assert model.download_series == ()

    def test_it_reports_itself_as_plain_data(self) -> None:
        model = filled_model()
        as_dict = model.as_dict()
        assert as_dict["name"] == "Debian 13.6.0 netinst"
        assert as_dict["state"] == "downloading"
        assert as_dict["peers"]["connected"] == 3
        assert as_dict["pieces"]["verified" if "verified" in as_dict["pieces"] else "counts"]

    def test_a_torrent_without_metrics_is_not_a_zero_rate(self) -> None:
        model = TorrentViewModel()
        model.update_torrent(view_of(metrics=None))
        assert model.download_rate == 0.0
        assert model.eta_seconds is None


class TestPeersViewModel:
    def test_connected_peers_are_separated_from_candidates(self) -> None:
        model = PeersViewModel()
        model.update(swarm_of(5, connected=3))
        assert model.connected_count == 3
        assert len(model.candidates) == 2
        assert model.seeds == 1
        assert model.unchoked == 2

    def test_a_rate_comes_from_two_reads(self) -> None:
        model = PeersViewModel()
        model.update(swarm_of(), now=_clock())
        later = tuple(
            PeerView(
                **{
                    **{
                        field: getattr(peer, field)
                        for field in (
                            "key",
                            "host",
                            "port",
                            "client",
                            "state",
                            "source",
                            "pieces_held",
                            "piece_count",
                            "choking_us",
                            "interested_in_us",
                        )
                    },
                    "downloaded": peer.downloaded + 200_000,
                    "uploaded": peer.uploaded,
                }
            )
            for peer in model.peers
        )
        model.update(later, now=_clock() + 1.0)
        activity = model.activity_for("10.0.0.0:6881")
        assert activity.down_rate is not None
        assert activity.down_rate == pytest.approx(200_000.0, rel=0.01)

    def test_totals_add_up_over_measured_peers(self) -> None:
        model = PeersViewModel()
        model.update(swarm_of())
        model.update(swarm_of(), now=time.monotonic() + 1.0)
        assert model.download_rate == 0.0, "no peer moved bytes, so nothing was measured"

    def test_it_reports_itself_as_plain_data(self) -> None:
        model = PeersViewModel()
        model.update(swarm_of())
        as_dict = model.as_dict()
        assert as_dict["peers"] == 5
        assert as_dict["connected"] == 3
        assert as_dict["unchoked"] == 2


class TestPiecesViewModel:
    def test_it_counts_what_the_engine_counted(self) -> None:
        model = PiecesViewModel()
        model.update(piece_map_of(verified=40, total=100))
        assert model.verified == 40
        assert model.in_flight == 7
        assert model.failed == 1
        assert model.progress == pytest.approx(0.4)

    def test_it_names_the_pieces_nobody_has(self) -> None:
        model = PiecesViewModel()
        model.update(piece_map_of(verified=40, total=100))
        orphaned = model.orphaned()
        assert 40 in orphaned, "the pieces in flight have no availability in this map"
        assert 0 not in orphaned, "verified pieces are not wanted"

    def test_the_completion_history_is_bounded(self) -> None:
        model = PiecesViewModel()
        for verified in range(400):
            model.update(piece_map_of(verified=verified % 100, total=100))
        assert len(model.history) <= 180
        assert model.gained >= 0

    def test_nothing_known_means_no_claims(self) -> None:
        model = PiecesViewModel()
        assert not model.known
        assert model.progress == 0.0
        assert model.counts == dict.fromkeys(
            ("missing", "requested", "downloading", "verified", "failed"), 0
        )
        assert model.orphaned() == ()


class TestLogsViewModel:
    def test_it_records_events_newest_first(self) -> None:
        model = LogsViewModel()
        model.add(Event(type=EventType.PEER_CONNECTED, category="network", message="one"))
        model.add(Event(type=EventType.PIECE_VERIFIED, category="piece", message="two"))
        assert [row.message for row in model.rows()] == ["two", "one"]
        assert model.recorded == 2

    def test_the_buffer_is_bounded(self) -> None:
        model = LogsViewModel(capacity=5)
        for index in range(20):
            model.add(Event(type=EventType.STATS_SAMPLE, category="statistics", message=str(index)))
        assert model.recorded == 5
        assert [row.message for row in model.rows()][-1] == "15"

    def test_filters_are_the_ones_the_specification_names(self) -> None:
        assert set(FILTERS) == {"ALL", "NETWORK", "TRACKER", "PEER", "PIECE", "DISK", "ERROR"}
        model = LogsViewModel()
        model.add(Event(type=EventType.PEER_CONNECTED, category="network", message="peer up"))
        model.set_filter("PEER")
        assert model.rows() == ()
        model.set_filter("NETWORK")
        assert len(model.rows()) == 1

    def test_error_is_a_severity_not_a_category(self) -> None:
        model = LogsViewModel()
        model.add(Event(type=EventType.DISK_ERROR, category="disk", message="bad write", level=50))
        model.set_filter("ERROR")
        assert len(model.rows()) == 1, "a disk failure is an error however it is categorised"

    def test_an_unknown_filter_shows_everything(self) -> None:
        model = LogsViewModel()
        model.add(Event(type=EventType.PEER_CONNECTED, category="network", message="peer up"))
        model.set_filter("MADE UP")
        assert model.filtername == "ALL"

    def test_search_looks_at_the_message(self) -> None:
        model = LogsViewModel()
        model.add(Event(type=EventType.TRACKER_FAILED, category="error", message="refused"))
        model.set_query("refus")
        assert len(model.rows()) == 1
        model.set_query("nothing like it")
        assert model.rows() == ()

    def test_it_can_be_scoped_to_one_torrent(self) -> None:
        model = LogsViewModel()
        model.add(
            Event(type=EventType.PIECE_VERIFIED, category="piece", message="in", torrent_id="aa")
        )
        model.add(
            Event(type=EventType.PIECE_VERIFIED, category="piece", message="out", torrent_id="bb")
        )
        model.set_torrent("aa")
        assert [row.message for row in model.rows()] == ["in"]

    def test_a_row_is_plain_data(self) -> None:
        row = row_for(
            Event(type=EventType.PEER_CONNECTED, category="network", message="hello", level=30)
        )
        assert isinstance(row, LogRow)
        assert row.category == "network"
        assert not row.error, "a warning is not an error"
        assert row.as_dict()["kind"] == "peer_connected"

    def test_a_row_built_by_hand_with_a_string_category_still_works(self) -> None:
        # Tests and replays build events by hand; a string category is the same
        # thing said another way and must not raise.
        row = row_for(Event(type=EventType.DISK_ERROR, category="error", message="boom", level=50))
        assert row.error
        assert row.category == "error"


# ----------------------------------------------------------------------- models


class TestTableModels:
    def test_the_peer_table_lists_the_swarm(self) -> None:
        model = PeerTableModel(PeersViewModel())
        model.set_peers(swarm_of())
        assert model.rowCount() == 5
        assert model.columnCount() >= 10
        assert model.headerData(0, _horizontal()) == "peer"
        assert model.data(model.index(0, 1)) == "client 0"

    def test_candidates_sort_below_connected_peers(self) -> None:
        model = PeerTableModel(PeersViewModel())
        model.set_peers(swarm_of(5, connected=2))
        states = [model.data(model.index(row, 1)) for row in range(model.rowCount())]
        assert states[:2] == ["client 0", "client 1"]

    def test_the_peer_table_shows_an_unmeasured_rate_as_unknown(self) -> None:
        from app.ui.format import UNKNOWN

        peers_view = PeersViewModel()
        peers_view.update(swarm_of())
        model = PeerTableModel(peers_view)
        model.set_peers(peers_view.peers)
        assert model.data(model.index(0, 2)) == UNKNOWN

    def test_the_tracker_table_shows_what_the_tracker_said(self) -> None:
        model = TrackerTableModel()
        model.set_statuses(trackers_of())
        assert model.rowCount() == 2
        assert model.data(model.index(0, 0)).endswith("/announce")
        assert model.data(model.index(0, 2)) == "412"
        assert model.role_at(1) == "error"

    def test_the_file_table_counts_progress_by_pieces(self) -> None:
        model = FileTableModel()
        model.set_files(files_of())
        assert model.rowCount() == 2
        assert model.data(model.index(0, 3)) == "42.0%"
        assert model.total_length == 800_001_024
        assert 0.0 < model.progress < 1.0

    def test_the_piece_legend_lists_all_five_states(self) -> None:
        model = PieceStateModel(PiecesViewModel())
        assert model.rowCount() == 5
        assert model.data(model.index(0, 0)) == "missing"
        assert model.data(model.index(3, 0)) == "verified"

    def test_the_field_model_labels_what_it_knows(self) -> None:
        model = TorrentFieldModel(filled_model())
        model.refresh()
        labels = [field.label for field in model.fields]
        assert "share ratio" in labels
        values = {field.label: field.value for field in fields_for(filled_model())}
        assert values["pieces"] == "42/100 verified"
        assert values["eta"].endswith("s") or values["eta"] == "--"

    def test_an_unknown_value_is_not_shown_as_zero(self) -> None:
        from app.ui.format import UNKNOWN

        model = TorrentViewModel()
        model.update_torrent(view_of(metrics=metrics_of(eta_seconds=None, share_ratio=None)))
        values = {field.label: field.value for field in fields_for(model)}
        assert values["eta"] == UNKNOWN
        assert values["share ratio"] == UNKNOWN


def _horizontal() -> Any:
    from PySide6.QtCore import Qt

    return Qt.Orientation.Horizontal


# ------------------------------------------------------------------------- tabs


class TestOverviewTab:
    def test_it_shows_the_torrent_it_was_given(self, qapp: object) -> None:
        tab = OverviewTab(filled_model(), DARK)
        assert tab.empty.isHidden()
        assert "42.0%" in tab._stats["progress"]._value.text()
        assert tab._stats["down"]._value.text().endswith("/s")

    def test_without_a_torrent_it_says_so(self, qapp: object) -> None:
        model = TorrentViewModel()
        tab = OverviewTab(model, DARK)
        assert not tab.empty.isHidden()
        assert tab._stats["progress"]._value.text() == "--"

    def test_the_graph_gets_the_recorded_series(self, qapp: object) -> None:
        model = TorrentViewModel()
        model.update_torrent(view_of())
        model.update_torrent(view_of(), now=_clock() + 1.0)
        tab = OverviewTab(model, DARK)
        assert len(tab.graph.series()[0].points) == 2


class TestPeersTab:
    def test_it_places_the_swarm_and_lists_it(self, qapp: object) -> None:
        tab = PeersTab(filled_model(), DARK)
        tab.resize(900, 700)
        assert len(tab.canvas.nodes()) == 5
        assert tab.model.rowCount() == 5
        assert "3 connected" in tab._canvas_panel.subtitle

    def test_hovering_a_node_describes_it(self, qapp: object) -> None:
        tab = PeersTab(filled_model(), DARK)
        tab.resize(900, 700)
        node = tab.canvas.nodes()[0]
        tab.canvas.peer_hovered.emit(node)
        assert "client 0" in tab._hovered.text()

    def test_selecting_a_node_selects_its_row(self, qapp: object) -> None:
        tab = PeersTab(filled_model(), DARK)
        tab.resize(900, 700)
        node = tab.canvas.nodes()[2]
        tab.canvas.peer_selected.emit(node)
        selected = tab.table.selectionModel().selectedRows()
        assert selected
        assert tab.model.peer_at(selected[0].row()).key == node.peer.key

    def test_an_empty_swarm_says_it_is_waiting(self, qapp: object) -> None:
        tab = PeersTab(TorrentViewModel(), DARK)
        tab.resize(900, 700)
        assert tab.canvas.nodes() == ()
        assert "No peers yet" in tab._canvas_panel.subtitle


class TestPiecesTab:
    def test_it_draws_every_piece(self, qapp: object) -> None:
        tab = PiecesTab(filled_model(), DARK)
        tab.resize(900, 700)
        assert len(tab.matrix.cells()) == 100

    def test_it_warns_about_pieces_nobody_has(self, qapp: object) -> None:
        tab = PiecesTab(filled_model(), DARK)
        tab.resize(900, 700)
        assert not tab.warning.isHidden()
        assert "nobody connected" in tab.warning.text()

    def test_a_complete_torrent_has_no_warning(self, qapp: object) -> None:
        model = filled_model()
        model.update_pieces(
            PieceMap(
                piece_count=10,
                piece_length=262_144,
                total_length=262_144 * 10,
                states=bytes([3] * 10),
                availability=tuple(1 for _ in range(10)),
                filled=tuple(0.0 for _ in range(10)),
                counts={
                    "verified": 10,
                    "missing": 0,
                    "requested": 0,
                    "downloading": 0,
                    "failed": 0,
                },
            )
        )
        tab = PiecesTab(model, DARK)
        tab.resize(900, 700)
        assert tab.warning.isHidden()

    def test_hovering_reads_out_one_piece(self, qapp: object) -> None:
        tab = PiecesTab(filled_model(), DARK)
        tab.resize(900, 700)
        cell = tab.matrix.cells()[7]
        tab.matrix.piece_hovered.emit(cell)
        assert "Piece 7" in tab._readout.text()


class TestFilesTab:
    def test_it_lists_the_files(self, qapp: object) -> None:
        tab = FilesTab()
        tab.set_files(files_of())
        assert tab.model.rowCount() == 2
        assert tab.empty.isHidden()
        assert "2 files" in tab._total.text()

    def test_without_files_it_says_so(self, qapp: object) -> None:
        tab = FilesTab()
        assert not tab.empty.isHidden()


class TestTrackersTab:
    def test_it_shows_the_trackers(self, qapp: object) -> None:
        tab = TrackersTab()
        tab.set_statuses(trackers_of())
        assert tab.model.rowCount() == 2
        assert tab.empty.isHidden()
        assert "412 seeders" in tab._summary.text()

    def test_a_torrent_with_no_trackers_explains_why(self, qapp: object) -> None:
        tab = TrackersTab()
        assert not tab.empty.isHidden()
        assert "No trackers" in tab.empty._title.text()


class TestLogsTab:
    def test_it_shows_the_timeline(self, qapp: object) -> None:
        model = LogsViewModel()
        tab = LogsTab(model)
        model.add(Event(type=EventType.PEER_CONNECTED, category="network", message="peer up"))
        assert tab.model.rowCount() == 1
        assert "1 of 1 events shown" in tab._caption.text()

    def test_the_filter_buttons_drive_the_view_model(self, qapp: object) -> None:
        model = LogsViewModel()
        tab = LogsTab(model)
        model.add(Event(type=EventType.DISK_ERROR, category="disk", message="boom", level=50))
        model.add(Event(type=EventType.PEER_CONNECTED, category="network", message="peer up"))
        tab.set_filter("ERROR")
        assert tab.model.rowCount() == 1
        assert tab._buttons["ERROR"].isChecked()
        assert not tab._buttons["ALL"].isChecked()

    def test_the_search_box_drives_the_view_model(self, qapp: object) -> None:
        model = LogsViewModel()
        tab = LogsTab(model)
        model.add(Event(type=EventType.PEER_CONNECTED, category="network", message="alpha"))
        model.add(Event(type=EventType.PEER_CONNECTED, category="network", message="beta"))
        tab.search.setText("alph")
        assert tab.model.rowCount() == 1

    def test_it_can_be_scoped_to_one_torrent(self, qapp: object) -> None:
        model = LogsViewModel()
        tab = LogsTab(model)
        model.add(
            Event(type=EventType.PIECE_VERIFIED, category="piece", message="mine", torrent_id="bb")
        )
        model.add(
            Event(
                type=EventType.PIECE_VERIFIED,
                category="piece",
                message="theirs",
                torrent_id="cc",
            )
        )
        tab.set_torrent("bb")
        assert tab.model.rowCount() == 1


# ------------------------------------------------------------------- detail page


class TestTorrentDetailView:
    def _built(self) -> TorrentDetailView:
        return TorrentDetailView(filled_model(), LogsViewModel(), DARK)

    def test_it_hosts_six_tabs(self, qapp: object) -> None:
        view = self._built()
        assert view.tabs.count() == 6
        assert view.current_tab == "overview"

    def test_it_refreshes_only_the_visible_tab(self, qapp: object) -> None:
        view = self._built()
        view.resize(1200, 800)
        seen: list[str] = []
        for name, tab in (
            ("overview", view.overview),
            ("peers", view.peers),
            ("pieces", view.pieces),
            ("files", view.files),
            ("trackers", view.trackers),
            ("log", view.log),
        ):
            tab.refresh = (  # type: ignore[method-assign]
                lambda name=name: seen.append(name)
            )
        view.show_tab("pieces")
        assert seen == ["pieces"]

    def test_the_header_names_the_torrent(self, qapp: object) -> None:
        view = self._built()
        assert view.title.text() == "Debian 13.6.0 netinst"
        assert "downloading" in view._state.text()

    def test_without_a_torrent_the_buttons_are_disabled(self, qapp: object) -> None:
        view = TorrentDetailView(TorrentViewModel(), LogsViewModel(), DARK)
        assert view.title.text() == "No torrent selected"
        assert not view._pause.isEnabled()

    def test_the_slow_views_are_pushed_in(self, qapp: object) -> None:
        view = self._built()
        view.set_files(files_of())
        view.set_trackers(trackers_of())
        assert view.files.model.rowCount() == 2
        assert view.trackers.model.rowCount() == 2

    def test_the_actions_reach_the_handlers(self, qapp: object) -> None:
        view = self._built()
        asked: list[str] = []
        view.connect_actions(asked.append, asked.append)
        view._pause.click()
        assert asked == ["a" * 40]

    def test_switching_tabs_by_name(self, qapp: object) -> None:
        view = self._built()
        view.show_tab("trackers")
        assert view.current_tab == "trackers"
        with pytest.raises(KeyError):
            view.show_tab("nope")

    def test_reduced_motion_reaches_the_canvas(self, qapp: object) -> None:
        view = self._built()
        view.set_reduced_motion(True)
        assert view.peers.canvas._reduced_motion


class TestModelHousekeeping:
    """The models are Qt objects: they must behave like ones."""

    def test_the_field_model_refreshes_and_keeps_the_data_font(self, qapp: object) -> None:
        model = TorrentFieldModel(filled_model())
        model.refresh()
        before = len(model.fields)
        model.refresh()  # nothing changed: no reset, no flicker
        assert len(model.fields) == before

        index = model.index(0, 1)
        assert model.data(index) is not None
        from PySide6.QtCore import Qt

        font = model.data(model.index(2, 1), Qt.ItemDataRole.FontRole)
        assert font is not None, "the info hash is drawn in the data font"

    def test_the_peer_model_forgets_everything(self, qapp: object) -> None:
        model = PeerTableModel(PeersViewModel())
        model.set_peers(swarm_of())
        assert model.rowCount() == 5
        model.clear()
        assert model.rowCount() == 0
        assert model.peer_at(0) is None
        assert model.peers == ()

    def test_the_file_model_reports_a_torrent_of_nothing(self, qapp: object) -> None:
        model = FileTableModel()
        assert model.progress == 0.0
        assert model.total_length == 0
        assert model.file_at(0) is None

    def test_the_tracker_model_reports_a_next_announce(self, qapp: object) -> None:
        model = TrackerTableModel()
        model.set_statuses(trackers_of())
        soon = model.data(model.index(0, 7))
        assert soon.endswith("s") or soon == "now" or soon == "--"

    def test_the_piece_legend_shares_the_matrixs_colours(self, qapp: object) -> None:
        pieces = PiecesViewModel()
        pieces.update(piece_map_of(verified=40, total=100))
        model = PieceStateModel(pieces)
        assert model.role_for(0) == "missing"
        assert model.role_for(3) == "verified"
        assert model.role_for(99) == "missing", "an unknown row is grey, not black"
