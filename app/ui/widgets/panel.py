"""A titled card: the unit every detail tab is built from.

The command centre has its own private copy of this idea; the detail tabs share
this one, because six tabs each with a slightly different card would drift the
moment someone restyled one.

The card is a frame with ``kind="card"`` (styled by the generated stylesheet)
and a vertical body layout. It does not scroll, stretch, or own a title bar: it
is a box with a heading, which is all a panel should be.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from app.ui.theme.tokens import TOKENS


class Panel(QFrame):
    """A titled card with a body layout.

    Args:
        title: The heading, drawn small and upper-cased.
        subtitle: An optional line under the heading, for units or caveats.
        parent: Qt parent.
    """

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("kind", "card")
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(
            TOKENS.spacing.lg, TOKENS.spacing.md, TOKENS.spacing.lg, TOKENS.spacing.lg
        )
        self._column.setSpacing(TOKENS.spacing.md)

        heading = QLabel(title.upper())
        heading.setProperty("role", "small")
        self._column.addWidget(heading)

        self._subtitle = QLabel(subtitle)
        self._subtitle.setProperty("role", "faint")
        self._subtitle.setWordWrap(True)
        self._subtitle.setVisible(bool(subtitle))
        self._column.addWidget(self._subtitle)

    @property
    def body(self) -> QVBoxLayout:
        """Where the panel's content goes."""
        return self._column

    def set_subtitle(self, text: str) -> None:
        """Change (or clear) the line under the heading."""
        self._subtitle.setText(text)
        self._subtitle.setVisible(bool(text))

    @property
    def subtitle(self) -> str:
        """The line under the heading."""
        return self._subtitle.text()
