"""The view models' shared parts: a bounded series and a throttled clock.

Two problems every chart in this client has, solved once:

**Memory.** A graph that appends a sample forever is a leak with a nice user
interface. :class:`SeriesBuffer` keeps a fixed number of samples and drops the
oldest — the same bounded-history rule the statistics module applies to its
rate windows, applied to the pixels.

**Repaints.** A swarm at full speed can produce far more measurements than a
screen can show. :class:`Throttle` refuses work that arrives sooner than the
frame budget allows, so a fast torrent cannot turn the UI into a busy loop.

Neither class knows anything about Qt (beyond using a monotonic clock) or about
BitTorrent, so both can be tested without a display.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from app.ui.theme.tokens import TOKENS

# Default capacity: the chart token's own, so a graph cannot quietly disagree
# with the design system about how much history it remembers.
DEFAULT_CAPACITY: int = TOKENS.chart.max_points

# An interval is a floor, not a knife edge.
#
# Both classes below compare the difference of two ``time.monotonic()`` readings
# against a fixed interval. Monotonic grows with the machine's uptime, so those
# readings are large floats, and for roughly one uptime in sixty ``base + 1.0 -
# base`` rounds to a hair *under* 1.0 rather than to it. Without slack that reads
# as "too soon": a sample taken exactly on time is dropped, and one dropped
# sample turns a five-minute chart window into a five-minute-and-one-second one.
# The failure then depends on how long the machine has been up, which is the
# worst property a test can have and a quietly wrong graph the worst property a
# chart can have.
#
# One millisecond is 0.1% of the shortest interval anything here uses, and well
# below the jitter a real timer has.
INTERVAL_TOLERANCE_SECONDS: float = 0.001


@dataclass(slots=True)
class SeriesBuffer:
    """A bounded, time-stamped series of floats.

    Samples are ``(seconds, value)`` pairs where the timestamp is measured when
    the sample is taken. The buffer keeps the most recent ``capacity`` of them.

    Args:
        capacity: How many samples to remember.
        min_gap_seconds: Ignore a sample that arrives sooner than this after
            the previous one, less :data:`INTERVAL_TOLERANCE_SECONDS` of slack.
            Graphs are drawn at 20 fps, not at network speed.
    """

    capacity: int = DEFAULT_CAPACITY
    min_gap_seconds: float = 0.0
    _samples: deque[tuple[float, float]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError(f"capacity must be positive, got {self.capacity}")
        if self.min_gap_seconds < 0.0:
            raise ValueError(f"min gap must not be negative, got {self.min_gap_seconds}")
        self._samples = deque(maxlen=self.capacity)

    # ------------------------------------------------------------------ writing

    def append(self, value: float, *, now: float | None = None) -> bool:
        """Record a sample.

        Args:
            value: The measurement.
            now: Timestamp; ``time.monotonic()`` when omitted.

        Returns:
            Whether the sample was kept. ``False`` means it arrived inside the
            minimum gap, which is not an error — it is the throttle working.
        """
        stamp = time.monotonic() if now is None else now
        if self.min_gap_seconds and self._samples:
            floor = max(self.min_gap_seconds - INTERVAL_TOLERANCE_SECONDS, 0.0)
            if stamp - self._samples[-1][0] < floor:
                return False
        self._samples.append((stamp, float(value)))
        return True

    def clear(self) -> None:
        """Forget every sample."""
        self._samples.clear()

    # ------------------------------------------------------------------ reading

    def __len__(self) -> int:
        return len(self._samples)

    @property
    def samples(self) -> tuple[tuple[float, float], ...]:
        """Every retained sample, oldest first."""
        return tuple(self._samples)

    @property
    def latest(self) -> float | None:
        """The most recent value, or ``None`` when nothing has been recorded."""
        return None if not self._samples else self._samples[-1][1]

    @property
    def maximum(self) -> float:
        """The largest value retained; ``0.0`` when empty."""
        return max((value for _stamp, value in self._samples), default=0.0)

    @property
    def span(self) -> float:
        """Seconds between the oldest and newest retained sample."""
        if len(self._samples) < 2:
            return 0.0
        return self._samples[-1][0] - self._samples[0][0]

    def as_tuples(self) -> tuple[tuple[float, float], ...]:
        """The shape :class:`~app.ui.widgets.sparkline.Sparkline` wants."""
        return self.samples


@dataclass(slots=True)
class Throttle:
    """Allow work at most once per interval.

    Args:
        interval_ms: The minimum gap between allowed calls.
    """

    interval_ms: int = 1000 // TOKENS.chart.max_fps
    _last: float = field(default=-1.0, init=False)

    def __post_init__(self) -> None:
        if self.interval_ms < 0:
            raise ValueError(f"interval must not be negative, got {self.interval_ms}")
        self._last = -1.0

    def allows(self, *, now: float | None = None) -> bool:
        """Whether now is soon enough after the last allowed call.

        Calling this *consumes* the allowance, so a caller that checks then
        does the work gets the interval it asked for.
        """
        stamp = time.monotonic() if now is None else now
        floor_ms = max(self.interval_ms - INTERVAL_TOLERANCE_SECONDS * 1000.0, 0.0)
        if self._last >= 0.0 and (stamp - self._last) * 1000.0 < floor_ms:
            return False
        self._last = stamp
        return True

    def reset(self) -> None:
        """Forget when the last call was allowed."""
        self._last = -1.0


@dataclass(frozen=True, slots=True)
class Health:
    """A ratio the client can actually count, with the counts that made it.

    Attributes:
        fraction: ``0.0``-``1.0``, or ``None`` when there is nothing to
            divide — a health of 100% built from zero peers is a guess, and
            this client does not guess.
        numerator: What was counted.
        denominator: What it was counted out of.
        note: The counts, as text, e.g. ``"3/8 unchoked"``.
    """

    fraction: float | None
    numerator: int = 0
    denominator: int = 0
    note: str = ""

    @property
    def known(self) -> bool:
        """Whether there was anything to measure."""
        return self.fraction is not None


def ratio(numerator: int, denominator: int, *, note: str = "") -> Health:
    """Build a :class:`Health` from two counts, refusing to divide by zero.

    Args:
        numerator: How many of the thing happened.
        denominator: How many could have.
        note: A label for the counts; ``"3/8"`` is used when omitted.
    """
    if denominator <= 0:
        return Health(None, numerator, denominator, note or "nothing to measure")
    return Health(
        min(1.0, max(0.0, numerator / denominator)),
        numerator,
        denominator,
        note or f"{numerator}/{denominator}",
    )
