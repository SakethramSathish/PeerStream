"""The swarm canvas (PRD §36 / TRD §36): the swarm, seen from where we sit.

You are the node in the middle. Everyone else is on a ring around you: seeds
green, leeches blue, peers still handshaking amber, peers we know of but have
not reached drawn as hollow rings on the outer circle. Lines run from you to
each peer you are talking to, and a line thickens and brightens while bytes are
actually moving over it, then decays.

Three promises this canvas keeps, because a swarm display is easy to make
pretty and easy to make dishonest:

* **Position is stable, not animated.** A peer's angle comes from a hash of its
  address, so peers do not slide around every time the set changes. If a node
  moves, it is because a peer appeared or left, and you can see that happen.
* **Every visual variable is measured.** Node size is how much of the torrent
  the peer holds; colour is what it is; line width and glow are the rate we
  measured over the last interval, decaying when it stops. Nothing pulses on a
  timer alone.
* **Candidates count.** Peers the tracker told us about are drawn even though
  we are not connected to them, because a swarm is not just the peers that
  happened to answer. Their rings are hollow so "known" never reads as
  "working".

The placement is a pure function (:func:`place_nodes`) so the mapping can be
tested without a window; the widget is only the paint and the pointer.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import (
    QEvent,
    QPointF,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QHideEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRadialGradient,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from app.services.torrent_service import PeerView
from app.ui.animations.transitions import Ticker
from app.ui.format import human_bytes, human_rate
from app.ui.theme.palette import Palette
from app.ui.theme.tokens import TOKENS, Tokens
from app.ui.viewmodels.peers_vm import PeerActivity, PeersViewModel

# How far out the rings sit, as a fraction of the smaller side of the canvas.
CONNECTED_RING: float = 0.30
CANDIDATE_RING: float = 0.44

# A node holding 0 % of the torrent is still visible; a seed is 1.5x the base.
MIN_NODE_SCALE: float = 0.70
MAX_NODE_SCALE: float = 1.50

# The floor for edge scaling: below this rate an edge is drawn at its thinnest,
# so "connected and idle" stays distinguishable from "not connected".
RATE_FLOOR: float = 8192.0

# How far a glow reaches, as a multiple of the node's radius.
GLOW_REACH: float = 1.9


@dataclass(frozen=True, slots=True)
class SwarmNode:
    """One peer, placed.

    Attributes:
        peer: The measurement it was built from.
        activity: What it did over the last interval.
        x / y: Centre of the node, in widget pixels.
        radius: Drawn radius, before any glow.
        role: ``"seed"``, ``"leech"``, ``"connecting"`` or ``"candidate"``.
        edge: ``0.0``-``1.0``, how thick the line to the centre should be.
        glow: ``0.0``-``1.0``, how brightly it should bloom.
    """

    peer: PeerView
    activity: PeerActivity
    x: float
    y: float
    radius: float
    role: str
    edge: float
    glow: float

    @property
    def connected(self) -> bool:
        """Whether we are talking to it, as opposed to merely knowing it."""
        return self.role != "candidate"

    def centre(self) -> QPointF:
        """The node's centre as a point."""
        return QPointF(self.x, self.y)

    def contains(self, point: QPointF, *, slack: float = 2.0) -> bool:
        """Whether a point is on this node, with a small grab margin."""
        reach = self.radius + slack
        return (point.x() - self.x) ** 2 + (point.y() - self.y) ** 2 <= reach * reach

    def as_dict(self) -> dict[str, float | str]:
        """The node as plain data: how the tests read the mapping."""
        return {
            "key": self.peer.key,
            "role": self.role,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "radius": round(self.radius, 2),
            "edge": round(self.edge, 3),
            "glow": round(self.glow, 3),
        }


class SwarmCanvas(QWidget):
    """The swarm around one torrent, drawn from measurements.

    Args:
        palette: Colours.
        design: Spacing, type sizes, chart geometry.
        reduced_motion: When true, node glows do not bloom; the data is
            identical, only the decoration is dropped.
        parent: Qt parent.
    """

    peer_hovered = Signal(object)
    """The :class:`SwarmNode` under the pointer, or ``None`` when it leaves."""

    peer_selected = Signal(object)
    """The :class:`SwarmNode` that was clicked."""

    def __init__(
        self,
        palette: Palette,
        design: Tokens | None = None,
        *,
        reduced_motion: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._design = design or TOKENS
        self._reduced_motion = reduced_motion
        self._view_model: PeersViewModel | None = None
        self._our_progress: float = 0.0
        self._hovered: SwarmNode | None = None
        self._nodes: tuple[SwarmNode, ...] = ()
        self._ticker = Ticker(self._on_tick, interval_ms=1000 // max(1, self._design.chart.max_fps))
        self._dirty: bool = False  # True when data changed and a repaint is needed
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(220, 200)
        self.setToolTip("")

    # ------------------------------------------------------------------- input

    def set_view_model(self, view_model: PeersViewModel | None) -> None:
        """Follow this torrent's swarm. Re-places the nodes and repaints."""
        self._view_model = view_model
        self._relayout()
        self._dirty = True
        self.update()
        # Restart the decay clock so glows animate on new data.
        if not self._reduced_motion and self.isVisible():
            self._ticker.start()

    def set_our_progress(self, value: float) -> None:
        """How much of the torrent we hold, ``0.0``-``1.0``, for the centre ring."""
        new = min(1.0, max(0.0, value))
        if new != self._our_progress:
            self._our_progress = new
            self._dirty = True
            self.update()

    def set_reduced_motion(self, value: bool) -> None:
        """Motion is a preference, and re-readable at any time.

        With motion reduced the glows are not drawn *and* the decay clock is
        stopped. A timer that ticked to animate nothing would still be a timer
        running in a window the user asked to keep still.
        """
        self._reduced_motion = bool(value)
        if self._reduced_motion or not self.isVisible():
            self._ticker.stop()
        else:
            self._ticker.start()
        self.update()

    def set_palette(self, palette: Palette) -> None:
        """Recolour, for when the theme changes."""
        self._palette = palette
        self.update()

    # ------------------------------------------------------------------ layout

    def nodes(self) -> tuple[SwarmNode, ...]:
        """Every node, placed. This is the mapping the tests read."""
        return self._nodes

    def node_at(self, point: QPointF) -> SwarmNode | None:
        """The node under a point, or ``None``. Topmost wins."""
        for node in reversed(self._nodes):
            if node.contains(point):
                return node
        return None

    def node_for(self, key: str) -> SwarmNode | None:
        """The node for one peer, by its ``"host:port"`` key."""
        for node in self._nodes:
            if node.peer.key == key:
                return node
        return None

    @property
    def hovered(self) -> SwarmNode | None:
        """The node the pointer is over."""
        return self._hovered

    def _relayout(self) -> None:
        """Place every peer again, keeping the hovered one hovered."""
        if self._view_model is None or self.width() <= 0 or self.height() <= 0:
            self._nodes = ()
            self._hovered = None
            return
        self._nodes = place_nodes(
            self._view_model.peers,
            self._view_model,
            width=float(self.width()),
            height=float(self.height()),
            design=self._design,
        )
        self._hovered = None if self._hovered is None else self.node_for(self._hovered.peer.key)

    # ----------------------------------------------------------------- drawing

    def sizeHint(self) -> QSize:
        """A canvas that wants to be looked at."""
        return QSize(360, 320)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw the swarm: edges, peers, us, then the hover and the legend."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        centre = QPointF(self.width() / 2.0, self.height() / 2.0)

        if not self._nodes:
            self._paint_empty(painter, centre)
            return

        base = float(self._design.chart.peer_node_radius)
        self_radius = float(self._design.chart.self_node_radius)

        for node in self._nodes:
            if node.role == "candidate":
                self._paint_candidate(painter, node)
                continue
            self._paint_edge(painter, centre, node, self_radius)
        for node in self._nodes:
            if node.role != "candidate":
                self._paint_node(painter, node)

        self._paint_self(painter, centre, self_radius)
        if self._hovered is not None:
            self._paint_hover(painter, self._hovered, base)
        self._paint_legend(painter)

    def _paint_empty(self, painter: QPainter, centre: QPointF) -> None:
        """Say what is missing rather than drawing an empty ring."""
        palette = self._palette
        font = QFont(self.font())
        font.setPixelSize(self._design.type.body)
        painter.setFont(font)
        painter.setPen(QColor(palette.text_muted))
        painter.drawText(
            QRectF(centre.x() - 160.0, centre.y() - 26.0, 320.0, 20.0),
            Qt.AlignmentFlag.AlignCenter,
            "No peers yet",
        )
        smaller = QFont(font)
        smaller.setPixelSize(max(9, self._design.type.body - 2))
        painter.setFont(smaller)
        painter.setPen(QColor(palette.with_alpha(palette.text_muted, 170)))
        painter.drawText(
            QRectF(centre.x() - 200.0, centre.y() - 4.0, 400.0, 18.0),
            Qt.AlignmentFlag.AlignCenter,
            "Waiting for a tracker or a handshake",
        )

    def _paint_edge(
        self,
        painter: QPainter,
        centre: QPointF,
        node: SwarmNode,
        self_radius: float,
    ) -> None:
        """The link between us and one peer.

        Width is the rate we measured; colour is whether it is serving us. A
        choked peer is drawn grey even if it holds everything, because a seed
        that will not send is not helping.
        """
        palette = self._palette
        idle = node.peer.choking_us and not node.peer.interested_in_us
        colour = QColor(palette.peer_idle if idle else palette.peer_leech)
        alive = max(node.edge, node.glow)
        alpha = int(60 + 165 * max(0.35, alive))
        painter.setPen(
            QPen(
                _tinted(colour, alpha),
                self._design.chart.edge_width * (1.0 + 2.4 * node.edge),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        start = _along(centre, node.centre(), self_radius + 1.0)
        end = _along(node.centre(), centre, node.radius + 1.0)
        painter.drawLine(start, end)

    def _paint_node(self, painter: QPainter, node: SwarmNode) -> None:
        """One peer: glow, body, and a green ring if it is serving us."""
        palette = self._palette
        fill = QColor(_role_colour(palette, node.role))
        centre = node.centre()

        if node.glow > 0.0 and not self._reduced_motion:
            glow = QRadialGradient(centre, node.radius * GLOW_REACH)
            glow.setColorAt(0.0, _tinted(fill, int(120 * node.glow)))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(centre, node.radius * GLOW_REACH, node.radius * GLOW_REACH)

        painter.setPen(QPen(_tinted(fill, 255), 1.2, Qt.PenStyle.SolidLine))
        painter.setBrush(_tinted(fill, 150 if node.role == "connecting" else 220))
        painter.drawEllipse(centre, node.radius, node.radius)

        if not node.peer.choking_us and node.peer.state == "connected":
            painter.setPen(QPen(QColor(palette.success), 1.4, Qt.PenStyle.SolidLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, node.radius + 3.0, node.radius + 3.0)

    def _paint_candidate(self, painter: QPainter, node: SwarmNode) -> None:
        """A peer we know of but are not talking to: hollow, outer ring."""
        palette = self._palette
        painter.setPen(
            QPen(QColor(palette.with_alpha(palette.peer_idle, 200)), 1.0, Qt.PenStyle.SolidLine)
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(node.centre(), node.radius, node.radius)

    def _paint_self(self, painter: QPainter, centre: QPointF, radius: float) -> None:
        """Us: a disc with the completion arc around it."""
        palette = self._palette
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette.surface_alt))
        painter.drawEllipse(centre, radius, radius)

        ring = radius + 4.0
        painter.setPen(QPen(QColor(palette.border), 2.5, Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, ring, ring)

        if self._our_progress > 0.0:
            painter.setPen(QPen(QColor(palette.accent), 2.5, Qt.PenStyle.SolidLine))
            painter.drawArc(
                QRectF(centre.x() - ring, centre.y() - ring, ring * 2, ring * 2),
                90 * 16,
                int(-self._our_progress * 360 * 16),
            )

        font = QFont(self.font())
        font.setPixelSize(max(8, self._design.type.body - 3))
        painter.setFont(font)
        painter.setPen(QColor(palette.text))
        painter.drawText(
            QRectF(centre.x() - radius, centre.y() - 7.0, radius * 2, 14.0),
            Qt.AlignmentFlag.AlignCenter,
            f"{self._our_progress * 100:.0f}%",
        )

    def _paint_hover(self, painter: QPainter, node: SwarmNode, base: float) -> None:
        """A ring and a caption for the node under the pointer."""
        palette = self._palette
        painter.setPen(QPen(QColor(palette.text), 1.2, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(node.centre(), node.radius + 5.0, node.radius + 5.0)

        caption = _caption(node)
        font = QFont(self.font())
        font.setPixelSize(max(9, self._design.type.body - 2))
        painter.setFont(font)
        wide = float(painter.fontMetrics().horizontalAdvance(caption)) + 12.0
        left = min(max(node.x - wide / 2.0, 2.0), float(self.width()) - wide - 2.0)
        top = node.y + node.radius + 8.0
        if top + 18.0 > float(self.height()):
            top = node.y - node.radius - 24.0
        box = QRectF(left, max(0.0, top), wide, 18.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette.with_alpha(palette.surface_alt, 240)))
        painter.drawRoundedRect(box, 4.0, 4.0)
        painter.setPen(QColor(palette.text))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, caption)

    def _paint_legend(self, painter: QPainter) -> None:
        """What the colours mean, in words, along the bottom."""
        palette = self._palette
        font = QFont(self.font())
        font.setPixelSize(max(8, self._design.type.body - 3))
        painter.setFont(font)
        entries = (
            (palette.peer_seed, "seed"),
            (palette.peer_leech, "leech"),
            (palette.peer_connecting, "handshake"),
            (palette.peer_idle, "known"),
        )
        x = 10.0
        y = float(self.height()) - 10.0
        for colour, label in entries:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawEllipse(QPointF(x + 3.0, y - 4.0), 3.0, 3.0)
            painter.setPen(QColor(palette.text_muted))
            painter.drawText(
                QRectF(x + 10.0, y - 12.0, 70.0, 14.0), Qt.AlignmentFlag.AlignVCenter, label
            )
            x += 10.0 + float(painter.fontMetrics().horizontalAdvance(label)) + 12.0

    # ------------------------------------------------------------------ events

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Track the pointer and say what it is over."""
        found = self.node_at(QPointF(event.position()))
        if found is not self._hovered:
            self._hovered = found
            self.peer_hovered.emit(found)
            if found is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(event.globalPosition().toPoint(), _tooltip(found), self)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """Clear the hover when the pointer goes."""
        if self._hovered is not None:
            self._hovered = None
            self.peer_hovered.emit(None)
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Select the node under the pointer."""
        found = self.node_at(QPointF(event.position()))
        if found is not None:
            self.peer_selected.emit(found)
        super().mousePressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-place the swarm when the canvas changes size."""
        self._relayout()
        super().resizeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        """Start the decay clock only while visible: hidden canvases cost nothing."""
        self._ticker.start()
        super().showEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self._ticker.stop()
        super().hideEvent(event)

    def _on_tick(self) -> None:
        """Decay the glows. Auto-stops when all pulses have fully faded."""
        if self._view_model is None or not self._nodes:
            self._ticker.stop()  # nothing to animate — save the CPU
            return
        any_active = any(
            self._view_model.pulse_for(node.peer.key) > 0.005 for node in self._nodes
        )
        if not any_active:
            # All pulses decayed — stop firing until new data arrives.
            self._ticker.stop()
            return
        self._relayout()
        self.update()


# ---------------------------------------------------------------------- mapping


def place_nodes(
    peers: Sequence[PeerView],
    view_model: PeersViewModel,
    *,
    width: float,
    height: float,
    design: Tokens | None = None,
) -> tuple[SwarmNode, ...]:
    """Place a swarm: the whole mapping, with no widget involved.

    Connected peers go on the inner ring, candidates on the outer one. A peer's
    angle is a hash of its address, so the same swarm always draws the same
    picture and a peer that stays stays put. Node radius comes from how much of
    the torrent it holds; edge width from the rate we measured.

    Args:
        peers: The peers to place.
        view_model: Source of per-peer activity and pulse levels.
        width / height: Canvas size in pixels.
        design: Chart geometry.

    Returns:
        One node per peer, connected peers first.
    """
    chart = (design or TOKENS).chart
    centre_x = width / 2.0
    centre_y = height / 2.0
    span = min(width, height)
    base = float(chart.peer_node_radius)

    # The busiest peer sets the scale: "thick" means "the fastest thing
    # happening right now", not some absolute we invented.
    fastest = max((view_model.activity_for(peer.key).rate for peer in peers), default=0.0)
    cap = max(RATE_FLOOR, fastest)

    nodes: list[SwarmNode] = []
    connected = [peer for peer in peers if peer.state == "connected"]
    candidates = [peer for peer in peers if peer.state != "connected"]

    for peer in connected:
        activity = view_model.activity_for(peer.key)
        angle = _angle_for(peer.key)
        radius = span * CONNECTED_RING
        nodes.append(
            SwarmNode(
                peer=peer,
                activity=activity,
                x=centre_x + radius * math.cos(angle),
                y=centre_y + radius * math.sin(angle),
                radius=base * _scale_for(peer),
                role=_role_of(peer),
                edge=_level(activity.rate, cap),
                glow=view_model.pulse_for(peer.key),
            )
        )

    for peer in candidates:
        angle = _angle_for(peer.key)
        radius = span * CANDIDATE_RING
        nodes.append(
            SwarmNode(
                peer=peer,
                activity=PeerActivity(),
                x=centre_x + radius * math.cos(angle),
                y=centre_y + radius * math.sin(angle),
                radius=base * MIN_NODE_SCALE,
                role="candidate",
                edge=0.0,
                glow=0.0,
            )
        )
    return tuple(nodes)


def _angle_for(key: str) -> float:
    """A stable angle for a peer, from a hash of its address.

    Peers must not drift when the set changes, so the angle depends on the
    address alone: a peer keeps its place on the ring for as long as it is in
    the swarm.
    """
    digest = zlib.crc32(key.encode("utf-8", "replace"))
    return (digest % 3600) / 3600.0 * math.tau


def _scale_for(peer: PeerView) -> float:
    """Node size from how much of the torrent the peer holds."""
    return MIN_NODE_SCALE + (MAX_NODE_SCALE - MIN_NODE_SCALE) * peer.share


def _level(rate: float, cap: float) -> float:
    """How thick an edge should be, ``0.0``-``1.0``."""
    if cap <= 0.0:
        return 0.0
    return min(1.0, max(0.0, rate / cap))


def _role_of(peer: PeerView) -> str:
    """What a peer is, which decides its colour."""
    if peer.state != "connected":
        return "connecting"
    return "seed" if peer.complete else "leech"


def _role_colour(palette: Palette, role: str) -> str:
    """The colour for a role. Unknown roles are grey, never black."""
    return {
        "seed": palette.peer_seed,
        "leech": palette.peer_leech,
        "connecting": palette.peer_connecting,
        "candidate": palette.peer_idle,
    }.get(role, palette.peer_idle)


def _tinted(colour: QColor, alpha: int) -> QColor:
    """A copy of ``colour`` at a stated alpha."""
    tinted = QColor(colour)
    tinted.setAlpha(max(0, min(255, alpha)))
    return tinted


def _along(start: QPointF, end: QPointF, inset: float) -> QPointF:
    """A point ``inset`` pixels along the line from ``start`` to ``end``."""
    dx = end.x() - start.x()
    dy = end.y() - start.y()
    length = math.hypot(dx, dy)
    if length <= inset:
        return QPointF(start)
    return QPointF(start.x() + dx / length * inset, start.y() + dy / length * inset)


def _caption(node: SwarmNode) -> str:
    """The short label drawn under a hovered node."""
    return f"{node.peer.client} · {human_rate(node.activity.down_rate)}"


def _tooltip(node: SwarmNode) -> str:
    """The full measurement, for the tooltip."""
    peer = node.peer
    lines = [
        f"{peer.client}  ({peer.label})",
        f"holds {peer.pieces_held}/{peer.piece_count} pieces ({peer.share * 100:.0f}%)",
        f"down {human_rate(node.activity.down_rate)}   up {human_rate(node.activity.up_rate)}",
        f"got {human_bytes(peer.downloaded)}   sent {human_bytes(peer.uploaded)}",
    ]
    state = "choking us" if peer.choking_us else "serving us"
    if peer.interested_in_us:
        state += " · wants our pieces"
    lines.append(state)
    if peer.latency_ms is not None:
        lines.append(f"latency {peer.latency_ms:.0f} ms")
    if peer.source:
        lines.append(f"found via {peer.source}")
    return "\n".join(lines)
