"""A labelled measurement in a card.

The command centre is mostly these. Two details are deliberate:

* **The value never changes width.** Numbers are drawn in the monospace data
  font and the label sits above them, so a rate going from 9.9 MB/s to
  10.1 MB/s cannot nudge the layout (PRD 11: speed values update without
  causing layout shifts).
* **The detail line is optional and always reserved.** A card that grows a
  second line when data appears would shift everything below it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from app.ui.theme.tokens import TOKENS


class StatCard(QWidget):
    """One measurement: a label, a value, and an optional detail line.

    Args:
        label: What is being measured, e.g. "Download".
        value: Initial value text.
        detail: Optional secondary line, e.g. a unit or a total.
        role: Text colour role for the value (``""`` for the default).
        parent: Qt parent.
    """

    def __init__(
        self,
        label: str,
        value: str = "--",
        *,
        detail: str = "",
        role: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("kind", "card")
        spacing = TOKENS.spacing

        layout = QVBoxLayout(self)
        layout.setContentsMargins(spacing.lg, spacing.md, spacing.lg, spacing.md)
        layout.setSpacing(spacing.xs)

        self._label = QLabel(label.upper())
        self._label.setProperty("role", "small")
        layout.addWidget(self._label)

        self._value = QLabel(value)
        self._value.setProperty("role", role or "data")
        self._value.setStyleSheet(f"font-size: {TOKENS.type.title}pt; font-weight: 600;")
        layout.addWidget(self._value)

        self._detail = QLabel(detail or " ")
        self._detail.setProperty("role", "faint")
        self._detail.setStyleSheet(f"font-size: {TOKENS.type.small}pt;")
        layout.addWidget(self._detail)

        self.setMinimumWidth(TOKENS.geometry.card_min_width)
        self.setMinimumHeight(84)

    # ------------------------------------------------------------------ access

    @property
    def value_label(self) -> QLabel:
        """The widget holding the value, for tests and for tooltips."""
        return self._value

    def set_value(self, text: str, *, role: str | None = None) -> None:
        """Update the value, and optionally its colour role."""
        self._value.setText(text)
        if role is not None and self._value.property("role") != role:
            self._value.setProperty("role", role)
            self._value.style().unpolish(self._value)
            self._value.style().polish(self._value)

    def set_detail(self, text: str) -> None:
        """Update the secondary line. Empty text leaves the row reserved."""
        self._detail.setText(text or " ")

    def set_label(self, text: str) -> None:
        self._label.setText(text.upper())


class StatGrid(QWidget):
    """A row of :class:`StatCard`s that wraps with the window.

    Args:
        columns: Preferred number of columns.
        parent: Qt parent.
    """

    def __init__(self, columns: int = 4, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(TOKENS.spacing.md)
        self._columns = max(1, columns)
        self._cards: dict[str, StatCard] = {}

    def add(self, key: str, label: str, value: str = "--", **kwargs: object) -> StatCard:
        """Add a card under ``key`` and return it."""
        card = StatCard(label, value, **kwargs)  # type: ignore[arg-type]
        self._cards[key] = card
        index = len(self._cards) - 1
        self._layout.addWidget(card, index // self._columns, index % self._columns)
        return card

    def card(self, key: str) -> StatCard | None:
        """The card registered under ``key``, if any."""
        return self._cards.get(key)

    def __getitem__(self, key: str) -> StatCard:
        return self._cards[key]

    def set_alignment_top(self) -> None:
        """Keep every card at the top of its grid cell (they vary in height)."""
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
