"""Navigation sidebar: where you are, and where you could be.

The sidebar is a list of destinations, not a list of tabs. Each entry carries an
icon, a label, and an optional badge — a count the interface can actually back
up, like the number of active torrents. A badge is never shown for something the
client has not counted.

The active entry is marked with an accent pill on its left edge. Keyboard users
get the same destinations, because a client that cannot be driven without a
mouse is only half a client.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import icons
from app.ui.theme.tokens import TOKENS

# The sidebar is fixed width: the content column is what needs the room.
SIDEBAR_WIDTH: int = TOKENS.geometry.sidebar_width


class _NavButton(QPushButton):
    """One destination. Clean, full width, with an accent indicator when selected."""

    def __init__(self, key: str, label: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.label_text = label
        self._badge = 0
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIcon(icons.icon(icon_name, size=TOKENS.geometry.icon))
        self.setIconSize(QSize(TOKENS.geometry.icon, TOKENS.geometry.icon))
        self.setMinimumHeight(38)
        self._render()

    def set_badge(self, count: int) -> None:
        """Show a count, or nothing when there is nothing to count."""
        count = max(0, int(count))
        if count == self._badge:
            return
        self._badge = count
        self._render()

    def _render(self) -> None:
        suffix = f"    {self._badge}" if self._badge else ""
        self.setText(f"  {self.label_text}{suffix}")


class Sidebar(QFrame):
    """The navigation column.

    Args:
        items: ``(key, label, icon name)`` for each destination.
        parent: Qt parent.
    """

    navigated = Signal(str)
    """Emitted with the key of the destination the user chose."""

    def __init__(
        self,
        items: Sequence[tuple[str, str, str]] = (),
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        spacing = TOKENS.spacing

        column = QVBoxLayout(self)
        column.setContentsMargins(spacing.sm, spacing.lg, spacing.sm, spacing.lg)
        column.setSpacing(spacing.xs)

        brand_container = QFrame(self)
        brand_row = QHBoxLayout(brand_container)
        brand_row.setContentsMargins(spacing.xs, 0, spacing.xs, spacing.sm)
        brand_row.setSpacing(spacing.sm)

        brand_logo = QLabel("✦")
        brand_logo.setProperty("role", "accent")
        brand_logo.setStyleSheet(f"font-size: {TOKENS.type.heading}pt; font-weight: 800;")
        brand_row.addWidget(brand_logo)

        brand = QLabel("PEERSTREAM")
        brand.setProperty("role", "strong")
        brand.setStyleSheet(
            f"letter-spacing: 2px; font-size: {TOKENS.type.heading}pt; font-weight: 800;"
        )
        brand_row.addWidget(brand)
        brand_row.addStretch(1)

        version_pill = QLabel("v0.1")
        version_pill.setProperty("kind", "pill")
        version_pill.setStyleSheet("font-size: 8pt; padding: 1px 6px;")
        brand_row.addWidget(version_pill)

        column.addWidget(brand_container)

        self._buttons: dict[str, _NavButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for key, label, icon_name in items:
            button = _NavButton(key, label, icon_name, self)
            self._buttons[key] = button
            self._group.addButton(button)
            column.addWidget(button)

        column.addStretch(1)

        footer_card = QFrame(self)
        footer_card.setProperty("kind", "inset")
        footer_layout = QHBoxLayout(footer_card)
        footer_layout.setContentsMargins(spacing.md, spacing.xs, spacing.md, spacing.xs)
        self._footer = QLabel("")
        self._footer.setProperty("role", "faint")
        self._footer.setWordWrap(True)
        self._footer.setStyleSheet(f"font-size: {TOKENS.type.caption}pt;")
        footer_layout.addWidget(self._footer)
        column.addWidget(footer_card)

        self._group.buttonClicked.connect(self._on_clicked)

    # ----------------------------------------------------------------- content

    @property
    def current(self) -> str | None:
        """The key of the selected destination, or ``None`` before one is chosen."""
        checked = self._group.checkedButton()
        return None if checked is None else str(checked.key)  # type: ignore[attr-defined]

    def set_current(self, key: str) -> None:
        """Select a destination without emitting :attr:`navigated`."""
        button = self._buttons.get(key)
        if button is None:
            raise KeyError(f"no such destination: {key!r}")
        button.setChecked(True)

    def set_badges(self, counts: Mapping[str, int]) -> None:
        """Update several counts at once. Unknown and missing keys are ignored."""
        for key, count in counts.items():
            if key in self._buttons:
                self._buttons[key].set_badge(count)

    def set_footer(self, text: str) -> None:
        """Small print at the bottom: version, session directory, and so on."""
        self._footer.setText(text)

    def _on_clicked(self, button: object) -> None:
        self.navigated.emit(str(button.key))  # type: ignore[attr-defined]

    def connect_shortcuts(self, on_navigate: Callable[[str], None]) -> None:
        """Wire ``navigated`` to a slot. A convenience so callers read plainly."""
        self.navigated.connect(on_navigate)
