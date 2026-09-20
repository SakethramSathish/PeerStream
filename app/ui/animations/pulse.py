"""Pulses: animation that only happens because something did.

A swarm canvas that shimmered on a timer would be decoration pretending to be
data. So a pulse here is *stimulated* — by a peer that just moved bytes — and
then decays on its own. If nothing is happening, the canvas is still, and that
stillness is information: it means the swarm went quiet.

Everything in this module is pure arithmetic over a monotonic clock. There is
no QTimer, no easing curve, and no thread, which is what makes it testable:
feed it a clock, read the level.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Half-life of a single pulse. Short enough to read as "just now", long enough
# to survive a 500 ms pump interval without flickering.
DEFAULT_HALF_LIFE: float = 0.35

# Below this a level is zero. Keeping a 0.004 tail alive costs redraws and
# shows the user nothing.
EPSILON: float = 0.01


def decay(level: float, seconds: float, *, half_life: float = DEFAULT_HALF_LIFE) -> float:
    """Decay ``level`` over ``seconds``.

    Half-life rather than linear fade: a pulse should drop quickly at first and
    then linger, which is how an eye reads "that one just moved".
    """
    if level <= 0.0:
        return 0.0
    if seconds <= 0.0:
        return level
    value: float = level * 0.5 ** (seconds / half_life)
    return 0.0 if value < EPSILON else value


def activity_level(rate: float, *, cap: float) -> float:
    """Map a measured rate onto ``0.0``-``1.0``.

    The cap is explicit because "fast" differs between a home connection and a
    datacentre, and a canvas that saturated at 100 KB/s would show every peer
    as maximally busy on anything faster. Above the cap the level is 1.0 — it
    does not wrap around, climb, or exaggerate.

    Args:
        rate: Bytes per second, as measured.
        cap: The rate that counts as fully busy. Non-positive caps clamp to
            zero activity rather than dividing by nothing.
    """
    if cap <= 0.0 or rate <= 0.0:
        return 0.0
    return min(1.0, rate / cap)


@dataclass(frozen=True, slots=True)
class Pulse:
    """One decaying value and when it was last set.

    Attributes:
        level: The value at ``stamp``.
        stamp: Monotonic time of the last change.
        half_life: How fast it decays.
    """

    level: float = 0.0
    stamp: float = 0.0
    half_life: float = DEFAULT_HALF_LIFE

    def at(self, now: float) -> float:
        """The level as of ``now``, decayed."""
        return decay(self.level, now - self.stamp, half_life=self.half_life)


@dataclass(slots=True)
class PulseBank:
    """A pulse per key: peers, pieces, whatever has identity.

    Pruned on read, because peers come and go and a bank that remembered a
    level for a peer that disconnected an hour ago is a leak with a nice name.

    Attributes:
        half_life: Applied to every pulse in the bank.
    """

    half_life: float = DEFAULT_HALF_LIFE
    _pulses: dict[str, Pulse] = field(default_factory=dict)

    def stimulate(self, key: str, amount: float = 1.0, *, now: float | None = None) -> None:
        """Set a pulse to at least ``amount``. Bigger wins; decay always pulls down."""
        stamp = time.monotonic() if now is None else now
        held = self._pulses.get(key)
        current = held.at(stamp) if held else 0.0
        self._pulses[key] = Pulse(
            level=max(current, min(1.0, amount)), stamp=stamp, half_life=self.half_life
        )

    def level(self, key: str, *, now: float | None = None) -> float:
        """The decayed level for ``key``, ``0.0`` if unknown."""
        stamp = time.monotonic() if now is None else now
        held = self._pulses.get(key)
        return 0.0 if held is None else held.at(stamp)

    def levels(self, *, now: float | None = None) -> dict[str, float]:
        """Every live level: zero entries are dropped rather than reported."""
        stamp = time.monotonic() if now is None else now
        live = {key: pulse.at(stamp) for key, pulse in self._pulses.items()}
        return {key: level for key, level in live.items() if level > 0.0}

    def prune(self, *, now: float | None = None) -> int:
        """Forget pulses that have decayed to nothing. Returns how many went."""
        stamp = time.monotonic() if now is None else now
        dead = [key for key, pulse in self._pulses.items() if pulse.at(stamp) <= 0.0]
        for key in dead:
            del self._pulses[key]
        return len(dead)

    def forget(self, key: str) -> None:
        """Drop one pulse, for a peer that has gone."""
        self._pulses.pop(key, None)

    def clear(self) -> None:
        self._pulses.clear()

    def __len__(self) -> int:
        return len(self._pulses)
