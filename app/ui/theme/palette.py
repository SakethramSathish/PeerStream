"""Colour: the dark, technical palette the interface is painted in.

Roles, not raw colours, are what widgets ask for — ``palette.text_muted``, not
``#9AA4B2`` — so a light theme is a second instance of the same dataclass and
not a second stylesheet.

The direction is "dark technical cinematic": near-black surfaces, one cool
accent, restrained state colours, no neon, no gradients for their own sake.
State colours double as *meaning* in the charts (verified is green, failed is
red), which is why they are part of the palette rather than literals in a
paint method.

Peers and pieces get their own ramps because those two views are the project's
signature: they need to stay legible at six pixels across.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    """Every colour role the interface uses.

    Attributes:
        background: Application background, behind everything.
        surface: Cards and panels.
        surface_alt: Raised surfaces: inputs, headers, hovered rows.
        surface_hover: A row the pointer is over.
        border: Hairlines between surfaces.
        border_strong: Focus rings and emphasised dividers.
        text: Primary text.
        text_muted: Secondary text (labels, units).
        text_faint: Tertiary text (timestamps, hints).
        accent: The brand colour: selection, primary actions, downloading.
        accent_soft: Accent at low alpha, for fills behind accent strokes.
            Eight-digit colours are written the way Qt reads them,
            ``#AARRGGBB`` — the alpha byte first.
        success: Verified, seeding, healthy.
        warning: Requested, slow, degraded.
        danger: Failed, errored, destructive.
        info: Informational, unchoked-but-idle.
        piece_missing / piece_requested / piece_downloading / piece_verified /
            piece_failed: The five piece states (PRD §10.5).
        peer_seed / peer_leech / peer_connecting / peer_idle: Peer node colours.
        graph_grid: Chart gridlines.
        graph_fill: Area fill under a rate curve.
        shadow: Panel shadow.
    """

    name: str = "dark"

    background: str = "#111111"
    surface: str = "#1A1A1A"
    surface_alt: str = "#222222"
    surface_hover: str = "#2A2A2A"
    border: str = "#333333"
    border_strong: str = "#444444"

    text: str = "#F3F4F6"
    text_muted: str = "#9CA3AF"
    text_faint: str = "#6B7280"

    accent: str = "#10B981"
    accent_soft: str = "#2E10B981"
    success: str = "#34D399"
    warning: str = "#FBBF24"
    danger: str = "#F87171"
    info: str = "#D1D5DB"

    piece_missing: str = "#4B5563"
    piece_requested: str = "#FBBF24"
    piece_downloading: str = "#10B981"
    piece_verified: str = "#34D399"
    piece_failed: str = "#F87171"

    peer_seed: str = "#34D399"
    peer_leech: str = "#10B981"
    peer_connecting: str = "#FBBF24"
    peer_idle: str = "#6B7280"

    graph_grid: str = "#222222"
    graph_fill: str = "#3310B981"
    shadow: str = "#66000000"

    # ------------------------------------------------------------------ roles

    def state_colour(self, state: str) -> str:
        """The colour for a named piece or peer state.

        Unknown states are painted as missing rather than as something
        invented: a state we do not recognise is a state we cannot vouch for.
        """
        return {
            "missing": self.piece_missing,
            "requested": self.piece_requested,
            "downloading": self.piece_downloading,
            "verified": self.piece_verified,
            "failed": self.piece_failed,
            "seeding": self.peer_seed,
            "leeching": self.peer_leech,
            "connecting": self.peer_connecting,
            "idle": self.peer_idle,
            "ok": self.success,
            "warning": self.warning,
            "error": self.danger,
            "info": self.info,
        }.get(state, self.piece_missing)

    def with_alpha(self, colour: str, alpha: int) -> str:
        """``#AARRGGBB`` for a colour role and an alpha byte.

        Qt reads eight-digit hex with the alpha *first*, so that is the order
        this returns: ``with_alpha(palette.accent, 46)`` gives ``#2E4C8DFF``,
        which a stylesheet and a ``QPainter`` both understand.

        Args:
            colour: A role, with or without a leading ``#``, and with or
                without an alpha pair already on the end.
            alpha: ``0``-``255``.

        Raises:
            ValueError: If ``alpha`` does not fit in a byte.
        """
        if not 0 <= alpha <= 255:
            raise ValueError(f"alpha must be 0-255, got {alpha}")
        return f"#{alpha:02X}{colour.lstrip('#')[:6]}"


DARK = Palette()
"""The default theme (dark, technical, cinematic)."""

AMOLED = Palette(
    name="amoled",
    background="#000000",
    surface="#0A0A0A",
    surface_alt="#121212",
    surface_hover="#1A1A1A",
    border="#262626",
    border_strong="#333333",
    text="#FFFFFF",
    text_muted="#A3A3A3",
    text_faint="#737373",
    accent="#F59E0B",
    accent_soft="#2EF59E0B",
    success="#10B981",
    warning="#FBBF24",
    danger="#EF4444",
    info="#D4D4D4",
    piece_missing="#525252",
    piece_requested="#FBBF24",
    piece_downloading="#F59E0B",
    piece_verified="#10B981",
    piece_failed="#EF4444",
    peer_seed="#10B981",
    peer_leech="#F59E0B",
    peer_connecting="#FBBF24",
    peer_idle="#737373",
    graph_grid="#171717",
    graph_fill="#33F59E0B",
    shadow="#99000000",
)
"""AMOLED mode for true black screens."""

LIGHT = Palette(
    name="light",
    background="#F7F8FA",
    surface="#FFFFFF",
    surface_alt="#F1F3F6",
    surface_hover="#EAEDF2",
    border="#DDE1E7",
    border_strong="#C3C9D2",
    text="#14181D",
    text_muted="#5A6472",
    # Darkened from #838D9B (3.4:1). A light theme is not automatically a
    # legible one: every role below was measured, and the ones that failed were
    # moved until they passed rather than left as decoration.
    text_faint="#626C7A",
    accent="#1F62E5",
    accent_soft="#261F62E5",
    success="#197D4F",
    warning="#8D6815",
    danger="#D12E3A",
    info="#3472AC",
    piece_missing="#7D8DA3",
    piece_requested="#8D6815",
    piece_downloading="#1F62E5",
    piece_verified="#197D4F",
    piece_failed="#D12E3A",
    peer_seed="#197D4F",
    peer_leech="#1F62E5",
    peer_connecting="#8D6815",
    peer_idle="#838D9D",
    graph_grid="#E4E4E7",
    graph_fill="#1A6366F1",
    shadow="#14000000",
)
"""A light theme, because accessibility means letting people choose."""


def palette_for(name: str) -> Palette:
    """The palette named in the configuration. Unknown names give the dark one."""
    name = name.strip().lower()
    if name == "light":
        return LIGHT
    if name == "amoled":
        return AMOLED
    return DARK
