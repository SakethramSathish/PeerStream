"""The three signature visuals, tested through their data mapping.

The rule for these charts is that every visual variable is a measurement. So
the tests do not take screenshots and squint at them: they feed numbers in and
assert on the geometry that comes out — where a peer was placed, how wide its
edge is, how many cells a matrix made, where a sample landed on a graph.

Two things are deliberately *not* tested here:

* **That it looks nice.** A pixel test cannot tell a beautiful lie from a
  beautiful truth.
* **Animation for its own sake.** The one animated thing — a peer's glow — is
  tested as arithmetic: stimulate, decay, read the level. No timer is advanced
  and no frame is faked.
"""

from __future__ import annotations

import math

import pytest
from app.services.torrent_service import PeerView, PieceMap
from app.ui.animations.pulse import PulseBank, activity_level, decay
from app.ui.animations.transitions import (
    Fader,
    Ticker,
    duration_for,
    ease_out_cubic,
    should_animate,
)
from app.ui.charts.piece_matrix import PieceMatrix, _tooltip, layout_cells
from app.ui.charts.rate_graph import RateGraph, _runs_of, map_series, nice_ceiling
from app.ui.charts.swarm_canvas import (
    CONNECTED_RING,
    MIN_NODE_SCALE,
    SwarmCanvas,
    SwarmNode,
    place_nodes,
)
from app.ui.theme.palette import DARK
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.peers_vm import MIN_SAMPLE_SECONDS, PeersViewModel
from app.ui.viewmodels.pieces_vm import PiecesViewModel
from app.ui.viewmodels.torrent_vm import TorrentViewModel
from app.ui.views.peers_tab import PeersTab
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtWidgets import QWidget


def peers_for(*indices: int, connected: int = 3) -> tuple[PeerView, ...]:
    """A swarm of peers, the first ``connected`` of them connected."""
    return tuple(
        PeerView(
            key=f"10.0.0.{index}:6881",
            host=f"10.0.0.{index}",
            port=6881,
            client=f"client {index}",
            state="connected" if position < connected else "candidate",
            source="tracker",
            pieces_held=100 if index == 0 else 10 * index,
            piece_count=100,
            choking_us=index % 2 == 1,
            interested_in_us=index == 0,
            latency_ms=10.0 * index,
        )
        for position, index in enumerate(indices)
    )


def model_with(peers: tuple[PeerView, ...]) -> PeersViewModel:
    """A view model holding one read of this swarm."""
    model = PeersViewModel()
    model.update(peers)
    return model


# ------------------------------------------------------------------ swarm canvas


class TestSwarmPlacement:
    """Where peers go, and why."""

    def test_connected_peers_sit_on_the_inner_ring(self) -> None:
        model = model_with(peers_for(0, 1, 2, connected=3))
        nodes = place_nodes(model.peers, model, width=400.0, height=400.0)
        centre = 200.0
        distances = [math.hypot(node.x - centre, node.y - centre) for node in nodes[:3]]
        assert all(abs(distance - 400 * CONNECTED_RING) < 0.5 for distance in distances)

    def test_candidates_sit_outside_the_connected_ring(self) -> None:
        model = model_with(peers_for(0, 1, 2, 3, 4, connected=2))
        nodes = place_nodes(model.peers, model, width=400.0, height=400.0)
        inner = [math.hypot(node.x - 200, node.y - 200) for node in nodes[:2]]
        outer = [math.hypot(node.x - 200, node.y - 200) for node in nodes[2:]]
        assert max(inner) < min(outer)

    def test_a_peers_angle_depends_only_on_its_address(self) -> None:
        # A peer must not slide around when the set changes, so placement is a
        # hash of the address and nothing else.
        first = model_with(peers_for(0, 1, 2))
        second = model_with(peers_for(3, 1, 2))
        elsewhere = place_nodes(first.peers, first, width=400.0, height=400.0)
        again = place_nodes(second.peers, second, width=400.0, height=400.0)
        assert elsewhere[1].as_dict()["x"] == again[1].as_dict()["x"]
        assert elsewhere[1].as_dict()["y"] == again[1].as_dict()["y"]

    def test_node_size_is_how_much_the_peer_holds(self) -> None:
        model = model_with(peers_for(0, 1, 2))
        nodes = place_nodes(model.peers, model, width=400.0, height=400.0)
        # Peer 0 holds all 100 pieces, peer 1 holds 10.
        assert nodes[0].radius > nodes[1].radius >= 7 * MIN_NODE_SCALE

    def test_a_seed_and_a_leech_are_different_kinds(self) -> None:
        model = model_with(peers_for(0, 1, 2))
        nodes = place_nodes(model.peers, model, width=400.0, height=400.0)
        assert nodes[0].role == "seed"
        assert nodes[1].role == "leech"

    def test_edge_width_follows_the_measured_rate(self) -> None:
        model = model_with(peers_for(0, 1, 2))
        model.update(
            tuple(
                peer.__class__(
                    **{
                        **_fields(peer),
                        "downloaded": 500_000 if peer.key.endswith("0:6881") else 10,
                    }
                )
                for peer in model.peers
            ),
            now=_now() + 1.0,
        )
        nodes = place_nodes(model.peers, model, width=400.0, height=400.0)
        by_key = {node.peer.key: node for node in nodes}
        fast = by_key["10.0.0.0:6881"]
        slow = by_key["10.0.0.1:6881"]
        assert fast.edge > slow.edge

    def test_an_unmeasured_rate_is_not_a_zero_rate(self) -> None:
        # One read cannot produce a rate. Claiming 0 B/s would say the peer
        # went quiet, which is not what was observed.
        model = model_with(peers_for(0, 1))
        assert model.activity_for("10.0.0.0:6881").down_rate is None
        assert model.download_rate == 0.0

    def test_a_rate_needs_two_reads_far_enough_apart(self) -> None:
        model = model_with(peers_for(0, 1))
        model.update(
            tuple(
                peer.__class__(**{**_fields(peer), "downloaded": peer.downloaded + 1_000})
                for peer in model.peers
            ),
            now=_now() + MIN_SAMPLE_SECONDS / 2,
        )
        assert model.activity_for("10.0.0.0:6881").down_rate is None

    def test_hovering_finds_the_node_under_the_pointer(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.resize(400, 400)
        canvas.set_view_model(model_with(peers_for(0, 1, 2)))
        node = canvas.nodes()[0]
        assert canvas.node_at(QPointF(node.x, node.y)) is node
        assert canvas.node_at(QPointF(1.0, 1.0)) is None

    def test_an_empty_swarm_places_nothing(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.resize(400, 400)
        canvas.set_view_model(PeersViewModel())
        assert canvas.nodes() == ()

    def test_the_canvas_draws_a_swarm(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.resize(360, 320)
        canvas.set_view_model(model_with(peers_for(0, 1, 2, 3, connected=3)))
        canvas.set_our_progress(0.42)
        image = canvas.grab().toImage()
        assert not image.isNull()
        colours = {
            image.pixelColor(x, y).name()
            for y in range(0, image.height(), 3)
            for x in range(0, image.width(), 3)
        }
        assert len(colours) >= 4, "peers, edges and the centre node are not one colour"

    def test_the_canvas_says_when_there_are_no_peers(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.resize(360, 320)
        canvas.set_view_model(PeersViewModel())
        assert not canvas.grab().isNull()


def _fields(peer: PeerView) -> dict[str, object]:
    """A peer's fields, so a test can rebuild it with one difference."""
    from dataclasses import fields

    return {field.name: getattr(peer, field.name) for field in fields(peer)}


def _now() -> float:
    import time

    return time.monotonic()


# ------------------------------------------------------------------ piece matrix


def piece_map_of(states: list[int], *, availability: int = 1) -> PieceMap:
    """A piece map from a list of state codes."""
    counts = dict.fromkeys(("missing", "requested", "downloading", "verified", "failed"), 0)
    names = tuple(counts)
    for code in states:
        counts[names[code]] += 1
    return PieceMap(
        piece_count=len(states),
        piece_length=262_144,
        total_length=262_144 * len(states),
        states=bytes(states),
        availability=tuple(availability for _ in states),
        filled=tuple(0.5 if code == 2 else 0.0 for code in states),
        counts=counts,
    )


class TestPieceMatrix:
    """Cells, grids, and what the grid says about a download."""

    def test_the_grid_wraps_at_the_available_width(self) -> None:
        cells, columns = layout_cells(piece_map_of([0] * 100), width=500.0, height=200.0)
        assert columns > 1
        assert len(cells) == 100
        assert cells[columns].row == 1
        assert cells[columns].column == 0

    def test_every_piece_gets_a_cell(self) -> None:
        cells, _columns = layout_cells(piece_map_of([3] * 37), width=400.0, height=160.0)
        assert [cell.index for cell in cells] == list(range(37))

    def test_cells_carry_their_state_and_availability(self) -> None:
        cells, _columns = layout_cells(
            piece_map_of([0, 1, 2, 3, 4], availability=0), width=400.0, height=160.0
        )
        assert [cell.name for cell in cells] == [
            "missing",
            "requested",
            "downloading",
            "verified",
            "failed",
        ]
        assert [cell.orphaned for cell in cells] == [True, True, True, False, True]

    def test_a_piece_nobody_has_is_orphaned(self) -> None:
        cells, _columns = layout_cells(piece_map_of([0], availability=0), width=400.0, height=160.0)
        assert cells[0].orphaned

    def test_the_grid_shrinks_rather_than_clipping(self) -> None:
        tall = piece_map_of([0] * 2000)
        roomy, _ = layout_cells(tall, width=400.0, height=1000.0)
        squeezed, _ = layout_cells(tall, width=400.0, height=120.0)
        assert squeezed[0].size <= roomy[0].size
        assert len(squeezed) == len(roomy) == 2000

    def test_hovering_finds_the_cell_under_the_pointer(self, qapp: object) -> None:
        matrix = PieceMatrix(DARK)
        matrix.resize(400, 160)
        matrix.set_piece_map(piece_map_of([0] * 64))
        target = matrix.cells()[9]
        assert matrix.cell_at(QPointF(target.x + 1, target.y + 1)) is target

    def test_the_keyboard_walks_the_grid(self, qapp: object) -> None:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QApplication

        matrix = PieceMatrix(DARK)
        QApplication.processEvents()
        matrix.resize(400, 160)
        matrix.set_piece_map(piece_map_of([0] * 64))
        matrix.set_cursor(0)
        for _ in range(3):
            matrix.keyPressEvent(
                QKeyEvent(
                    QEvent.Type.KeyPress,
                    _key("Right"),
                    _no_modifier(),
                )
            )
        assert matrix.cursor_index == 3

    def test_the_matrix_draws_its_states(self, qapp: object) -> None:
        matrix = PieceMatrix(DARK)
        matrix.resize(400, 160)
        matrix.set_piece_map(piece_map_of([0, 0, 1, 1, 2, 2, 3, 3, 4, 4] * 4))
        image = matrix.grab().toImage()
        assert not image.isNull()
        colours = {
            image.pixelColor(x, y).name()
            for y in range(0, image.height(), 2)
            for x in range(0, image.width(), 2)
        }
        assert len(colours) >= 4, "five piece states are not one colour"

    def test_an_empty_matrix_says_so(self, qapp: object) -> None:
        matrix = PieceMatrix(DARK)
        matrix.resize(400, 160)
        matrix.set_piece_map(None)
        assert matrix.cells() == ()
        assert not matrix.grab().isNull()


def _key(name: str) -> int:
    from PySide6.QtCore import Qt

    return int(getattr(Qt.Key, f"Key_{name}"))


def _no_modifier() -> object:
    from PySide6.QtCore import Qt

    return Qt.KeyboardModifier.NoModifier


# -------------------------------------------------------------------- rate graph


class TestRateGraph:
    """Samples in, points out, with an honest axis."""

    def test_two_samples_make_a_line(self) -> None:
        series = map_series(
            [(0.0, 0.0), (1.0, 1_000_000.0)],
            name="download",
            colour="#4C8DFF",
            area=QRectF(0.0, 0.0, 200.0, 100.0),
            scale=1_048_576.0,
        )
        assert series.plotted
        assert len(series.points) == 2
        first, last = series.points
        assert first.x() == pytest.approx(0.0)
        assert last.x() == pytest.approx(200.0)
        assert last.y() < first.y(), "a higher rate is drawn higher up"

    def test_one_sample_is_not_a_trend(self) -> None:
        series = map_series(
            [(0.0, 10.0)],
            name="download",
            colour="#4C8DFF",
            area=QRectF(0.0, 0.0, 200.0, 100.0),
            scale=1.0,
        )
        assert not series.plotted
        assert series.current == 10.0

    def test_the_axis_rounds_up_to_a_clean_unit(self) -> None:
        assert nice_ceiling(700_000) == 1_048_576
        assert nice_ceiling(1_048_576) == 1_048_576
        assert nice_ceiling(3_000_000) == 4_194_304
        assert nice_ceiling(0.0) == 1_048_576, "an empty graph is still scaled in MiB/s"

    def test_values_are_clamped_to_the_plot_area(self) -> None:
        series = map_series(
            [(0.0, 0.0), (1.0, 10.0)],
            name="download",
            colour="#4C8DFF",
            area=QRectF(10.0, 20.0, 100.0, 50.0),
            scale=1.0,
        )
        assert all(20.0 <= point.y() <= 70.0 for point in series.points)

    def test_the_peak_is_the_largest_sample_in_the_buffer(self, qapp: object) -> None:
        graph = RateGraph(DARK)
        graph.resize(400, 160)
        samples = [(float(index), float(index * 100_000)) for index in range(10)]
        graph.set_series(samples)
        download, _upload = graph.series()
        assert download.peak == pytest.approx(900_000)
        assert download.current == pytest.approx(900_000)

    def test_the_graph_draws_two_series(self, qapp: object) -> None:
        graph = RateGraph(DARK)
        graph.resize(400, 160)
        graph.set_series(
            [(float(index), float(index * 80_000)) for index in range(20)],
            [(float(index), float(index * 20_000)) for index in range(20)],
        )
        image = graph.grab().toImage()
        assert not image.isNull()
        colours = {
            image.pixelColor(x, y).name()
            for y in range(0, image.height(), 2)
            for x in range(0, image.width(), 2)
        }
        assert len(colours) >= 4, "grid, fill, download and upload are not one colour"

    def test_no_data_says_it_is_waiting(self, qapp: object) -> None:
        graph = RateGraph(DARK)
        graph.resize(400, 160)
        graph.clear()
        assert not graph.series()[0].plotted
        assert not graph.grab().isNull()


# ---------------------------------------------------------------------- motion


class TestMotion:
    """Animation that is bound to data, and switchable."""

    def test_a_pulse_decays(self) -> None:
        assert decay(1.0, 0.0) == 1.0
        assert decay(1.0, 0.35) == pytest.approx(0.5, abs=0.01)
        assert decay(1.0, 10.0) == 0.0

    def test_activity_is_scaled_by_a_stated_cap(self) -> None:
        assert activity_level(0.0, cap=1_000_000) == 0.0
        assert activity_level(500_000, cap=1_000_000) == 0.5
        assert activity_level(5_000_000, cap=1_000_000) == 1.0
        assert activity_level(100.0, cap=0.0) == 0.0, "no cap means no claim"

    def test_a_bank_remembers_only_live_pulses(self) -> None:
        bank = PulseBank()
        bank.stimulate("a", 1.0, now=0.0)
        bank.stimulate("b", 1.0, now=0.0)
        assert bank.level("a", now=0.0) == 1.0
        assert bank.level("a", now=10.0) == 0.0
        assert bank.prune(now=10.0) == 2
        assert len(bank) == 0

    def test_stimulation_never_lowers_a_pulse(self) -> None:
        bank = PulseBank()
        bank.stimulate("a", 1.0, now=0.0)
        bank.stimulate("a", 0.2, now=0.1)
        assert bank.level("a", now=0.1) > 0.2

    def test_a_peer_only_glows_when_it_moved_bytes(self) -> None:
        model = model_with(peers_for(0, 1))
        assert model.pulse_for("10.0.0.0:6881") == 0.0
        model.update(
            tuple(
                peer.__class__(**{**_fields(peer), "downloaded": peer.downloaded + 200_000})
                for peer in model.peers
            ),
            now=_now() + 1.0,
        )
        assert model.pulse_for("10.0.0.0:6881") > 0.0

    def test_reduced_motion_makes_fades_instant(self, qapp: object) -> None:
        from PySide6.QtWidgets import QWidget

        assert should_animate(reduced_motion=False)
        assert not should_animate(reduced_motion=True)
        assert duration_for(140, reduced_motion=False) == 140
        assert duration_for(140, reduced_motion=True) == 0

        host = QWidget()
        fader = Fader(host, reduced_motion=True)
        fader.to(0.0)
        assert fader.opacity == 0.0, "with motion reduced the value is applied at once"

    def test_easing_stays_in_range(self) -> None:
        assert ease_out_cubic(0.0) == 0.0
        assert ease_out_cubic(1.0) == 1.0
        assert ease_out_cubic(-1.0) == 0.0
        assert ease_out_cubic(2.0) == 1.0
        assert 0.0 < ease_out_cubic(0.5) < 1.0

    def test_the_ticker_runs_at_a_bounded_rate(self, qapp: object) -> None:
        ticks: list[int] = []
        ticker = Ticker(lambda: ticks.append(1), interval_ms=1)
        ticker.start()
        ticker.start()  # starting twice must not double the ticks
        ticker.stop()
        assert ticker.interval_ms == 16, "faster than one frame is a busy loop"
        assert not ticker.active
        assert ticks == []


# ---------------------------------------------------------------- interaction


def _move(widget: QWidget, x: float, y: float) -> None:
    """Send a real mouse-move to a widget, the way the window manager would."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(
        widget,
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(x, y),
            _no_button(),
            _no_button(),
            _no_modifier(),  # type: ignore[arg-type]
        ),
    )


def _press(widget: QWidget, x: float, y: float) -> None:
    """Send a real click to a widget."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(
        widget,
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(x, y),
            _left_button(),
            _left_button(),
            _no_modifier(),  # type: ignore[arg-type]
        ),
    )


def _no_button() -> object:
    from PySide6.QtCore import Qt

    return Qt.MouseButton.NoButton


def _left_button() -> object:
    from PySide6.QtCore import Qt

    return Qt.MouseButton.LeftButton


class TestPointerAndKeyboard:
    """The charts are looked at and pointed at: those paths must work too."""

    def test_pointing_at_a_peer_selects_it(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.resize(400, 400)
        canvas.set_view_model(model_with(peers_for(0, 1, 2)))
        node = canvas.nodes()[0]
        seen: list[SwarmNode] = []
        canvas.peer_hovered.connect(seen.append)
        _move(canvas, node.x, node.y)
        assert canvas.hovered is node
        assert seen == [node]

        chosen: list[SwarmNode] = []
        canvas.peer_selected.connect(chosen.append)
        _press(canvas, node.x, node.y)
        assert chosen == [node]

    def test_leaving_the_canvas_clears_the_hover(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.resize(400, 400)
        canvas.set_view_model(model_with(peers_for(0, 1, 2)))
        node = canvas.nodes()[0]
        _move(canvas, node.x, node.y)
        assert canvas.hovered is not None
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication

        QApplication.sendEvent(canvas, QEvent(QEvent.Type.Leave))
        assert canvas.hovered is None

    def test_the_canvas_replaces_its_swarm_when_resized(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.show()  # resize events are only delivered to a shown widget
        canvas.resize(400, 400)
        canvas.set_view_model(model_with(peers_for(0, 1, 2)))
        first = canvas.nodes()[0]
        canvas.resize(700, 500)
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        again = canvas.nodes()[0]
        assert again.x != first.x or again.y != first.y

    def test_the_canvas_stops_ticking_when_hidden(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.resize(400, 400)
        canvas.set_view_model(model_with(peers_for(0, 1, 2)))
        canvas.show()
        assert canvas._ticker.active
        canvas.hide()
        assert not canvas._ticker.active

    def test_the_glow_decays_on_its_own(self, qapp: object) -> None:
        # Nothing to redraw when nothing is glowing: a tick that repainted
        # anyway would be a busy loop with a chart attached.
        canvas = SwarmCanvas(DARK)
        canvas.resize(400, 400)
        canvas.set_view_model(model_with(peers_for(0, 1, 2)))
        assert canvas._on_tick() is None

    def test_clicking_a_piece_selects_it(self, qapp: object) -> None:
        matrix = PieceMatrix(DARK)
        matrix.resize(400, 160)
        matrix.set_piece_map(piece_map_of([0] * 30))
        cell = matrix.cells()[5]
        chosen: list[int] = []
        matrix.piece_selected.connect(chosen.append)
        _press(matrix, cell.x + 1, cell.y + 1)
        assert chosen == [5]
        assert matrix.cursor_index == 5

    def test_hovering_a_piece_names_it(self, qapp: object) -> None:
        matrix = PieceMatrix(DARK)
        matrix.resize(400, 160)
        matrix.set_piece_map(piece_map_of([2] * 30, availability=3))
        cell = matrix.cells()[2]
        seen: list[object] = []
        matrix.piece_hovered.connect(seen.append)
        _move(matrix, cell.x + 1, cell.y + 1)
        assert matrix.hovered is cell
        assert seen == [cell]

    def test_escape_clears_the_keyboard_cursor(self, qapp: object) -> None:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        matrix = PieceMatrix(DARK)
        matrix.resize(400, 160)
        matrix.set_piece_map(piece_map_of([0] * 30))
        matrix.set_cursor(4)
        matrix.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, _key("Escape"), _no_modifier())  # type: ignore[arg-type]
        )
        assert matrix.cursor_index is None

    def test_enter_chooses_the_piece_under_the_cursor(self, qapp: object) -> None:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        matrix = PieceMatrix(DARK)
        matrix.resize(400, 160)
        matrix.set_piece_map(piece_map_of([0] * 30))
        matrix.set_cursor(7)
        chosen: list[int] = []
        matrix.piece_selected.connect(chosen.append)
        matrix.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, _key("Return"), _no_modifier())  # type: ignore[arg-type]
        )
        assert chosen == [7]

    def test_the_graph_reads_out_the_hovered_sample(self, qapp: object) -> None:
        graph = RateGraph(DARK)
        graph.resize(400, 160)
        graph.set_series([(float(index), float(index * 10_000)) for index in range(12)])
        seen: list[object] = []
        graph.hovered.connect(seen.append)
        area = graph.plot_rect()
        _move(graph, area.left() + area.width() * 0.5, area.center().y())
        assert seen and seen[-1] is not None
        seconds_ago, down, up = seen[-1]  # type: ignore[misc]
        assert seconds_ago >= 0.0
        assert down > 0.0
        assert up is None, "no upload series was given, so none is reported"

    def test_leaving_the_graph_drops_the_crosshair(self, qapp: object) -> None:
        graph = RateGraph(DARK)
        graph.resize(400, 160)
        graph.set_series([(float(index), float(index * 10_000)) for index in range(12)])
        area = graph.plot_rect()
        _move(graph, area.left() + 10.0, area.center().y())
        assert graph._hover is not None
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication

        QApplication.sendEvent(graph, QEvent(QEvent.Type.Leave))
        assert graph._hover is None

    def test_the_graph_asks_for_a_sensible_size(self, qapp: object) -> None:
        graph = RateGraph(DARK)
        hint = graph.sizeHint()
        assert hint.width() >= 220
        assert hint.height() >= 96
        assert graph.minimumSizeHint().width() <= hint.width()


# --------------------------------------------------------------- M13: the window


class TestTheWindowTheChartsAgreeOn:
    """Five minutes, one sample a second, in every chart."""

    def test_the_view_models_take_their_window_from_the_design_system(self) -> None:
        # A graph that remembered a different window than the tokens describe
        # would be a second, quieter bug: two charts covering different
        # stretches of the same minute.
        model = TorrentViewModel()
        assert model.download_series is not None
        assert model._download.capacity == TOKENS.chart.max_points
        assert model._download.min_gap_seconds == TOKENS.chart.update_interval_ms / 1000

    def test_the_piece_history_covers_the_same_stretch(self) -> None:
        model = PiecesViewModel()
        assert model._history.capacity == TOKENS.chart.max_points
        assert model._history.min_gap_seconds == TOKENS.chart.update_interval_ms / 1000

    def test_five_minutes_of_samples_fit_exactly_once(self) -> None:
        model = TorrentViewModel()
        now = _now()
        for index in range(TOKENS.chart.max_points + 50):
            model.update_torrent(view_of_torrent(), now=now + index)
        assert len(model.download_series) == TOKENS.chart.max_points
        span = model.download_series[-1][0] - model.download_series[0][0]
        assert span == pytest.approx(TOKENS.chart.max_points - 1, abs=0.001), (
            "one sample per second, for five minutes"
        )

    def test_the_window_is_five_minutes_whatever_the_uptime(self) -> None:
        """The same rounding, seen from the chart rather than the buffer.

        The test above takes its base from ``time.monotonic()``, so whether it
        passed depended on how long the machine had been up: for about one base
        in sixty, one of the 350 on-time samples landed a float error inside the
        minimum gap, was dropped, and left the retained window a second wider
        than the tokens promise. These bases are the ones that did it.
        """
        for base in (9.9, 1925.8549559671494, 16324.469312085103):
            model = TorrentViewModel()
            for index in range(TOKENS.chart.max_points + 50):
                model.update_torrent(view_of_torrent(), now=base + index)

            series = model.download_series
            assert len(series) == TOKENS.chart.max_points, base
            span = series[-1][0] - series[0][0]
            assert span == pytest.approx(TOKENS.chart.max_points - 1, abs=0.001), (base, span)


def view_of_torrent() -> object:
    """A torrent view, built locally so the chart tests stay about charts."""
    from app.services.engine import TorrentState
    from app.services.torrent_service import TorrentView

    return TorrentView(
        info_hash="b" * 40,
        name="chart fixture",
        state=TorrentState.DOWNLOADING,
        progress=0.5,
        total_length=10_000_000,
        verified_pieces=5,
        missing_pieces=5,
        piece_count=10,
        port=6881,
        resumed=None,  # type: ignore[arg-type]
        metrics=None,
    )


# --------------------------------------------------------- M13: honest geometry


class TestGapsAndSizes:
    """What the charts refuse to invent."""

    def test_a_gap_in_the_recording_is_drawn_as_a_gap(self) -> None:
        # A paused client is not a client that slid gracefully to zero.
        samples = [(0.0, 100.0), (1.0, 110.0), (31.0, 120.0), (32.0, 130.0)]
        series = map_series(
            samples,
            name="download",
            colour="#4C8DFF",
            area=QRectF(0.0, 0.0, 200.0, 100.0),
            scale=1_048_576.0,
        )
        assert not series.continuous
        runs = _runs_of(series)
        assert len(runs) == 2
        assert len(runs[0]) == 2 and len(runs[1]) == 2

    def test_an_unbroken_recording_is_one_line(self) -> None:
        samples = [(float(index), float(index * 1000)) for index in range(10)]
        series = map_series(
            samples,
            name="download",
            colour="#4C8DFF",
            area=QRectF(0.0, 0.0, 200.0, 100.0),
            scale=1_048_576.0,
        )
        assert series.continuous
        assert series.span == pytest.approx(9.0)

    def test_the_axis_says_how_far_back_the_window_reaches(self, qapp: object) -> None:
        graph = RateGraph(DARK)
        graph.resize(400, 160)
        graph.set_series([(float(index), float(index * 10_000)) for index in range(120)])
        download, _upload = graph.series()
        assert download.span == pytest.approx(119.0)
        assert not graph.grab().isNull()

    def test_a_piece_knows_how_big_it_is(self) -> None:
        piece_map = PieceMap(
            piece_count=4,
            piece_length=262_144,
            total_length=262_144 * 3 + 1_000,
            states=bytes([3, 3, 3, 0]),
            availability=(1, 1, 1, 0),
            filled=(0.0, 0.0, 0.0, 0.0),
            counts={"verified": 3, "missing": 1, "requested": 0, "downloading": 0, "failed": 0},
        )
        assert piece_map.piece_size(0) == 262_144
        assert piece_map.piece_size(3) == 1_000, "the last piece is short, and says so"

    def test_the_matrix_shows_the_size_of_the_piece_you_point_at(self, qapp: object) -> None:
        matrix = PieceMatrix(DARK)
        matrix.resize(400, 160)
        matrix.set_piece_map(piece_map_of([2] * 8))
        cell = matrix.cells()[3]
        assert cell.bytes > 0
        assert "B" in _tooltip(cell), "the tooltip names the piece's size"


class TestClickToInspect:
    """Clicking a node answers 'what is that one, exactly?'."""

    def _tab(self) -> PeersTab:
        from app.ui.viewmodels.torrent_vm import TorrentViewModel

        model = TorrentViewModel()
        peers = peers_for(0, 1, 2)
        model.update_peers(tuple(peers))
        model.update_peers(tuple(peers))
        tab = PeersTab(model, DARK)
        tab.resize(900, 800)
        return tab

    def test_clicking_a_node_inspects_it(self, qapp: object) -> None:
        tab = self._tab()
        node = tab.canvas.nodes()[0]
        tab.canvas.peer_selected.emit(node)
        assert tab.inspector.peer is node.peer
        assert node.peer.label in tab.inspector.title.text()
        assert node.peer.client in tab.inspector.title.text()
        assert "holds 100/100 pieces" in tab.inspector.body.text()
        assert "down --" in tab.inspector.body.text(), "an unmeasured rate is not zero"

    def test_clicking_a_row_inspects_it(self, qapp: object) -> None:
        tab = self._tab()
        peer = tab.model.peer_at(1)
        assert peer is not None
        tab._on_row_clicked(tab.model.index(1, 0))
        assert tab.inspector.peer is peer

    def test_the_inspector_can_be_dismissed(self, qapp: object) -> None:
        tab = self._tab()
        tab.canvas.peer_selected.emit(tab.canvas.nodes()[0])
        assert tab.inspector.peer is not None
        tab.inspector.clear_button.click()
        assert tab.inspector.peer is None
        assert "Click a node" in tab.inspector.body.text()

    def test_the_inspector_re_reads_rates_on_refresh(self, qapp: object) -> None:
        tab = self._tab()
        node = tab.canvas.nodes()[0]
        tab.canvas.peer_selected.emit(node)
        before = tab.inspector.body.text()
        tab.refresh()
        assert tab.inspector.body.text() == before, "a refresh keeps the selection"

    def test_reduced_motion_stops_the_decay_clock(self, qapp: object) -> None:
        canvas = SwarmCanvas(DARK)
        canvas.show()
        canvas.set_view_model(model_with(peers_for(0, 1, 2)))
        assert canvas._ticker.active
        canvas.set_reduced_motion(True)
        assert not canvas._ticker.active, "a still canvas runs no timers"
        canvas.set_reduced_motion(False)
        assert canvas._ticker.active
