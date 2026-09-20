"""Rolling-window rate meters (instant, 5 s, 30 s, session).

A speed is not a number a client stores, it is a number a client *measures*:
bytes that arrived over the last few seconds, divided by those seconds. That
distinction is the whole module. Two consequences matter:

* **An idle meter reads zero.** A rate is not remembered from before the
  connection went quiet, so a stalled download shows 0 B/s rather than the
  speed it had a minute ago.
* **A window is a promise about the past.** ``RateMeter(5.0)`` answers "what
  was the throughput of the last five seconds?" — not "since when?", not
  "on average?". A test can therefore feed it a scripted history and check the
  arithmetic, which is why this module has no sockets in it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from app.core.constants import (
    DEFAULT_INSTANT_WINDOW_SECONDS,
    DEFAULT_LONG_WINDOW_SECONDS,
    DEFAULT_STATS_WINDOW_SECONDS,
)

Clock = Callable[[], float]

# The windows the UI cares about: what is happening now, what the last few
# seconds looked like, and what the trend is.
DEFAULT_WINDOWS: tuple[float, ...] = (
    DEFAULT_INSTANT_WINDOW_SECONDS,
    DEFAULT_STATS_WINDOW_SECONDS,
    DEFAULT_LONG_WINDOW_SECONDS,
)


@dataclass(frozen=True, slots=True)
class SpeedRates:
    """One meter read at one moment.

    Attributes:
        instant: Bytes per second over the shortest window (~1 s). Lively,
            and jumpy for the same reason.
        short: Over the standard window (~5 s). What a graph should draw.
        long: Over the long window (~30 s). What a trend is.
        average: Total bytes divided by the time since the first byte: the
            honest session speed, including the idle stretches.
        total: Every byte counted by this meter, ever.
    """

    instant: float = 0.0
    short: float = 0.0
    long: float = 0.0
    average: float = 0.0
    total: int = 0

    @property
    def displayed(self) -> float:
        """The number to show when only one number fits.

        The short window, because it is steady enough to read and short enough
        to still be true.
        """
        return self.short


class RateMeter:
    """Bytes counted over a trailing window of time.

    Args:
        window: Length of the window in seconds. Must be positive.
        clock: Time source; injectable so tests can run faster than real time.
    """

    def __init__(self, window: float, *, clock: Clock = time.monotonic) -> None:
        if window <= 0:
            raise ValueError(f"window must be positive, got {window!r}")
        self._window = float(window)
        self._clock = clock
        self._samples: list[tuple[float, int]] = []
        self._total = 0

    @property
    def window(self) -> float:
        """How far back this meter looks."""
        return self._window

    @property
    def total(self) -> int:
        """Every byte this meter has counted, including pruned ones."""
        return self._total

    @property
    def sample_count(self) -> int:
        """Samples still inside the window."""
        return len(self._samples)

    @property
    def bytes_in_window(self) -> int:
        """Bytes counted inside the window (the numerator of :meth:`rate`)."""
        return sum(amount for _stamp, amount in self._samples)

    def add(self, amount: int, *, now: float | None = None) -> None:
        """Count ``amount`` bytes as having moved now.

        Amounts of zero or less are ignored: a meter that counted nothing
        happening would only be able to report a smaller rate.
        """
        if amount <= 0:
            return
        stamp = self._clock() if now is None else now
        self._samples.append((stamp, amount))
        self._total += amount
        self.prune(now=stamp)

    def rate(self, *, now: float | None = None) -> float:
        """Bytes per second over the window, ``0.0`` when nothing is moving."""
        stamp = self._clock() if now is None else now
        self.prune(now=stamp)
        if not self._samples:
            return 0.0
        return self.bytes_in_window / self._window

    def prune(self, *, now: float | None = None) -> int:
        """Forget samples that fell out of the window. Returns how many went."""
        stamp = self._clock() if now is None else now
        cutoff = stamp - self._window
        kept = [sample for sample in self._samples if sample[0] > cutoff]
        dropped = len(self._samples) - len(kept)
        if dropped:
            self._samples = kept
        return dropped

    def reset(self) -> None:
        """Forget the window and the total alike."""
        self._samples = []
        self._total = 0


class SpeedMeter:
    """One stream (download or upload) read at several windows at once.

    Args:
        windows: Window lengths in seconds, shortest first.
        clock: Time source; injectable for tests.

    Example:
        >>> meter = SpeedMeter()
        >>> meter.add(16384)
        >>> round(meter.rate(window=5.0), 1) >= 0
        True
    """

    def __init__(
        self,
        windows: tuple[float, ...] = DEFAULT_WINDOWS,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        if not windows:
            raise ValueError("a speed meter needs at least one window")
        self._clock = clock
        self._windows = tuple(float(window) for window in windows)
        self._meters = tuple(RateMeter(window, clock=clock) for window in self._windows)
        self._started_at: float | None = None

    @property
    def windows(self) -> tuple[float, ...]:
        """The window lengths this meter reports."""
        return self._windows

    @property
    def total(self) -> int:
        """Every byte counted, ever (all meters see the same bytes)."""
        return self._meters[0].total

    @property
    def started_at(self) -> float | None:
        """When the first byte moved, ``None`` while nothing has."""
        return self._started_at

    def elapsed(self, *, now: float | None = None) -> float:
        """Seconds since the first byte; ``0.0`` before there was one."""
        if self._started_at is None:
            return 0.0
        stamp = self._clock() if now is None else now
        return max(0.0, stamp - self._started_at)

    def average_at(self, *, now: float | None = None) -> float:
        """Session speed as of ``now``: total bytes over time since byte one.

        Takes the timestamp explicitly so a caller reading every window at one
        instant gets an average measured at that same instant.
        """
        elapsed = self.elapsed(now=now)
        if elapsed <= 0:
            return 0.0
        return self.total / elapsed

    @property
    def average(self) -> float:
        """Session speed, measured on the meter's own clock."""
        return self.average_at()

    def add(self, amount: int, *, now: float | None = None) -> None:
        """Count ``amount`` bytes as having moved on this stream."""
        if amount <= 0:
            return
        stamp = self._clock() if now is None else now
        if self._started_at is None:
            self._started_at = stamp
        for meter in self._meters:
            meter.add(amount, now=stamp)

    def rate(self, window: float | None = None, *, now: float | None = None) -> float:
        """Bytes per second over one window (the longest, when omitted)."""
        stamp = self._clock() if now is None else now
        if window is None:
            return self._meters[-1].rate(now=stamp)
        for meter in self._meters:
            if meter.window == window:
                return meter.rate(now=stamp)
        raise ValueError(f"no meter for window {window!r}")

    def rates(self, *, now: float | None = None) -> SpeedRates:
        """Read every window at once.

        The windows are handed out by position — instant, short, long — with
        the shortest standing in for missing ones, so a meter built with one
        window still fills the whole snapshot.
        """
        stamp = self._clock() if now is None else now
        values = [meter.rate(now=stamp) for meter in self._meters]
        while len(values) < 3:
            values.insert(0, values[0])
        return SpeedRates(
            instant=values[0],
            short=values[1] if len(values) > 1 else values[0],
            long=values[2] if len(values) > 2 else values[-1],
            average=self.average_at(now=stamp),
            total=self.total,
        )

    def snapshot(self, *, now: float | None = None) -> SpeedRates:
        """Alias for :meth:`rates`, for callers that sample on a schedule."""
        return self.rates(now=now)

    def reset(self) -> None:
        """Forget everything: the windows, the total, and when we started."""
        for meter in self._meters:
            meter.reset()
        self._started_at = None


@dataclass(frozen=True, slots=True)
class Counter:
    """A named total that only ever goes up, plus when it last moved.

    Statistics are full of "how many, and how long ago": peers discovered,
    pieces failed, requests refused. Keeping the stamp with the count is what
    lets the UI say "3 minutes ago" instead of guessing.
    """

    name: str
    value: int = 0
    updated_at: float | None = None

    def bump(self, amount: int = 1, *, now: float) -> Counter:
        """Return a copy advanced by ``amount``."""
        if amount == 0:
            return self
        return replace(self, value=self.value + amount, updated_at=now)

    def seconds_since(self, *, now: float) -> float | None:
        """How long ago this counter last moved, ``None`` if it never did."""
        if self.updated_at is None:
            return None
        return max(0.0, now - self.updated_at)


@dataclass(slots=True)
class CounterSet:
    """Named counters in one place, so "unknown counter" cannot happen."""

    clock: Clock = time.monotonic
    counters: dict[str, Counter] = field(default_factory=dict)

    def names(self) -> tuple[str, ...]:
        """Every counter name, in the order they were first bumped."""
        return tuple(self.counters)

    def get(self, name: str) -> int:
        """The current value of ``name``; ``0`` for a counter never bumped."""
        counter = self.counters.get(name)
        return 0 if counter is None else counter.value

    def bump(self, name: str, amount: int = 1) -> int:
        """Advance ``name`` by ``amount`` and return its new value."""
        now = self.clock()
        current = self.counters.get(name) or Counter(name=name)
        updated = current.bump(amount, now=now)
        self.counters[name] = updated
        return updated.value

    def as_dict(self) -> dict[str, int]:
        """A plain snapshot: name → value."""
        return {name: counter.value for name, counter in self.counters.items()}
