"""Design tokens: the numbers the interface is built from.

Spacing, type sizes, radii, durations and geometry live here and nowhere else,
so a widget never invents a margin and the whole application can be retuned by
editing one file. Everything is plain data — no Qt imports — so the tokens can
be read by tests, by the stylesheet builder, and by the charts without a
QApplication existing.

The scale is deliberately small: four spacing steps, four radii, six type
sizes. A design system with fifty values is a design system nobody follows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Spacing:
    """Spacing scale, in pixels."""

    xs: int = 6
    sm: int = 10
    md: int = 16
    lg: int = 24
    xl: int = 32
    xxl: int = 40


@dataclass(frozen=True, slots=True)
class Radii:
    """Corner radii for a sleek modern interface."""

    sm: int = 6
    md: int = 12
    lg: int = 18
    pill: int = 999


@dataclass(frozen=True, slots=True)
class Type:
    """Font sizes (points) and the families used for UI and data."""

    family: str = "Inter, Outfit, 'Segoe UI Variable Display', 'Segoe UI', -apple-system, sans-serif"
    mono: str = "'JetBrains Mono', 'Cascadia Code', SFMono-Regular, Consolas, monospace"

    display: int = 26
    title: int = 18
    heading: int = 14
    body: int = 11
    small: int = 10
    caption: int = 10
    micro: int = 9
    data: int = 11
    subtitle: int = 14


@dataclass(frozen=True, slots=True)
class Duration:
    """Animation durations, in milliseconds."""

    fast: int = 120
    normal: int = 220
    slow: int = 400


@dataclass(frozen=True, slots=True)
class Geometry:
    """Fixed sizes for the shell and its rows."""

    sidebar_width: int = 250
    topbar_height: int = 64
    icon: int = 20
    icon_large: int = 40
    control_height: int = 38
    content_margin: int = 24
    row_height: int = 76
    card_min_width: int = 180
    tab_height: int = 40
    scrollbar: int = 8


@dataclass(frozen=True, slots=True)
class Chart:
    """How the signature visualisations are drawn and how much they remember."""

    # The window the signature visuals show is the one the specification asks
    # for: five minutes of history at one sample per second. It is bounded — the
    # graphs keep a fixed number of samples and drop the oldest, for the same
    # reason the statistics history does — and it is *stated here* rather than
    # in each chart, because a graph that disagreed with the design system
    # about how much history it remembers would be a second, quieter bug.
    max_points: int = 300
    update_interval_ms: int = 1000
    # Repaints are capped, so a fast swarm cannot turn into a busy loop.
    max_fps: int = 20
    # How far a gap between samples may stretch before the graph stops drawing
    # a line across it: three missing samples is a stall, and connecting the
    # two sides would invent a slope the client never measured.
    max_gap_factor: float = 3.0

    peer_node_radius: int = 7
    self_node_radius: int = 13
    edge_width: float = 1.0
    piece_cell: int = 12
    piece_gap: int = 2
    graph_height: int = 132


@dataclass(frozen=True, slots=True)
class Tokens:
    """The whole design system."""

    spacing: Spacing = Spacing()
    radii: Radii = Radii()
    type: Type = Type()
    duration: Duration = Duration()
    geometry: Geometry = Geometry()
    chart: Chart = Chart()

    def px(self, step: str) -> int:
        """A spacing step by name, e.g. ``tokens.px("lg")`` gives 16.

        Raises:
            AttributeError: If there is no such step. A typo in a layout should
                be loud, not silently zero.
        """
        value: int = getattr(self.spacing, step)
        return value


TOKENS = Tokens()
"""The one instance every widget reads."""
