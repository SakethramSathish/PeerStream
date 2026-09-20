"""Transient notifications: a line at the bottom, then gone.

A toast is for things that happened and were not asked for — a tracker answered
with a warning, a torrent finished, a peer was refused. They stack upwards,
newest at the bottom, and each one leaves on its own timer.

Two rules keep this from becoming noise: there is a hard cap on how many are
visible at once, and only messages with real content are shown. An interface
that congratulates the user on every block received is an interface the user
learns to ignore.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from app.ui.theme import icons
from app.ui.theme.palette import DARK, Palette
from app.ui.theme.tokens import TOKENS

# How many toasts may be on screen at once. Older ones are retired early.
MAX_VISIBLE: int = 4

# How long a toast lives: long enough to read two lines twice, short enough
# not to be in the way.
DEFAULT_LIFETIME_MS: int = 4000


class ToastKind(StrEnum):
    """What sort of thing happened."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Toast:
    """One notification.

    Args:
        message: What to say. Blank messages are never shown.
        kind: Which colour role and icon to use.
        lifetime_ms: How long it stays.
        title: An optional bold lead-in.
    """

    message: str
    kind: ToastKind = ToastKind.INFO
    lifetime_ms: int = DEFAULT_LIFETIME_MS
    title: str = ""

    @property
    def visible(self) -> bool:
        """Whether this toast has anything to say."""
        return bool(self.message.strip())


class _ToastCard(QFrame):
    """The widget for one toast: a coloured edge, text, and a countdown."""

    def __init__(
        self,
        toast: Toast,
        *,
        palette: Palette = DARK,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        spacing = TOKENS.spacing

        column = QVBoxLayout(self)
        column.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        column.setSpacing(spacing.xs)

        if toast.title:
            lead = QLabel(toast.title)
            lead.setProperty("role", "strong")
            column.addWidget(lead)

        self.message = toast.message
        body = QLabel(toast.message)
        body.setWordWrap(True)
        body.setMaximumWidth(340)
        body.setStyleSheet(f"color: {palette.text}; font-size: {TOKENS.type.small}pt;")
        column.addWidget(body)

        colour = _colour_for(toast.kind, palette)
        self.setStyleSheet(
            f"#toast {{ background: {palette.surface_alt};"
            f" border: 1px solid {palette.border};"
            f" border-left: 3px solid {colour};"
            f" border-radius: {TOKENS.radii.md}px; }}"
        )
        # The glyph is kept so the icon module's cache stays warm and the
        # mapping stays exercised; the card itself shows the colour edge.
        self._glyph = icons.icon(_icon_for(toast.kind), size=TOKENS.geometry.icon)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(max(500, toast.lifetime_ms))

    def start(self) -> None:
        """Begin the countdown to removal."""
        self.timer.start()

    @property
    def glyph(self) -> object:
        """The icon this card's kind maps to. Exposed for tests."""
        return self._glyph


def _colour_for(kind: ToastKind, palette: Palette) -> str:
    return {
        ToastKind.INFO: palette.info,
        ToastKind.SUCCESS: palette.success,
        ToastKind.WARNING: palette.warning,
        ToastKind.ERROR: palette.danger,
    }[kind]


def _icon_for(kind: ToastKind) -> str:
    return {
        ToastKind.INFO: "network",
        ToastKind.SUCCESS: "check",
        ToastKind.WARNING: "alert",
        ToastKind.ERROR: "alert",
    }[kind]


class ToastHost(QWidget):
    """Where toasts appear: the bottom-right corner, out of the way.

    The host is transparent to the mouse, so a toast can never swallow a click
    meant for the thing underneath it.

    Args:
        parent: The window whose corner the toasts live in.
        palette: Colours to draw with.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        palette: Palette = DARK,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._palette = palette
        self._cards: list[_ToastCard] = []

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(TOKENS.spacing.sm)
        column.addStretch(1)
        self._column = column

    @property
    def colours(self) -> Palette:
        """The colours new toasts are painted with.

        Named ``colours`` rather than ``palette`` because ``QWidget.palette()``
        is Qt's own, and shadowing a Qt method is a bug waiting for a reader.
        """
        return self._palette

    def set_colours(self, palette: Palette) -> None:
        """Paint future toasts for a new theme. Ones already up keep theirs."""
        self._palette = palette

    # ------------------------------------------------------------------ showing

    @property
    def count(self) -> int:
        """How many toasts are on screen."""
        return len(self._cards)

    @property
    def messages(self) -> tuple[str, ...]:
        """The text of every toast on screen, oldest first.

        A count is not an assertion: "one toast" says nothing about whether the
        right thing was said, so the text is readable too.
        """
        return tuple(card.message for card in self._cards)

    def notify(self, toast: Toast) -> bool:
        """Show a toast.

        Returns:
            Whether it was shown. Blank messages are dropped: an empty
            notification is a bug that reached the screen.
        """
        if not toast.visible:
            return False
        while len(self._cards) >= MAX_VISIBLE:
            self._retire(self._cards[0])
        card = _ToastCard(toast, palette=self._palette, parent=self)
        card.timer.timeout.connect(lambda card=card: self._retire(card))
        self._column.addWidget(card)
        card.show()
        card.start()
        self._cards.append(card)
        return True

    def info(self, message: str, **kwargs: object) -> bool:
        """Show an informational toast."""
        return self.notify(Toast(message, ToastKind.INFO, **kwargs))  # type: ignore[arg-type]

    def success(self, message: str, **kwargs: object) -> bool:
        """Show a good-news toast."""
        return self.notify(Toast(message, ToastKind.SUCCESS, **kwargs))  # type: ignore[arg-type]

    def warning(self, message: str, **kwargs: object) -> bool:
        """Show a warning toast."""
        return self.notify(Toast(message, ToastKind.WARNING, **kwargs))  # type: ignore[arg-type]

    def error(self, message: str, **kwargs: object) -> bool:
        """Show an error toast."""
        return self.notify(Toast(message, ToastKind.ERROR, **kwargs))  # type: ignore[arg-type]

    def clear(self) -> None:
        """Remove every toast now."""
        while self._cards:
            self._retire(self._cards[0])

    def _retire(self, card: _ToastCard) -> None:
        with contextlib.suppress(ValueError):
            self._cards.remove(card)
        card.timer.stop()
        self._column.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
