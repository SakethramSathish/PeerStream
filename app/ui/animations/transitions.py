"""Fades, and the guard that turns them off.

Motion in this interface is for two things: showing that something changed, and
showing where it went. It is never for its own sake, and it is always
switchable — the setting lives in :class:`~app.settings.schema.UiSettings` as
``reduced_motion``, and when it is on every helper here becomes instant rather
than merely quicker. A fade that still animates for 80 ms is not an
accessibility feature; it is a fade with a shorter argument.

These are the only animations the interface owns. The swarm canvas pulses on
real data (:mod:`app.ui.animations.pulse`), the graphs draw measured samples,
and nothing spins, bounces, or shimmers.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QTimer,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

# Long enough to be seen, short enough not to be waited for. Nothing in the
# interface is ever blocked on an animation finishing.
FADE_MS: Final[int] = 140
RAISE_MS: Final[int] = 180

# A fade below this many milliseconds is not a fade, it is a repaint.
MIN_MS: Final[int] = 1


def should_animate(*, reduced_motion: bool) -> bool:
    """Whether animation should happen at all."""
    return not reduced_motion


def duration_for(requested: int, *, reduced_motion: bool) -> int:
    """The duration to use: the requested one, or nothing at all."""
    return 0 if reduced_motion else max(MIN_MS, requested)


def ease_out_cubic(progress: float) -> float:
    """Easing that starts quickly and settles, matching how eyes track motion."""
    clamped = min(1.0, max(0.0, progress))
    return 1.0 - (1.0 - clamped) ** 3


class Fader(QObject):
    """Fade one widget in or out.

    Implemented with a :class:`QGraphicsOpacityEffect` because a plain widget
    cannot be made translucent: ``windowOpacity`` only applies to top-level
    windows, and the things worth fading (toasts, panels, chart legends) are
    children of one.

    Args:
        target: The widget to fade.
        reduced_motion: When true, :meth:`to` applies the opacity at once.
        parent: Qt parent.
    """

    def __init__(
        self,
        target: QWidget,
        *,
        reduced_motion: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._target = target
        self._reduced = reduced_motion
        self._effect = QGraphicsOpacityEffect(target)
        self._effect.setOpacity(1.0)
        target.setGraphicsEffect(self._effect)
        self._animation = QPropertyAnimation(self._effect, b"opacity", self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ------------------------------------------------------------------ control

    @property
    def opacity(self) -> float:
        """The widget's current opacity, ``0.0``-``1.0``."""
        return float(self._effect.opacity())

    @property
    def reduced_motion(self) -> bool:
        return self._reduced

    @reduced_motion.setter
    def reduced_motion(self, value: bool) -> None:
        self._reduced = bool(value)

    def to(
        self,
        opacity: float,
        *,
        duration_ms: int = FADE_MS,
        reduced_motion: bool | None = None,
    ) -> None:
        """Animate to ``opacity``, or jump there when motion is reduced.

        Args:
            opacity: Where to end up, clamped to ``0.0``-``1.0``.
            duration_ms: How long the fade should take when there is one.
            reduced_motion: Overrides the instance setting for this call.
        """
        goal = min(1.0, max(0.0, opacity))
        reduced = self._reduced if reduced_motion is None else reduced_motion
        milliseconds = duration_for(duration_ms, reduced_motion=reduced)
        self._animation.stop()
        if milliseconds <= 0:
            self._effect.setOpacity(goal)
            return
        self._animation.setDuration(milliseconds)
        self._animation.setStartValue(self._effect.opacity())
        self._animation.setEndValue(goal)
        self._animation.start()

    def fade_in(self, *, reduced_motion: bool | None = None) -> None:
        """Fade to fully opaque."""
        self.to(1.0, duration_ms=FADE_MS, reduced_motion=reduced_motion)

    def fade_out(self, *, reduced_motion: bool | None = None) -> None:
        """Fade to invisible."""
        self.to(0.0, duration_ms=FADE_MS, reduced_motion=reduced_motion)

    def stop(self) -> None:
        """Stop where it is, leaving the current opacity in place."""
        self._animation.stop()


class Ticker(QObject):
    """A repaint metronome with a stated ceiling.

    The charts redraw on data, not on a fixed timer; but a pulse has to decay,
    and decay needs ticks. So this timer exists, runs at a bounded rate, and is
    the only animation-loop-like thing in the app. A widget that needs it
    starts it when shown and stops it when hidden, so a background window
    costs nothing.

    Args:
        on_tick: Called at every interval. Connected once, in the constructor:
            calling :meth:`start` twice must not double the ticks.
        interval_ms: Milliseconds between ticks; clamped to at least 16 (one
            frame at 60 Hz), because a faster metronome is a busy loop.
        parent: Qt parent.
    """

    def __init__(
        self,
        on_tick: object,
        interval_ms: int = 50,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._interval = max(16, interval_ms)
        self._timer.setInterval(self._interval)
        self._timer.timeout.connect(on_tick)

    @property
    def interval_ms(self) -> int:
        return self._interval

    @interval_ms.setter
    def interval_ms(self, value: int) -> None:
        self._interval = max(16, int(value))
        self._timer.setInterval(self._interval)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        """Start ticking, if it isn't already."""
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
