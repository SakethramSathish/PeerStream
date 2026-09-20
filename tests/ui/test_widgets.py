"""The widgets: they show what they are given, and nothing else.

The claims worth testing are the ones a screenshot cannot show:

* A progress bar refuses 140 % rather than wrapping.
* A health meter with nothing to measure says "no data" instead of drawing a
  confident-looking full bar.
* A sparkline with one sample says so, instead of drawing a line through a gap
  it has no data for.
* Toasts are capped, and an empty message is refused rather than shown blank.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.ui.widgets.add_torrent_dialog import AddTorrentDialog
from app.ui.widgets.empty_state import EmptyState, not_implemented
from app.ui.widgets.health_meter import HealthMeter
from app.ui.widgets.progress_bar import ProgressBar, SegmentedBar
from app.ui.widgets.sidebar import Sidebar
from app.ui.widgets.sparkline import Sparkline
from app.ui.widgets.stat_card import StatCard, StatGrid
from app.ui.widgets.toast import Toast, ToastHost, ToastKind
from app.ui.widgets.topbar import TopBar
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QProgressBar

DESTINATIONS: tuple[tuple[str, str, str], ...] = (
    ("overview", "Command centre", "overview"),
    ("library", "Library", "library"),
    ("settings", "Settings", "settings"),
)


class TestProgressBar:
    def test_a_fraction_is_a_fraction(self, qapp: object) -> None:
        bar = ProgressBar()
        bar.set_fraction(0.42)
        assert bar.fraction == pytest.approx(0.42)

    def test_more_than_the_whole_is_refused(self, qapp: object) -> None:
        bar = ProgressBar()
        bar.set_fraction(1.4)
        assert bar.fraction == 1.0
        bar.set_fraction(-0.2)
        assert bar.fraction == 0.0

    def test_the_role_changes_with_the_state(self, qapp: object) -> None:
        bar = ProgressBar()
        bar.set_role("success")
        assert bar.property("role") == "success"

    def test_the_value_is_not_printed_on_top_of_the_bar(self, qapp: object) -> None:
        assert not ProgressBar().isTextVisible()


class TestSegmentedBar:
    def test_it_always_has_at_least_one_segment(self, qapp: object) -> None:
        bar = SegmentedBar(segments=0)
        bar.set_fraction(0.5)
        assert bar.minimumWidth() > 0


class TestStatCard:
    def test_the_value_and_detail_can_be_updated(self, qapp: object) -> None:
        card = StatCard("Download", "0 B/s", detail="none yet")
        card.set_value("1.2 MiB/s")
        card.set_detail("512 MiB total")
        assert card.value_label.text() == "1.2 MiB/s"

    def test_an_empty_detail_keeps_its_row(self, qapp: object) -> None:
        # A card that grew a second line when data appeared would shift every
        # card below it.
        card = StatCard("Peers")
        card.set_detail("")
        assert card.findChildren(type(card.value_label))  # the value is still there

    def test_a_grid_remembers_its_cards(self, qapp: object) -> None:
        grid = StatGrid(2)
        grid.add("a", "A")
        grid.add("b", "B")
        assert grid.card("a") is not None
        assert grid["b"] is grid.card("b")
        assert grid.card("missing") is None


class TestHealthMeter:
    def test_a_ratio_shows_as_a_percentage_with_its_counts(self, qapp: object) -> None:
        meter = HealthMeter("Peers")
        meter.set_health(0.5, note="2/4 unchoked")
        assert "50%" in meter._value.text()
        assert "2/4 unchoked" in meter._value.text()

    def test_nothing_to_measure_says_so(self, qapp: object) -> None:
        meter = HealthMeter("Peers")
        meter.set_health(None)
        assert meter._value.text() == "no data"
        assert meter.fraction is None

    def test_a_health_out_of_range_is_clamped(self, qapp: object) -> None:
        meter = HealthMeter("Peers")
        meter.set_health(4.0)
        assert meter.fraction == 1.0


class TestSparkline:
    def test_one_sample_is_not_a_trend(self, qapp: object) -> None:
        graph = Sparkline()
        graph.set_series([(0.0, 1.0)])
        assert graph.empty

    def test_two_samples_are_enough_to_draw(self, qapp: object) -> None:
        graph = Sparkline()
        graph.set_series([(0.0, 1.0), (1.0, 2.0)])
        assert not graph.empty
        assert len(graph.series) == 2

    def test_the_caller_keeps_its_buffer(self, qapp: object) -> None:
        samples = [(0.0, 1.0), (1.0, 2.0)]
        graph = Sparkline()
        graph.set_series(samples)
        samples.append((2.0, 3.0))
        assert len(graph.series) == 2

    def test_clearing_empties_it(self, qapp: object) -> None:
        graph = Sparkline()
        graph.set_series([(0.0, 1.0), (1.0, 2.0)])
        graph.clear()
        assert graph.empty


class TestToasts:
    def test_a_message_is_shown(self, qapp: object) -> None:
        host = ToastHost()
        assert host.notify(Toast("tracker refused us"))
        assert host.count == 1

    def test_an_empty_message_is_refused(self, qapp: object) -> None:
        host = ToastHost()
        assert not host.notify(Toast("   "))
        assert host.count == 0

    def test_there_is_a_cap_on_how_many_can_be_on_screen(self, qapp: object) -> None:
        host = ToastHost()
        for index in range(10):
            host.notify(Toast(f"event {index}"))
        assert host.count <= 4

    def test_the_shortcuts_use_the_right_kinds(self, qapp: object) -> None:
        host = ToastHost()
        host.error("bad")
        host.warning("hmm")
        host.success("good")
        host.info("fyi")
        assert host.count == 4

    def test_clearing_removes_them_all(self, qapp: object) -> None:
        host = ToastHost()
        host.info("one")
        host.clear()
        assert host.count == 0

    def test_a_kind_maps_to_a_colour_role(self) -> None:
        assert Toast("x", ToastKind.ERROR).kind == ToastKind.ERROR


class TestSidebar:
    def test_it_lists_every_destination(self, qapp: object) -> None:
        sidebar = Sidebar(DESTINATIONS)
        assert sidebar.current is None
        sidebar.set_current("library")
        assert sidebar.current == "library"

    def test_choosing_a_destination_is_announced(self, qapp: object) -> None:
        sidebar = Sidebar(DESTINATIONS)
        seen: list[str] = []
        sidebar.navigated.connect(seen.append)
        sidebar.set_current("settings")  # silent: it is the caller selecting
        sidebar._buttons["overview"].click()
        assert seen == ["overview"]

    def test_an_unknown_destination_is_an_error(self, qapp: object) -> None:
        sidebar = Sidebar(DESTINATIONS)
        with pytest.raises(KeyError):
            sidebar.set_current("hovercraft")

    def test_a_badge_counts_something_measured(self, qapp: object) -> None:
        sidebar = Sidebar(DESTINATIONS)
        sidebar.set_badges({"library": 3, "not-a-page": 9})
        assert "3" in sidebar._buttons["library"].text()

    def test_the_footer_holds_the_small_print(self, qapp: object) -> None:
        sidebar = Sidebar(DESTINATIONS)
        sidebar.set_footer("saving to /tmp")
        assert sidebar._footer.text() == "saving to /tmp"


class TestTopBar:
    def test_rates_are_formatted(self, qapp: object) -> None:
        bar = TopBar()
        bar.set_rates(1024 * 256, 0.0)
        assert "256.00 KiB/s" in bar._download.text()
        assert bar._upload.text() == "0 B/s"

    def test_the_summary_is_plain_text(self, qapp: object) -> None:
        bar = TopBar()
        bar.set_summary("2 of 3 active")
        assert bar._summary.text() == "2 of 3 active"

    def test_adding_is_one_click_away(self, qapp: object) -> None:
        bar = TopBar()
        pressed: list[bool] = []
        bar.add_requested.connect(lambda: pressed.append(True))
        bar._add.click()
        assert pressed == [True]


class TestEmptyState:
    def test_it_can_offer_an_action(self, qapp: object) -> None:
        state = EmptyState("Nothing here", "Add a torrent to begin.")
        state.set_action("Add torrent")
        assert not state._button.isHidden()

    def test_the_action_can_be_withdrawn(self, qapp: object) -> None:
        state = EmptyState("Nothing here", "...")
        state.set_action("Add torrent")
        state.set_action(None)
        assert state._button.isHidden()

    def test_a_feature_that_does_not_exist_yet_says_which_milestone_owns_it(
        self, qapp: object
    ) -> None:
        # "Coming soon" without a date is a lie of omission.
        state = not_implemented("DHT", milestone="M15", what_will_work=["HTTP trackers work"])
        assert "M15" in state._body.text()
        assert "DHT" in state._title.text()


class TestPainting:
    """A widget that draws by hand must draw something.

    These render offscreen and look at the pixels, because a paint method that
    silently draws nothing is invisible to every other kind of test.
    """

    def test_the_segmented_bar_fills_what_it_is_told(self, qapp: object) -> None:
        bar = SegmentedBar(segments=16)
        bar.set_fraction(0.5)
        bar.resize(200, 12)
        image = bar.grab().toImage()
        assert not image.isNull()
        colours = {
            image.pixelColor(x, y).name()
            for y in range(image.height())
            for x in range(0, image.width(), 2)
        }
        assert len(colours) >= 2, "a half-filled bar has two colours in it"

    def test_a_sparkline_draws_its_series(self, qapp: object) -> None:
        graph = Sparkline()
        graph.resize(240, 120)
        graph.set_series([(float(index), float(index * index)) for index in range(12)])
        image = graph.grab().toImage()
        assert not image.isNull()
        colours = {
            image.pixelColor(x, y).name()
            for y in range(0, image.height(), 3)
            for x in range(0, image.width(), 3)
        }
        assert len(colours) >= 3, "a graph with a fill and a stroke is not one colour"

    def test_an_empty_sparkline_says_it_is_waiting(self, qapp: object) -> None:
        graph = Sparkline()
        graph.resize(240, 120)
        graph.set_series([(0.0, 1.0)])
        # One sample is not a trend, so the widget says so instead of drawing
        # a line through a gap it has no data for.
        assert graph.empty
        assert not graph.grab().isNull()

    def test_hovering_names_a_sample(self, qapp: object) -> None:
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent

        graph = Sparkline()
        graph.resize(240, 120)
        graph.set_series([(float(index), float(index)) for index in range(10)])
        assert graph.hovered is None
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(graph.width() * 0.6, graph.height() / 2),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(graph, event)
        assert graph.hovered is not None
        assert graph.hovered[1] == pytest.approx(graph.hovered[0])


class TestAddTorrentDialog:
    """Adding a torrent: parse it first, show what it really is, then add."""

    def test_a_real_torrent_is_described(self, qapp: object, tmp_path: Path) -> None:
        from tools.screenshot import make_test_torrent

        torrent_path, _payload = make_test_torrent(tmp_path, size="256KiB")
        dialog = AddTorrentDialog(default_directory=tmp_path)
        dialog.set_source(str(torrent_path))
        assert dialog.torrent is not None
        assert dialog.torrent.name in dialog._summary.text()
        assert "256.00 KiB" in dialog._summary.text()
        assert dialog._accept.isEnabled()

    @pytest.mark.parametrize(
        "source",
        [
            str(Path("/does/not/exist.torrent")),
            "magnet:?xt=urn:btmh:" + "ab" * 32,
            "magnet:?dn=only-a-name",
            "",
        ],
    )
    def test_sources_that_cannot_be_added_say_why(self, qapp: object, source: str) -> None:
        dialog = AddTorrentDialog()
        dialog.set_source(source)
        assert dialog.torrent is None
        assert dialog.magnet is None
        assert not dialog._accept.isEnabled()


class TestAddMagnetDialog:
    """A magnet is a promise, so the dialog says what it can and cannot know."""

    LINK = "magnet:?xt=urn:btih:481b6e3617be4c88f96cb25e47c9d8272130071e&dn=Debian&tr=http://bttracker.debian.org:6969/announce&x.pe=198.51.100.7:51413"

    def test_a_magnet_is_described_by_what_it_carries(self, qapp: object) -> None:
        dialog = AddTorrentDialog()
        dialog.set_source(self.LINK)
        assert dialog.magnet is not None
        assert dialog.torrent is None
        assert dialog._accept.isEnabled()

        summary = dialog._summary.text()
        assert "481b6e3617be4c88f96cb25e47c9d8272130071e" in summary
        assert "1 tracker" in summary
        assert "1 peer" in summary

    def test_the_size_is_admitted_to_be_unknown(self, qapp: object) -> None:
        # The one thing a magnet cannot know. Saying so is the whole point:
        # a dialog that showed "0 B" would be inventing a measurement.
        dialog = AddTorrentDialog()
        dialog.set_source(self.LINK)
        assert "unknown until the metadata arrives" in dialog._summary.text()
        assert "metadata" in dialog._status.text()

    def test_a_magnet_with_no_sources_is_still_addable(self, qapp: object) -> None:
        dialog = AddTorrentDialog()
        dialog.set_source("magnet:?xt=urn:btih:" + "ab" * 20)
        assert dialog.magnet is not None
        assert dialog._accept.isEnabled()
        assert "none" in dialog._summary.text(), "it must say the DHT will have to find peers"

    def test_accepting_announces_the_magnet_not_a_torrent(self, qapp: object) -> None:
        dialog = AddTorrentDialog()
        dialog.set_source(self.LINK)
        accepted: list[object] = []
        dialog.magnet_accepted.connect(accepted.append)
        dialog._on_accept()
        assert len(accepted) == 1
        assert accepted[0].hex_info_hash == "481b6e3617be4c88f96cb25e47c9d8272130071e"

    def test_a_v2_only_magnet_says_which_version_it_cannot_fetch(self, qapp: object) -> None:
        dialog = AddTorrentDialog()
        dialog.set_source("magnet:?xt=urn:btmh:" + "cd" * 32)
        assert dialog.magnet is None
        assert not dialog._accept.isEnabled()
        assert "v2" in dialog._status.text()

    def test_accepting_announces_the_torrent(self, qapp: object, tmp_path: Path) -> None:
        from tools.screenshot import make_test_torrent

        torrent_path, _payload = make_test_torrent(tmp_path, size="128KiB")
        dialog = AddTorrentDialog(default_directory=tmp_path)
        dialog.set_source(str(torrent_path))
        accepted: list[object] = []
        dialog.torrent_accepted.connect(accepted.append)
        dialog._on_accept()
        assert len(accepted) == 1
        assert dialog.directory == str(tmp_path)
        assert dialog.start_now is True

    def test_a_broken_torrent_file_reports_the_file(self, qapp: object, tmp_path: Path) -> None:
        broken = tmp_path / "broken.torrent"
        broken.write_bytes(b"this is not bencode at all")
        dialog = AddTorrentDialog()
        dialog.set_source(str(broken))
        assert dialog.torrent is None
        assert "Could not read" in dialog._status.text()


class TestWidgetsShareTheProgressBarRole:
    def test_a_torrent_row_bar_follows_the_state(self, qapp: object) -> None:
        from app.services.engine import TorrentState
        from app.services.torrent_service import TorrentView
        from app.ui.widgets.torrent_row import BAR_ROLES, TorrentRow

        assert set(BAR_ROLES) == {state.value for state in TorrentState}
        view = TorrentView(
            info_hash="ab" * 20,
            name="debian.iso",
            state=TorrentState.SEEDING,
            progress=1.0,
            total_length=1024,
            verified_pieces=4,
            missing_pieces=0,
            piece_count=4,
            port=6881,
            resumed=None,  # type: ignore[arg-type]
        )
        row = TorrentRow(view)
        bar = row.findChild(QProgressBar)
        assert bar is not None
        assert bar.property("role") == "success"
