"""The honest empty state: what is missing, why, and what to do about it.

There are two quite different kinds of "nothing here", and the interface does
not blur them:

* **Nothing yet** — the list is genuinely empty, and adding a torrent is the
  obvious next step. The action is offered.
* **Not available** — a subsystem that is switched off or has nothing to say
  yet: DHT before it is enabled, a magnet whose peers have not answered, a
  search that matched nothing. This client never renders a fake torrent list to
  make a screenshot look finished; the placeholder says what is missing and,
  where a milestone owns it, which one.

A placeholder that says "coming soon" without saying when is a lie of omission,
so the caller supplies the milestone.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.theme import icons
from app.ui.theme.tokens import TOKENS


class EmptyState(QWidget):
    """A centred message with an optional action.

    Args:
        title: The one-line headline.
        body: One or two sentences of explanation.
        icon_name: Which code-drawn icon to show above the text.
        parent: Qt parent.
    """

    action_requested = Signal()
    """Emitted when the user presses the action button."""

    def __init__(
        self,
        title: str = "Nothing here yet",
        body: str = "",
        *,
        icon_name: str = "library",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        spacing = TOKENS.spacing

        column = QVBoxLayout(self)
        column.setContentsMargins(spacing.xxl, spacing.xxl, spacing.xxl, spacing.xxl)
        column.setSpacing(spacing.md)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._glyph = QLabel()
        self._glyph.setPixmap(
            icons.icon(icon_name, size=TOKENS.geometry.icon_large).pixmap(
                TOKENS.geometry.icon_large, TOKENS.geometry.icon_large
            )
        )
        self._glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self._glyph)

        self._title = QLabel(title)
        self._title.setProperty("role", "strong")
        self._title.setStyleSheet(f"font-size: {TOKENS.type.heading}pt; font-weight: 700;")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self._title)

        self._body = QLabel(body)
        self._body.setProperty("role", "muted")
        self._body.setWordWrap(True)
        self._body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body.setMaximumWidth(460)
        column.addWidget(self._body)

        self._button = QPushButton("Add torrent")
        self._button.setObjectName("primary")
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setMinimumHeight(TOKENS.geometry.control_height)
        self._button.hide()
        self._button.clicked.connect(self.action_requested.emit)
        column.addWidget(self._button, alignment=Qt.AlignmentFlag.AlignCenter)

    # ----------------------------------------------------------------- content

    def set_text(self, title: str, body: str = "", *, icon_name: str | None = None) -> None:
        """Replace the message."""
        self._title.setText(title)
        self._body.setText(body)
        if icon_name is not None:
            self._glyph.setPixmap(
                icons.icon(icon_name, size=TOKENS.geometry.icon_large).pixmap(
                    TOKENS.geometry.icon_large, TOKENS.geometry.icon_large
                )
            )

    def set_action(self, label: str | None) -> None:
        """Offer a button, or remove it when there is nothing useful to do."""
        if label is None:
            self._button.hide()
            return
        self._button.setText(label)
        self._button.show()


def not_implemented(
    feature: str,
    *,
    milestone: str,
    what_will_work: Sequence[str] = (),
    parent: QWidget | None = None,
) -> EmptyState:
    """An empty state for a subsystem that is genuinely not built yet.

    Args:
        feature: The feature's name, e.g. "DHT".
        milestone: The milestone that owns it, e.g. "M15".
        what_will_work: What the user can do meanwhile.
        parent: Qt parent.

    Returns:
        An :class:`EmptyState` that says so plainly.
    """
    lines = [f"{feature} arrives with {milestone}. Until then this panel stays empty on purpose."]
    lines.extend(f"• {item}" for item in what_will_work)
    return EmptyState(
        f"{feature} is not enabled",
        "\n".join(lines),
        icon_name="dht",
        parent=parent,
    )
