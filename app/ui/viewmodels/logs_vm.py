"""The protocol timeline's view model (PRD §10.10).

The engine already records every event worth recording: trackers answering,
peers handshaking, pieces verifying, disk errors. This view model is the part
that remembers them, bounded, and filters them the way the log tab's buttons
say it will.

Two decisions worth writing down:

* **The buffer is bounded.** 500 events by default. A session that ran for a
  week is not a log the interface should be holding in memory, and a list that
  grew without limit is a leak with a friendly face.
* **ERROR is a filter, not a category.** The engine categories are about *what*
  happened; error-ness is about *how it went*. A disk error is categorised
  ``DISK`` and a tracker failure ``ERROR``, and both belong under the ERROR
  filter. So the filter matches either.

Nothing here polls. Events are pushed in by whoever receives them — the bridge's
event feed — as they happen.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from app.core.events import Event, EventCategory

# The filters, in the order the tab shows them. ``ALL`` first, as it is the
# default: the timeline is most useful before it is narrowed.
FILTERS: tuple[str, ...] = ("ALL", "NETWORK", "TRACKER", "PEER", "PIECE", "DISK", "ERROR")

# How much is remembered. Old events fall off the end, newest first.
CAPACITY: int = 500

# Below this level an event is ordinary; at or above it, it is an error.
ERROR_LEVEL: int = logging.ERROR


@dataclass(frozen=True, slots=True)
class LogRow:
    """One line of the timeline, ready to draw.

    Attributes:
        when: ``HH:MM:SS`` of the event, in local time.
        category: The engine's category, e.g. ``"peer"``.
        kind: The event type, e.g. ``"peer_connected"``.
        message: The one-line description.
        torrent: Which torrent it belongs to, if any.
        error: Whether it should be styled as an error.
    """

    when: str
    category: str
    kind: str
    message: str
    torrent: str | None
    error: bool

    def matches(self, query: str) -> bool:
        """Case-insensitive search over kind, message and torrent."""
        if not query:
            return True
        needle = query.casefold()
        return (
            needle in self.kind.casefold()
            or needle in self.message.casefold()
            or needle in (self.torrent or "").casefold()
        )

    def as_dict(self) -> dict[str, object]:
        """The row as plain data."""
        return {
            "when": self.when,
            "category": self.category,
            "kind": self.kind,
            "message": self.message,
            "torrent": self.torrent,
            "error": self.error,
        }


def row_for(event: Event) -> LogRow:
    """Turn an engine event into a timeline row.

    The category is read by value rather than by identity: an event built by
    :func:`~app.core.events.make_event` carries the enum, and one assembled by
    hand (in a test, or from a replayed log) may carry the plain string. Both
    are the same thing said two ways, and neither should raise.
    """
    category = _value_of(event.category)
    return LogRow(
        when=datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S"),
        category=category,
        kind=_value_of(event.type),
        message=event.message,
        torrent=event.torrent_id,
        error=category == str(EventCategory.ERROR) or event.level >= ERROR_LEVEL,
    )


def _value_of(value: object) -> str:
    """The string an enum carries, or the string itself."""
    return str(getattr(value, "value", value))


class LogsViewModel(QObject):
    """The timeline: a bounded, filtered record of what happened.

    Args:
        capacity: How many events to remember.
        parent: Qt parent.
    """

    changed = Signal(object)

    def __init__(self, capacity: int = CAPACITY, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._capacity = max(1, capacity)
        self._rows: list[LogRow] = []
        self._filter = "ALL"
        self._query = ""
        self._torrent: str | None = None
        self._last: LogRow | None = None

    # ------------------------------------------------------------------- input

    def add(self, event: Event) -> LogRow:
        """Record one event. Returns the row, newest first."""
        return self.add_many((event,))[0]

    def add_many(self, events: Sequence[Event]) -> tuple[LogRow, ...]:
        """Record a batch of events, with one refresh for the whole batch.

        A busy transfer produces a few hundred events a second. Refreshing the
        timeline once per event costs more than the timeline is worth: measured
        on a 512 MiB transfer, 9,768 single-event updates took 26.6 s of
        GUI-thread time in a 47 s run. A batch is one update, so the window
        keeps painting.

        Returns:
            One row per event, in the order they were given.
        """
        rows = tuple(row_for(event) for event in events)
        if not rows:
            return ()
        self._rows.extend(rows)
        if len(self._rows) > self._capacity:
            del self._rows[: len(self._rows) - self._capacity]
        self._last = rows[-1]
        self.changed.emit(self)
        return rows

    def clear(self) -> None:
        """Forget the timeline."""
        self._rows.clear()
        self._last = None
        self.changed.emit(self)

    # ----------------------------------------------------------------- filters

    @property
    def filtername(self) -> str:
        """The active filter, always one of :data:`FILTERS`."""
        return self._filter

    def set_filter(self, name: str) -> None:
        """Filter by category, or by ``"ERROR"`` for anything that went wrong.

        Unknown names fall back to ``ALL``: a bad filter should show
        everything, not nothing.
        """
        wanted = name.strip().upper()
        self._filter = wanted if wanted in FILTERS else "ALL"
        self.changed.emit(self)

    @property
    def query(self) -> str:
        """The search text."""
        return self._query

    def set_query(self, text: str) -> None:
        """Search the timeline. Empty means everything."""
        self._query = text
        self.changed.emit(self)

    @property
    def torrent(self) -> str | None:
        """Restrict to one torrent, or ``None`` for all of them."""
        return self._torrent

    def set_torrent(self, hex_info_hash: str | None) -> None:
        """Scope the timeline to one torrent, or to the whole session."""
        self._torrent = hex_info_hash
        self.changed.emit(self)

    # ------------------------------------------------------------------ access

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def recorded(self) -> int:
        """How many events are being held, filtered or not."""
        return len(self._rows)

    @property
    def last(self) -> LogRow | None:
        """The most recent event, whatever the filters say."""
        return self._last

    def rows(self) -> tuple[LogRow, ...]:
        """What the timeline shows: newest first, filtered and searched."""
        wanted = self._filter
        scoped = self._torrent
        visible: list[LogRow] = []
        for row in reversed(self._rows):
            if scoped is not None and row.torrent not in (None, scoped):
                continue
            if wanted == "ERROR":
                if not row.error:
                    continue
            elif wanted != "ALL" and row.category.upper() != wanted:
                continue
            if not row.matches(self._query):
                continue
            visible.append(row)
        return tuple(visible)

    def as_dict(self) -> dict[str, object]:
        """The timeline's state, for tests and for the CLI."""
        errors = sum(1 for row in self._rows if row.error)
        return {
            "recorded": len(self._rows),
            "visible": len(self.rows()),
            "filter": self._filter,
            "query": self._query,
            "torrent": self._torrent,
            "errors": errors,
            "last": None if self._last is None else self._last.as_dict(),
        }
