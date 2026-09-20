"""Window header: what the client is doing, and the one action that adds work.

The header answers three questions without the user going anywhere: how fast is
it downloading, how fast is it uploading, and how many torrents are doing it.
Every number here comes from a snapshot; none of them is remembered between
frames.

The "Add torrent" button lives here rather than in a menu because adding a
torrent is the thing the client is for.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.format import human_rate
from app.ui.theme import icons
from app.ui.theme.tokens import TOKENS


class _Rate(QLabel):
    """A big number with a small unit underneath it."""

    def __init__(self, unit: str, parent: QWidget | None = None) -> None:
        super().__init__("0 B/s", parent)
        self._unit = unit
        self.setProperty("role", "strong")
        self.setStyleSheet(
            f"font-size: {TOKENS.type.heading}pt; font-weight: 700; "
            f"font-family: {TOKENS.type.mono};"
        )
        self._caption = QLabel(unit)
        self._caption.setProperty("role", "faint")
        self._caption.setStyleSheet(f"font-size: {TOKENS.type.caption}pt; font-weight: 500;")

    @property
    def caption(self) -> QLabel:
        return self._caption


class TopBar(QFrame):
    """The header strip.

    Args:
        parent: Qt parent.
    """

    add_requested = Signal()
    """Emitted when the user asks to add a torrent."""
    mode_toggled = Signal()
    """Emitted when the user asks to toggle the theme mode."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topbar")
        self.setFixedHeight(TOKENS.geometry.topbar_height)
        spacing = TOKENS.spacing

        row = QHBoxLayout(self)
        row.setContentsMargins(spacing.lg, spacing.sm, spacing.lg, spacing.sm)
        row.setSpacing(spacing.md)

        # Download metric chip
        down_card = QFrame(self)
        down_card.setProperty("kind", "inset")
        down_row = QHBoxLayout(down_card)
        down_row.setContentsMargins(spacing.md, spacing.xs, spacing.md, spacing.xs)
        down_row.setSpacing(spacing.sm)

        down_icon = QLabel("↓")
        down_icon.setProperty("role", "accent")
        down_icon.setStyleSheet(f"font-size: {TOKENS.type.heading}pt; font-weight: 800;")
        down_row.addWidget(down_icon)

        down_stack = QVBoxLayout()
        down_stack.setContentsMargins(0, 0, 0, 0)
        down_stack.setSpacing(0)
        self._download = _Rate("download", down_card)
        self._download.setProperty("role", "accent")
        down_stack.addWidget(self._download, alignment=Qt.AlignmentFlag.AlignLeft)
        down_stack.addWidget(self._download.caption, alignment=Qt.AlignmentFlag.AlignLeft)
        down_row.addLayout(down_stack)
        row.addWidget(down_card)

        # Upload metric chip
        up_card = QFrame(self)
        up_card.setProperty("kind", "inset")
        up_row = QHBoxLayout(up_card)
        up_row.setContentsMargins(spacing.md, spacing.xs, spacing.md, spacing.xs)
        up_row.setSpacing(spacing.sm)

        up_icon = QLabel("↑")
        up_icon.setProperty("role", "success")
        up_icon.setStyleSheet(f"font-size: {TOKENS.type.heading}pt; font-weight: 800;")
        up_row.addWidget(up_icon)

        up_stack = QVBoxLayout()
        up_stack.setContentsMargins(0, 0, 0, 0)
        up_stack.setSpacing(0)
        self._upload = _Rate("upload", up_card)
        self._upload.setProperty("role", "success")
        up_stack.addWidget(self._upload, alignment=Qt.AlignmentFlag.AlignLeft)
        up_stack.addWidget(self._upload.caption, alignment=Qt.AlignmentFlag.AlignLeft)
        up_row.addLayout(up_stack)
        row.addWidget(up_card)

        row.addStretch(1)

        self._summary = QLabel("no torrents")
        self._summary.setProperty("kind", "pill")
        self._summary.setProperty("role", "muted")
        row.addWidget(self._summary)

        self._mode_toggle = QPushButton(" 🌙 Dark ")
        self._mode_toggle.setToolTip("Switch theme mode (Dark / Light / AMOLED)")
        self._mode_toggle.setAccessibleName("Toggle theme mode")
        self._mode_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_toggle.setMinimumHeight(TOKENS.geometry.control_height)
        self._mode_toggle.clicked.connect(self.mode_toggled.emit)
        row.addWidget(self._mode_toggle)

        self._add = QPushButton(" Add torrent")
        self._add.setIcon(icons.icon("plus", size=TOKENS.geometry.icon))
        self._add.setObjectName("primary")
        self._add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add.setMinimumHeight(TOKENS.geometry.control_height)
        self._add.clicked.connect(self.add_requested.emit)
        row.addWidget(self._add)

    # ------------------------------------------------------------------ readouts

    def set_rates(self, download_rate: float, upload_rate: float) -> None:
        """Show the two numbers the client exists to move."""
        self._download.setText(human_rate(download_rate))
        self._upload.setText(human_rate(upload_rate))

    def set_summary(self, text: str) -> None:
        """One line of context: counts, not decoration."""
        self._summary.setText(text)

    def set_theme(self, theme_name: str) -> None:
        """Update the mode toggle label to reflect the active theme."""
        name = theme_name.strip().lower()
        if name == "light":
            self._mode_toggle.setText(" ☀️ Light ")
        elif name == "amoled":
            self._mode_toggle.setText(" 🖤 AMOLED ")
        else:
            self._mode_toggle.setText(" 🌙 Dark ")
