"""Bounded ring buffers of timestamped samples, for graphs.

A download runs for hours; a graph has a fixed number of pixels. This module
is where those two facts meet: every series keeps at most ``capacity`` samples
and drops the oldest when a new one arrives, so a long session costs the same
memory as a short one.

The rule that makes it safe to hand these to a UI: **the buffer never grows**.
Not "grows slowly", not "grows until someone remembers to trim it". Samples
past the end are gone, which is also honest: a graph claiming to show the last
five minutes must not quietly show the last fifty.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import NamedTuple

from app.core.constants import DEFAULT_HISTORY_SAMPLES

Clock = Callable[[], float]


class Sample(NamedTuple):
    """One point on a graph.

    Attributes:
        timestamp: When the value was observed, on the collector's clock.
        value: The observed value (bytes per second, peer count, …).
    """

    timestamp: float
    value: float


class History:
    """A fixed-capacity series of :class:`Sample` values.

    Args:
        capacity: How many samples to keep. Must be at least one.
        clock: Time source; injectable so tests do not have to wait.

    Example:
        >>> history = History(capacity=2)
        >>> history.add(1.0, now=0.0)
        Sample(timestamp=0.0, value=1.0)
        >>> history.add(2.0, now=1.0).value
        2.0
        >>> [sample.value for sample in history.series()]
        [1.0, 2.0]
        >>> history.add(3.0, now=2.0).value
        3.0
        >>> [sample.value for sample in history.series()]
        [2.0, 3.0]
    """

    def __init__(
        self,
        capacity: int = DEFAULT_HISTORY_SAMPLES,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {capacity!r}")
        self._capacity = int(capacity)
        self._clock = clock
        self._samples: deque[Sample] = deque(maxlen=self._capacity)
        self._added = 0

    @property
    def capacity(self) -> int:
        """How many samples this history keeps, at most."""
        return self._capacity

    @property
    def added(self) -> int:
        """How many samples were ever added, including dropped ones."""
        return self._added

    @property
    def dropped(self) -> int:
        """How many samples fell off the end."""
        return max(0, self._added - len(self._samples))

    @property
    def full(self) -> bool:
        """Whether the buffer has reached its capacity."""
        return len(self._samples) >= self._capacity

    def __len__(self) -> int:
        return len(self._samples)

    def __iter__(self) -> Iterator[Sample]:
        return iter(self._samples)

    def add(self, value: float, *, now: float | None = None) -> Sample:
        """Record ``value`` now. Returns the sample that was stored."""
        stamp = self._clock() if now is None else now
        sample = Sample(timestamp=float(stamp), value=float(value))
        self._samples.append(sample)
        self._added += 1
        return sample

    def extend(self, values: list[float] | tuple[float, ...], *, now: float | None = None) -> int:
        """Record several values at the same instant. Returns how many."""
        stamp = self._clock() if now is None else now
        for value in values:
            self.add(value, now=stamp)
        return len(values)

    def series(self) -> tuple[Sample, ...]:
        """Every retained sample, oldest first."""
        return tuple(self._samples)

    def values(self) -> tuple[float, ...]:
        """Just the values, oldest first — what a chart's y-axis wants."""
        return tuple(sample.value for sample in self._samples)

    def timestamps(self) -> tuple[float, ...]:
        """Just the timestamps, oldest first — the x-axis."""
        return tuple(sample.timestamp for sample in self._samples)

    @property
    def latest(self) -> Sample | None:
        """The most recent sample, or ``None`` when nothing was recorded."""
        return self._samples[-1] if self._samples else None

    @property
    def oldest(self) -> Sample | None:
        """The oldest retained sample, or ``None`` when empty."""
        return self._samples[0] if self._samples else None

    @property
    def span(self) -> float:
        """Seconds between the oldest and newest retained sample."""
        if len(self._samples) < 2:
            return 0.0
        return float(self._samples[-1].timestamp - self._samples[0].timestamp)

    @property
    def peak(self) -> float:
        """Largest retained value, ``0.0`` when empty (a graph needs a top)."""
        return max(self.values(), default=0.0)

    @property
    def floor(self) -> float:
        """Smallest retained value, ``0.0`` when empty."""
        return min(self.values(), default=0.0)

    @property
    def mean(self) -> float:
        """Average of the retained values, ``0.0`` when empty."""
        values = self.values()
        if not values:
            return 0.0
        return sum(values) / len(values)

    def clear(self) -> None:
        """Drop the retained samples; ``added`` still counts them."""
        self._samples.clear()


class HistoryBook:
    """Several named series sharing one capacity.

    Args:
        capacity: Samples kept per series.
        clock: Time source; injectable for tests.

    The book creates series on demand, so adding a graph to the UI does not
    mean editing a statistics module to declare it.
    """

    def __init__(
        self,
        capacity: int = DEFAULT_HISTORY_SAMPLES,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self._capacity = int(capacity)
        self._clock = clock
        self._series: dict[str, History] = {}

    @property
    def capacity(self) -> int:
        """Samples kept per series."""
        return self._capacity

    @property
    def names(self) -> tuple[str, ...]:
        """Every series recorded so far, in the order they first appeared."""
        return tuple(self._series)

    def __len__(self) -> int:
        return len(self._series)

    def __contains__(self, name: object) -> bool:
        return name in self._series

    def __getitem__(self, name: str) -> History:
        """The series named ``name``, created empty if it is new."""
        return self.history(name)

    def history(self, name: str) -> History:
        """The series named ``name``, created empty if it is new."""
        series = self._series.get(name)
        if series is None:
            series = History(self._capacity, clock=self._clock)
            self._series[name] = series
        return series

    def record(self, name: str, value: float, *, now: float | None = None) -> Sample:
        """Add ``value`` to the series named ``name``."""
        return self.history(name).add(value, now=now)

    def series(self, name: str) -> tuple[Sample, ...]:
        """Retained samples of one series; empty when never recorded.

        Reading does not create: a graph must not appear in the book because
        somebody asked to look at it.
        """
        series = self._series.get(name)
        return () if series is None else series.series()

    def values(self, name: str) -> tuple[float, ...]:
        """Retained values of one series."""
        series = self._series.get(name)
        return () if series is None else series.values()

    def latest(self, name: str) -> Sample | None:
        """Most recent sample of one series."""
        series = self._series.get(name)
        return None if series is None else series.latest

    def as_dict(self) -> dict[str, tuple[Sample, ...]]:
        """Every series, by name — the shape a chart widget wants."""
        return {name: series.series() for name, series in self._series.items()}

    def clear(self) -> None:
        """Drop retained samples in every series, keeping the series."""
        for series in self._series.values():
            series.clear()

    def forget(self, name: str) -> bool:
        """Remove a series entirely. Returns whether it was there."""
        return self._series.pop(name, None) is not None
