"""The theme: palettes, tokens, the stylesheet, and the code-drawn icons.

Two claims are being made here and both are worth pinning down:

* **The stylesheet is generated, not hand-written twice.** A second theme is a
  second :class:`Palette`, and the rules stay in one place.
* **Icons are drawn in code and cached.** No binary assets, no missing files at
  runtime, and a cache that does not return the same object for two colours.
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from app.ui.theme import DARK, LIGHT, icons, palette_for
from app.ui.theme.qss import build_stylesheet
from app.ui.theme.tokens import TOKENS, Geometry, Tokens
from PySide6.QtGui import QColor, QIcon

ALL_ICONS: tuple[str, ...] = (
    "overview",
    "library",
    "peers",
    "network",
    "dht",
    "logs",
    "settings",
    "plus",
    "play",
    "pause",
    "stop",
    "trash",
    "folder",
    "check",
    "alert",
    "search",
)


class TestPalette:
    def test_a_theme_name_resolves_to_a_palette(self) -> None:
        assert palette_for("dark") is DARK
        assert palette_for("light") is LIGHT

    def test_an_unknown_theme_falls_back_rather_than_breaking(self) -> None:
        assert palette_for("neon").name == DARK.name

    def test_every_state_has_a_colour(self) -> None:
        for state in ("missing", "requested", "downloading", "verified", "failed"):
            colour = DARK.state_colour(state)
            assert QColor(colour).isValid(), f"{state} has no valid colour"

    def test_state_colours_are_the_roles_the_dictionary_promises(self) -> None:
        assert DARK.state_colour("verified") == DARK.success
        assert DARK.state_colour("failed") == DARK.danger

    def test_with_alpha_keeps_the_colour(self) -> None:
        # Qt reads eight-digit hex as #AARRGGBB, so that is what comes out.
        faded = DARK.with_alpha(DARK.accent, 64)
        assert faded == f"#40{DARK.accent.lstrip('#')}"
        assert QColor(faded).alpha() == 64
        assert QColor(faded).toRgb().name() == DARK.accent.lower()

    def test_the_soft_accent_is_the_accent_with_alpha(self) -> None:
        # A palette is easy to write wrong here: #RRGGBBAA reads as a
        # completely different colour to Qt, not as a faint version of one.
        assert QColor(DARK.accent_soft).toRgb().name() == DARK.accent.lower()
        assert QColor(DARK.accent_soft).alpha() < 128

    def test_with_alpha_refuses_an_alpha_that_is_not_a_byte(self) -> None:
        with pytest.raises(ValueError, match="alpha"):
            DARK.with_alpha(DARK.accent, 256)

    def test_the_light_palette_is_the_same_roles_retinted(self) -> None:
        assert {field.name for field in fields(LIGHT)} == {field.name for field in fields(DARK)}
        assert LIGHT.text != DARK.text


class TestTokens:
    def test_the_scale_is_small_on_purpose(self) -> None:
        assert len(fields(TOKENS.spacing)) <= 8
        assert TOKENS.spacing.xs < TOKENS.spacing.sm < TOKENS.spacing.lg

    def test_a_step_can_be_read_by_name(self) -> None:
        assert TOKENS.px("lg") == TOKENS.spacing.lg

    def test_a_typo_is_loud(self) -> None:
        with pytest.raises(AttributeError):
            TOKENS.px("enormous")

    def test_charts_are_bounded(self) -> None:
        # Five minutes at one sample a second, as the specification asks, and
        # a repaint ceiling so a fast swarm cannot become a busy loop.
        assert TOKENS.chart.max_points * TOKENS.chart.update_interval_ms == 300_000
        assert TOKENS.chart.update_interval_ms == 1000
        assert TOKENS.chart.max_fps == 20

    def test_tokens_are_plain_data(self) -> None:
        # No Qt: the stylesheet builder and the charts read them without a
        # QApplication existing.
        assert isinstance(TOKENS, Tokens)
        assert isinstance(TOKENS.geometry, Geometry)


class TestStylesheet:
    def test_it_mentions_the_palette_it_was_built_from(self) -> None:
        sheet = build_stylesheet(DARK, TOKENS)
        assert DARK.background in sheet
        assert DARK.accent in sheet

    def test_it_styles_the_property_selectors_the_widgets_set(self) -> None:
        sheet = build_stylesheet(DARK, TOKENS)
        for selector in (
            '[kind="card"]',
            '[role="muted"]',
            '[role="success"]',
            "#sidebar",
            "#topbar",
            "#row",
            '[kind="pill"]',
        ):
            assert selector in sheet, f"{selector} is unstyled"

    def test_a_second_theme_is_a_second_palette_not_a_second_stylesheet(self) -> None:
        dark = build_stylesheet(DARK, TOKENS)
        light = build_stylesheet(LIGHT, TOKENS)
        assert DARK.accent in dark
        assert LIGHT.accent in light
        assert dark != light

    def test_the_sidebar_and_content_widths_agree_with_the_tokens(self) -> None:
        assert str(TOKENS.geometry.sidebar_width) in build_stylesheet(DARK, TOKENS)


class TestIcons:
    @pytest.mark.parametrize("name", ALL_ICONS)
    def test_every_icon_draws_something(self, name: str, qapp: object) -> None:
        icon = icons.icon(name, size=24)
        assert isinstance(icon, QIcon)
        assert not icon.isNull()
        assert icon.availableSizes(), f"{name} drew nothing"

    def test_the_cache_is_keyed_by_name_size_and_colour(
        self, qapp: object, icon_cache_is_cold: None
    ) -> None:
        first = icons.icon("play", size=18)
        assert icons.icon("play", size=18) is first
        assert icons.icon("play", size=32) is not first
        assert icons.icon("play", size=18, colour="#ff0000") is not first

    def test_clearing_the_cache_forgets_everything(
        self, qapp: object, icon_cache_is_cold: None
    ) -> None:
        first = icons.icon("pause", size=18)
        icons.clear_cache()
        assert icons.icon("pause", size=18) is not first

    def test_an_unknown_icon_name_is_an_error_not_a_blank(self, qapp: object) -> None:
        with pytest.raises(KeyError):
            icons.icon("hovercraft")
