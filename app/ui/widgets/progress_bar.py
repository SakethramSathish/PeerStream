"""A progress bar that reports the number it was given.

Two behaviours matter enough to be worth a subclass:

* The value is a **fraction** (0.0 to 1.0), not a percentage, because every
  progress number in the client is a fraction except the one a human reads.
* The colour follows the state: blue while downloading, green when seeding,
  red on failure — through the stylesheet's ``role`` property, so the painted
  widget and the styled widgets agree.
"""

from __future__ import annotations

from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QProgressBar, QWidget

from app.ui.theme.palette import DARK
from app.ui.theme.tokens import TOKENS


class ProgressBar(QProgressBar):
    """A horizontal progress bar with a state-driven colour role.

    Args:
        parent: Qt parent.
        height: Bar thickness in pixels.
    """

    def __init__(
        self, parent: QWidget | None = None, *, height: int = 6, role: str = "accent"
    ) -> None:
        super().__init__(parent)
        self.setRange(0, 1000)
        self.setValue(0)
        self.setTextVisible(False)
        self.setFixedHeight(height)
        self.set_role(role)

    def set_role(self, role: str) -> None:
        """Repaint the bar for a state: ``accent``, ``success``, ``warning``, ``danger``."""
        current = self.property("role")
        if current != role:
            self.setProperty("role", role)
            # Qt caches the stylesheet: it has to be nudged to re-evaluate a
            # property selector.
            self.style().unpolish(self)
            self.style().polish(self)

    def set_fraction(self, fraction: float) -> None:
        """Show ``fraction`` of the bar filled, clamped to ``[0.0, 1.0]``.

        Values outside the range are refused rather than wrapped: a progress of
        140 % is a bug somewhere else, and hiding it here would hide the bug.
        """
        clamped = min(1.0, max(0.0, float(fraction)))
        self.setValue(int(clamped * 1000))

    @property
    def fraction(self) -> float:
        """The fraction currently shown."""
        return self.value() / 1000.0


class SegmentedBar(QWidget):
    """A progress bar split into the torrent's pieces.

    Used where a single average would hide the interesting part: how much of
    the middle is still missing. Each segment is one piece-slot wide, filled in
    proportion, so a torrent that is 90% done in scattered pieces looks
    different from one that is 90% done contiguously — which is the truth a
    plain bar cannot show.

    Args:
        segments: How many segments to draw.
        parent: Qt parent.
    """

    def __init__(self, segments: int = 64, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(TOKENS.spacing.sm)
        self._segments = max(1, segments)
        self._filled = 0.0
        self.setMinimumWidth(self._segments * 3)

    def set_fraction(self, fraction: float) -> None:
        """Fill ``fraction`` of the segments."""
        self._filled = min(1.0, max(0.0, float(fraction)))
        self.update()

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        width = self.width()
        gap = 1
        segment_width = max(1.0, (width - gap * (self._segments - 1)) / self._segments)
        filled = self._filled * self._segments
        for index in range(self._segments):
            left = index * (segment_width + gap)
            amount = min(1.0, max(0.0, filled - index))
            painter.fillRect(
                int(left),
                0,
                max(1, int(segment_width * amount)),
                self.height(),
                DARK.accent if amount else DARK.piece_missing,
            )
        painter.end()
