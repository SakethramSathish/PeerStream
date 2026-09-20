"""A health meter: one number, a bar, and a plain-language verdict.

Health is not a vibe. Each meter is a ratio the client can actually count:

* **Peer health** is the share of connected peers that are unchoked and
  serving, over the peers we are connected to.
* **Tracker health** is the share of trackers that answered their last
  announce.
* **Piece health** is the share of pieces that verified on the first attempt,
  over the pieces attempted.

A meter with nothing to measure says "no data" rather than showing a
confident-looking 100%, because a green bar standing for "we have no idea" is
the kind of thing this client does not do.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from app.ui.theme.tokens import TOKENS


class HealthMeter(QWidget):
    """A labelled bar with a percentage, coloured by how good the number is.

    Args:
        label: What is being measured, e.g. "Peer health".
        parent: Qt parent.
    """

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._health: float | None = None
        spacing = TOKENS.spacing

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(spacing.xs)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(spacing.sm)

        self._caption = QLabel(label)
        self._caption.setProperty("role", "small")
        row.addWidget(self._caption)
        row.addStretch(1)

        self._value = QLabel("no data")
        self._value.setProperty("role", "faint")
        self._value.setStyleSheet(f"font-size: {TOKENS.type.small}pt;")
        row.addWidget(self._value)
        column.addLayout(row)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(TOKENS.spacing.xs + 2)
        column.addWidget(self._bar)

    # ------------------------------------------------------------------ access

    @property
    def fraction(self) -> float | None:
        """The health currently shown, or ``None`` when there is nothing to measure."""
        return self._health

    def set_health(self, fraction: float | None, *, note: str = "") -> None:
        """Show a health ratio, or honestly admit there is nothing to measure.

        Args:
            fraction: ``0.0``-``1.0``, or ``None`` for "no data".
            note: An optional explanation shown beside the value, e.g. ``"3/8
                unchoked"``. A percentage alone hides the sample size, and a
                health of 100% built from one peer is not the same claim as
                100% built from forty.
        """
        if fraction is None:
            self._health = None
            self._bar.setValue(0)
            self._value.setText("no data")
            self._tint("faint")
            return

        clamped = min(1.0, max(0.0, float(fraction)))
        self._health = clamped
        self._bar.setValue(round(clamped * 100))
        self._value.setText(f"{clamped * 100:.0f}%" + (f"  ({note})" if note else ""))
        self._tint(_role_for(clamped))

    def _tint(self, role: str) -> None:
        self._value.setProperty("role", role)
        self._value.style().unpolish(self._value)
        self._value.style().polish(self._value)
        self._bar.setProperty("role", role)
        self._bar.style().unpolish(self._bar)
        self._bar.style().polish(self._bar)


def _role_for(fraction: float) -> str:
    """Which colour role a health ratio deserves.

    At or above 0.75 is healthy, at or above 0.4 is degraded, below that is
    poor. The thresholds are a judgement, so they are written down here rather
    than implied by a colour someone picked.
    """
    if fraction >= 0.75:
        return "success"
    if fraction >= 0.4:
        return "warning"
    return "danger"
