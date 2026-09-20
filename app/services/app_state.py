"""Observable application state: the one thing the UI is allowed to read.

A Qt widget must never hold a socket, and it must never reach into an engine to
ask how fast it is going. It reads :class:`AppState` instead. This module is
the reducer half of that arrangement:

    Engine ──emit──▶ EventBus ──▶ AppState.reduce() ──▶ UI refresh

Two kinds of truth live here, and it is worth being precise about which is
which:

* **Pushed truth** — events. Every event that passes over the bus is reduced
  into the recent-events ring and into per-type counters. This is how the
  protocol timeline stays complete even for things no counter summarises.
* **Pulled truth** — measurements. Rates, progress, and peer counts are read
  from the session *at snapshot time*, because a cached copy of a rate is a
  stale rate, and a stale rate is a lie.

Nothing in this module invents a value. Where a number has not been measured,
it is zero or ``None``.

This layer is deliberately Qt-free: it is plain Python, so it can be tested
without a display and reused by the CLI. The Qt bridge that turns a new
revision into a queued signal arrives with the UI.
"""

from __future__ import annotations

import contextlib
import logging
from collections import Counter as _Counter
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from time import time

from app.core.event_bus import EventBus, Subscription
from app.core.events import Event, EventType
from app.services.session import Session, SessionTotals
from app.services.torrent_service import TorrentView

logger = logging.getLogger(__name__)

# How many events to keep for the timeline. One torrent at full speed emits a
# few dozen a second, so this is a couple of minutes of history — enough to
# scroll back through a stall and see what happened, small enough to keep.
DEFAULT_EVENT_CAPACITY: int = 2000


@dataclass(frozen=True, slots=True)
class AppSnapshot:
    """Everything the UI needs to draw one frame. Immutable by design."""

    totals: SessionTotals
    torrents: tuple[TorrentView, ...]
    events: tuple[Event, ...]
    counts: tuple[tuple[str, int], ...]
    revision: int
    generated_at: float

    @property
    def active_torrents(self) -> tuple[TorrentView, ...]:
        """The torrents that are transferring right now."""
        return tuple(view for view in self.torrents if view.active)


class AppState:
    """Reduces events and serves snapshots.

    Args:
        session: Optional session to observe; :meth:`attach` works too.
        event_capacity: How many recent events to keep.
        bus: Optional bus to reduce from, when there is no session yet.

    Example:
        >>> state = AppState()                      # doctest: +SKIP
        >>> state.attach(session)                   # doctest: +SKIP
        >>> snap = state.snapshot()                 # doctest: +SKIP
        >>> snap.totals.download_rate               # doctest: +SKIP
        0.0
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        event_capacity: int = DEFAULT_EVENT_CAPACITY,
        bus: EventBus | None = None,
    ) -> None:
        if event_capacity <= 0:
            raise ValueError(f"event capacity must be positive, got {event_capacity}")
        self._session = session
        self._bus = bus or (session.event_bus if session is not None else None)
        self._events: deque[Event] = deque(maxlen=event_capacity)
        self._counts: _Counter[str] = _Counter()
        self._revision = 0
        self._subscription: Subscription | None = None
        self._listeners: list[Callable[[int], None]] = []
        if self._bus is not None:
            self.subscribe_to(self._bus)

    # ---------------------------------------------------------------- wiring

    @property
    def session(self) -> Session | None:
        return self._session

    @property
    def revision(self) -> int:
        """Monotonic counter, bumped on every reduction. The UI's dirty flag."""
        return self._revision

    def subscribe_to(self, bus: EventBus) -> Subscription:
        """Start reducing events from ``bus``. Idempotent per bus."""
        if self._subscription is not None:
            self.detach()
        self._bus = bus
        self._subscription = bus.subscribe_all(self.reduce)
        return self._subscription

    def attach(self, session: Session) -> Subscription:
        """Observe ``session``: reduce its bus and read its torrents."""
        self._session = session
        return self.subscribe_to(session.event_bus)

    def detach(self) -> None:
        """Stop reducing events. The events already collected are kept."""
        if self._subscription is not None and self._bus is not None:
            self._bus.unsubscribe(self._subscription)
        self._subscription = None

    def on_change(self, handler: Callable[[int], None]) -> Callable[[], None]:
        """Register a listener called with the new revision after a reduction.

        Returns:
            A callable that unregisters the listener — the Qt bridge needs to
            disconnect when a window closes.
        """
        if not callable(handler):
            raise TypeError(f"handler must be callable, got {type(handler).__name__}")
        self._listeners.append(handler)

        def remove() -> None:
            with _suppress(ValueError):
                self._listeners.remove(handler)

        return remove

    # --------------------------------------------------------------- reducer

    def reduce(self, event: Event) -> None:
        """Fold one event into the state, then tell the listeners."""
        self._events.append(event)
        self._counts[event.type.value] += 1
        self._revision += 1
        for listener in tuple(self._listeners):
            with _suppress(Exception, label="state listener"):
                listener(self._revision)

    # ------------------------------------------------------------ inspection

    @property
    def events(self) -> tuple[Event, ...]:
        """Recent events, oldest first. This is the protocol timeline."""
        return tuple(self._events)

    def events_for(self, hex_info_hash: str) -> tuple[Event, ...]:
        """Recent events for one torrent."""
        return tuple(event for event in self._events if event.torrent_id == hex_info_hash)

    def count(self, event_type: EventType) -> int:
        """How many events of this type have been reduced."""
        return self._counts[event_type.value]

    def counts(self) -> dict[str, int]:
        """Every event type seen, with its count, most common first."""
        return dict(self._counts.most_common())

    def clear(self) -> None:
        """Forget the collected events and counts (not the session)."""
        self._events.clear()
        self._counts.clear()
        self._revision += 1

    def snapshot(self) -> AppSnapshot:
        """Read the world as it is right now.

        Measurements are taken here, not cached: a rate remembered from a
        second ago is not a rate.
        """
        session = self._session
        torrents: tuple[TorrentView, ...] = ()
        totals = SessionTotals(
            torrents=0,
            active=0,
            download_rate=0.0,
            upload_rate=0.0,
            downloaded_bytes=0,
            uploaded_bytes=0,
        )
        if session is not None:
            torrents = tuple(service.view() for service in session.services)
            totals = session.totals()
        return AppSnapshot(
            totals=totals,
            torrents=torrents,
            events=self.events,
            counts=tuple(self._counts.most_common()),
            revision=self._revision,
            generated_at=time(),
        )

    def render(self) -> str:
        """A multi-line text summary — what the CLI and the tests read."""
        snapshot = self.snapshot()
        totals = snapshot.totals
        lines = [
            f"torrents: {totals.torrents} ({totals.active} active)",
            f"download: {_rate(totals.download_rate)}  uploaded: {_rate(totals.upload_rate)}",
            f"transferred: {_size(totals.downloaded_bytes)} down / "
            f"{_size(totals.uploaded_bytes)} up",
        ]
        for view in snapshot.torrents:
            lines.append(
                f"  {view.info_hash[:8]}  {view.state.value:<12} "
                f"{view.progress * 100:6.2f}%  "
                f"{view.verified_pieces}/{view.piece_count} pieces"
                + (f"  [{view.error}]" if view.error else "")
            )
        if snapshot.events:
            last = snapshot.events[-1]
            lines.append(f"last event: [{last.type.value}] {last.message}")
        return "\n".join(lines)


def _rate(value: float) -> str:
    """Bytes per second, humanised."""
    return f"{_size(int(value))}/s"


def _size(value: int) -> str:
    """Bytes, humanised. Binary units, because that is what disks use."""
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024.0:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024.0
    return f"{amount:.1f} TiB"


@contextlib.contextmanager
def _suppress(*errors: type[BaseException], label: str = "") -> Iterator[None]:
    """Swallow (and log) ``errors`` inside a ``with`` block.

    A listener that raises must not stop the other listeners from hearing the
    event — the UI's bugs are not the engine's problem.
    """
    try:
        yield
    except errors as error:
        if label:
            logger.warning("%s failed: %s", label, error)


__all__ = ["DEFAULT_EVENT_CAPACITY", "AppSnapshot", "AppState"]
