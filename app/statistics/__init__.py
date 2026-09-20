"""Statistics engine: rolling rates, counters and bounded history buffers.

The pieces:

* :mod:`app.statistics.speed` — :class:`RateMeter` and :class:`SpeedMeter`
  measure bytes per second over trailing windows (1 s, 5 s, 30 s and the
  session). An idle meter reads zero, because a rate is something measured,
  not something remembered.
* :mod:`app.statistics.history` — :class:`History` and :class:`HistoryBook`
  keep bounded series of timestamped samples for graphs. Past the end of the
  buffer the samples are gone, so a five-minute graph cannot quietly become a
  fifty-minute one.
* :mod:`app.statistics.metrics` — :class:`MetricsCollector` reads the download
  and upload engines, counts bytes from events, and publishes
  :class:`MetricsSnapshot` values the UI can draw without inventing anything.

Nothing here estimates. When there is no measurement there is no number: ETA
is ``None`` until a rate exists, and a subsystem that is not wired up reports
zeroes rather than a guess at what it might have been doing.
"""

from app.statistics.history import History, HistoryBook, Sample
from app.statistics.metrics import MetricsCollector, MetricsSnapshot
from app.statistics.speed import Counter, CounterSet, RateMeter, SpeedMeter, SpeedRates

__all__ = [
    "Counter",
    "CounterSet",
    "History",
    "HistoryBook",
    "MetricsCollector",
    "MetricsSnapshot",
    "RateMeter",
    "Sample",
    "SpeedMeter",
    "SpeedRates",
]
