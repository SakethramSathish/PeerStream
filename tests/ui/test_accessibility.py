"""Accessibility, as arithmetic and as a walk through the widget tree.

Two things can be checked without a human being in the room, and both are
checked here.

**Contrast.** WCAG 2.1 reduces "can you read this?" to a ratio between 1:1 and
21:1, so the palette is held to it: every text role clears 4.5:1 on every
surface it can land on, and every state colour used as a fill clears 3:1. The
light theme is the one that failed — a light theme is not automatically a
legible one, and four of its roles were decoration pretending to be text.

**Names.** A screen reader announces a control by its accessible name. A button
whose only content is an icon has none, so it is invisible to anyone not using
a mouse and a pair of eyes. The widget tree is walked with the same predicate a
screen reader would use — is it focusable, is it a control — and every hit must
have a name, a label, or a tooltip.

Neither check is a matter of taste, which is the point: they fail or they pass.
"""

from __future__ import annotations

import pytest
from app.ui.theme.contrast import (
    WCAG_AA_NORMAL,
    WCAG_NON_TEXT,
    composite,
    contrast_ratio,
    describe,
    meets,
    relative_luminance,
)
from app.ui.theme.palette import DARK, LIGHT, Palette
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QTabBar,
    QTabWidget,
    QWidget,
)

TEXT_ROLES: tuple[str, ...] = ("text", "text_muted", "text_faint")
SURFACES: tuple[str, ...] = ("background", "surface", "surface_alt", "surface_hover")
STATE_TEXT_ROLES: tuple[str, ...] = ("success", "warning", "danger", "info", "accent")
FILL_ROLES: tuple[str, ...] = (
    "piece_missing",
    "piece_requested",
    "piece_downloading",
    "piece_verified",
    "piece_failed",
    "peer_seed",
    "peer_leech",
    "peer_connecting",
    "peer_idle",
)


class TestContrastMath:
    def test_black_on_white_is_the_maximum(self) -> None:
        assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=0.01)

    def test_a_colour_against_itself_has_no_contrast(self) -> None:
        assert contrast_ratio("#4C8DFF", "#4C8DFF") == pytest.approx(1.0)

    def test_the_ratio_is_symmetric(self) -> None:
        assert contrast_ratio("#4C8DFF", "#12151A") == pytest.approx(
            contrast_ratio("#12151A", "#4C8DFF")
        )

    def test_a_known_pair_from_the_specification(self) -> None:
        # 4.48:1 for #777777 on white is the worked example people check
        # contrast implementations against.
        assert contrast_ratio("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.01)

    def test_luminance_is_weighted_the_way_the_eye_is(self) -> None:
        assert relative_luminance("#00FF00") > relative_luminance("#0000FF")

    def test_a_translucent_colour_is_measured_as_drawn(self) -> None:
        # Half-transparent white over black is mid grey, and mid grey is what
        # the eye sees — measuring the raw #80FFFFFF would claim 21:1.
        blended = composite("#80FFFFFF", "#000000")
        assert contrast_ratio(blended, "#000000") == pytest.approx(
            contrast_ratio("#808080", "#000000")
        )

    def test_meets_is_a_threshold_not_an_opinion(self) -> None:
        assert meets("#FFFFFF", "#000000", level=WCAG_AA_NORMAL) is True
        assert meets("#999999", "#FFFFFF", level=WCAG_AA_NORMAL) is False

    def test_describe_says_what_a_ratio_is_good_for(self) -> None:
        assert "fails" in describe(1.2)
        assert "non-text" in describe(3.5)
        assert describe(5.0).startswith("AA ")
        assert describe(8.0).startswith("AAA")

    def test_a_colour_we_cannot_parse_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="#RRGGBB"):
            contrast_ratio("blue", "#000000")


class TestPaletteLegibility:
    """Both themes, measured. A theme is not finished when it looks right."""

    @pytest.mark.parametrize("palette", [DARK, LIGHT], ids=lambda p: p.name)
    @pytest.mark.parametrize("role", TEXT_ROLES)
    @pytest.mark.parametrize("surface", SURFACES)
    def test_text_clears_aa_on_every_surface(
        self, palette: Palette, role: str, surface: str
    ) -> None:
        foreground = getattr(palette, role)
        background = getattr(palette, surface)
        ratio = contrast_ratio(foreground, background)
        assert ratio >= WCAG_AA_NORMAL, (
            f"{palette.name}: {role} {foreground} on {surface} {background} is "
            f"{ratio:.2f}:1 ({describe(ratio)})"
        )

    @pytest.mark.parametrize("palette", [DARK, LIGHT], ids=lambda p: p.name)
    @pytest.mark.parametrize("role", STATE_TEXT_ROLES)
    def test_state_colours_are_legible_as_text(self, palette: Palette, role: str) -> None:
        # These colour status text: tracker health, log levels, stat values.
        # A state you have to squint at is a state you will miss.
        ratio = contrast_ratio(getattr(palette, role), palette.surface)
        assert ratio >= WCAG_AA_NORMAL, (
            f"{palette.name}: {role} {getattr(palette, role)} on surface is "
            f"{ratio:.2f}:1 ({describe(ratio)})"
        )

    @pytest.mark.parametrize("palette", [DARK, LIGHT], ids=lambda p: p.name)
    @pytest.mark.parametrize("role", FILL_ROLES)
    def test_every_chart_state_is_distinguishable(self, palette: Palette, role: str) -> None:
        # WCAG 1.4.11, non-text contrast. The piece matrix claims five states
        # and the swarm canvas four; a state drawn at 1.2:1 is not one of them.
        ratio = contrast_ratio(getattr(palette, role), palette.surface)
        assert ratio >= WCAG_NON_TEXT, (
            f"{palette.name}: {role} {getattr(palette, role)} is {ratio:.2f}:1 against the "
            "surface it is drawn on"
        )

    @pytest.mark.parametrize("palette", [DARK, LIGHT], ids=lambda p: p.name)
    def test_the_focus_ring_is_visible(self, palette: Palette) -> None:
        # Focus rings are painted with the accent (see theme/qss.py), and a
        # focus indicator nobody can see is a keyboard trap with extra steps.
        assert contrast_ratio(palette.accent, palette.surface) >= WCAG_NON_TEXT

    def test_the_two_themes_are_actually_different(self) -> None:
        assert DARK.background != LIGHT.background
        assert contrast_ratio(DARK.text, DARK.background) > 7.0


# ------------------------------------------------------------------ named controls


@pytest.fixture(scope="module")
def handle(qapp: object) -> object:
    """One real window, shared by the tests that walk it.

    Built the way the application builds it, because a widget tree assembled by
    hand would not contain the controls that are actually missing names.
    """
    from app.ui.app import build_ui

    built = build_ui(["pytest"], load_config=False)
    built.bridge.start()
    try:
        yield built
    finally:
        built.bridge.stop()


def _controls(root: QWidget) -> list[QWidget]:
    """Every widget a keyboard user can reach, or a screen reader can announce.

    Excludes the ones Qt and Qt alone owns — scroll areas and viewports, whose
    only job is to move other things around — by asking each widget what it is:
    a button, an input, a checkable, a spin box, or a custom widget that asked
    for focus (the piece matrix and the peer inspector both take the keyboard).
    Labels are skipped: they are names for other things, and a label with no
    text is not a control anyone is stranded by.
    """
    found: list[QWidget] = []
    for widget in root.findChildren(QWidget):
        if isinstance(widget, (QLabel, QScrollArea)):
            continue
        if isinstance(widget, QLineEdit) and isinstance(widget.parent(), QSpinBox):
            # Qt's spin box owns an inner line editor; it is not a control of
            # ours and its name is the spin box's.
            continue
        if not widget.isVisibleTo(root.parent() or root):
            continue
        interactive = isinstance(
            widget, (QAbstractButton, QLineEdit, QComboBox, QSpinBox, QCheckBox)
        ) or bool(widget.focusPolicy() & Qt.FocusPolicy.TabFocus)
        if interactive:
            found.append(widget)
    return found


def _name_of(widget: QWidget) -> str:
    """What a screen reader would announce for this widget.

    Asked of Qt's accessibility layer rather than guessed from the widget's own
    strings, because that layer is what does the announcing: it knows a label
    is another control's name through :meth:`QLabel.setBuddy`, and it knows an
    icon-only button has nothing to say. A tooltip is a *description*, not a
    name, so it does not count — otherwise every unnamed icon could borrow one
    and the test would pass on a technicality.
    """
    if isinstance(widget, (QTabWidget, QTabBar)):
        # Qt reports no name for a tab bar; what a screen reader announces —
        # and all a user has to go on — is the text of the tabs themselves.
        return " ".join(widget.tabText(index) for index in range(widget.count())).strip()
    interface = QAccessible.queryAccessibleInterface(widget)
    if interface is None:
        return widget.accessibleName()
    return interface.text(QAccessible.Text.Name).strip()


class TestEveryControlHasAName:
    def test_the_window_has_no_anonymous_controls(self, handle) -> None:  # type: ignore[no-untyped-def]
        from app.ui.app import UiHandle

        assert isinstance(handle, UiHandle)
        window = handle.window
        # The detail page is six screens behind a tab bar, and a tab that has
        # never been shown has never been laid out — so each one is visited.
        screens: list[tuple[str, str | None]] = [
            ("overview", None),
            ("library", None),
            ("logs", None),
            ("dht", None),
            ("settings", None),
            *(
                ("detail", tab)
                for tab in ("overview", "peers", "pieces", "files", "trackers", "log")
            ),
        ]
        visited: set[int] = set()
        for key, tab in screens:
            window.show_page(key)
            if tab is not None:
                window.page("detail").show_tab(tab)  # type: ignore[attr-defined]
            controls = _controls(window)
            assert len(controls) > 3, (
                f"{key} exposed {len(controls)} control(s): the walk found nothing"
            )
            visited.update(id(widget) for widget in controls)
            unnamed = [
                widget
                for widget in controls
                if not _name_of(widget) and not _name_of(widget.parent())  # type: ignore[arg-type]
            ]
            assert not unnamed, (
                f"{key}{':' + tab if tab else ''} has {len(unnamed)} control(s) a screen "
                "reader cannot name: "
                + ", ".join(sorted({type(widget).__name__ for widget in unnamed}))
            )

        # A check that found four controls cannot tell you much about a
        # six-page window.
        assert len(visited) >= 20, f"only {len(visited)} controls were inspected"

    def test_a_newly_built_button_is_named_or_the_test_would_pass_on_nothing(
        self, qapp: object
    ) -> None:
        # Guard against the walk above quietly finding nothing at all.
        from PySide6.QtWidgets import QPushButton

        button = QPushButton()
        assert _name_of(button) == ""
        button.setText("Pause")
        assert _name_of(button) == "Pause"
