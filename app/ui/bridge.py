"""The Qt ↔ asyncio bridge: the only place that knows about both worlds.

The engine lives on an asyncio loop. Qt lives on its own event loop. Everything
either side needs to say crosses here, and it crosses in one of two ways:

**Commands go out** by handing a coroutine to the loop with
:meth:`EngineBridge.submit`, which returns a concurrent future. Nothing in the
UI ever awaits: a widget that blocks stops painting, and a widget that stops
painting looks crashed.

**Measurements come in** by *pulling* on a Qt timer. :class:`StatePump` asks
:class:`~app.services.app_state.AppState` for a snapshot a few times a second
and hands it to whoever is listening. Rates and progress are read at the moment
they are drawn, because a rate remembered from a second ago is not a rate.

**Discrete events come in** by push: the bus subscriber emits a Qt signal from
the asyncio thread, and Qt delivers it to the GUI thread as a queued call. That
is how a log line or a toast appears the moment it happens, without polling. The
events are batched (:class:`EventFeed`) because one queued call each is one
timeline refresh each, and a few hundred a second is a window that has stopped
painting.

No widget holds an engine, a socket, or a coroutine. That is the whole point.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import deque
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from PySide6.QtCore import QObject, QTimer, Signal

from app.core.events import Event
from app.services.app_state import AppSnapshot, AppState
from app.services.session import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How often the UI reads the world. 200 ms is fast enough to feel live and slow
# enough that repainting is never the bottleneck; the charts throttle
# themselves further.
DEFAULT_PUMP_INTERVAL_MS: int = 200


class EngineLoop:
    """An asyncio event loop running the engine on its own daemon thread.

    Qt owns the main thread; the network needs a loop that never blocks it.

    Example:
        >>> loop = EngineLoop()                       # doctest: +SKIP
        >>> loop.start()                              # doctest: +SKIP
        >>> loop.submit(coro)                         # doctest: +SKIP
    """

    def __init__(self, *, name: str = "engine") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The running loop.

        Raises:
            RuntimeError: If :meth:`start` has not been called.
        """
        if self._loop is None:
            raise RuntimeError("the engine loop is not running; call start() first")
        return self._loop

    @property
    def running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

    def start(self) -> asyncio.AbstractEventLoop:
        """Start the loop thread and wait until it is serving."""
        if self._loop is not None:
            return self._loop
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._thread = threading.Thread(
            target=self._run, args=(loop,), name=self._name, daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=10.0)
        return loop

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            asyncio.set_event_loop(None)
            loop.close()
            logger.info("engine loop stopped")

    def submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """Run ``coro`` on the engine loop. Never blocks the caller."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, callback: Callable[[], Any]) -> None:
        """Run a plain (thread-safe) function on the engine loop's thread."""
        self.loop.call_soon_threadsafe(callback)

    def stop(self, *, timeout: float = 10.0) -> None:
        """Stop the loop and join its thread."""
        loop, self._loop = self._loop, None
        thread, self._thread = self._thread, None
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=timeout)
        self._ready.clear()


class StatePump(QObject):
    """Reads :class:`AppState` on a timer and publishes what it found.

    Args:
        state: The state to read.
        interval_ms: How often to read it.
        parent: Qt parent.
    """

    snapshot = Signal(object)
    """Emitted with an :class:`AppSnapshot` on every tick."""

    def __init__(
        self,
        state: AppState,
        *,
        interval_ms: int = DEFAULT_PUMP_INTERVAL_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._timer = QTimer(self)
        self._timer.setInterval(max(16, interval_ms))
        self._timer.timeout.connect(self._tick)
        self._ticks = 0

    @property
    def state(self) -> AppState:
        return self._state

    @property
    def ticks(self) -> int:
        """How many snapshots have been published. For tests and status bars."""
        return self._ticks

    @property
    def interval_ms(self) -> int:
        return self._timer.interval()

    def start(self) -> None:
        """Begin pumping."""
        self._timer.start()

    def stop(self) -> None:
        """Stop pumping."""
        self._timer.stop()

    def tick(self) -> AppSnapshot:
        """Read one snapshot and publish it. Safe to call from tests."""
        snapshot = self._state.snapshot()
        self._ticks += 1
        self.snapshot.emit(snapshot)
        return snapshot

    def _tick(self) -> None:
        self.tick()


class EventFeed(QObject):
    """Pushes discrete events from the bus onto the Qt thread, in batches.

    The subscriber runs on the asyncio thread, so the signal Qt delivers is a
    queued call into the GUI thread: a log line cannot arrive halfway through a
    repaint.

    One queued call *per event* is a mistake, and an expensive one. A busy
    transfer produces a few hundred events a second — every block received,
    every piece verified — and each queued call used to refresh the timeline and
    the toasts. Measured on a 512 MiB transfer: 9,768 calls costing 26.6 s of
    GUI-thread time in a 47 s run, which is a window that has stopped painting
    while it catches up on log lines.

    So events accumulate here and cross as one batch per drain, with at most one
    queued signal outstanding: the GUI does one update for everything that
    happened since it last looked, however much that was.
    """

    events_pending = Signal()
    """Emitted (at most once per drain) when events are waiting to be read."""

    def __init__(self, state: AppState, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._remove: Callable[[], None] | None = None
        self._pending: deque[Event] = deque()
        self._lock = threading.Lock()
        self._announced = False

    def start(self) -> None:
        """Subscribe to the state's reductions."""
        if self._remove is None:
            self._remove = self._state.on_change(self._on_change)

    def stop(self) -> None:
        """Unsubscribe."""
        if self._remove is not None:
            self._remove()
            self._remove = None

    @property
    def state(self) -> AppState:
        """The state this feed is subscribed to."""
        return self._state

    @property
    def pending(self) -> int:
        """How many events are waiting. For tests and status bars."""
        with self._lock:
            return len(self._pending)

    def drain(self) -> tuple[Event, ...]:
        """Take everything that has arrived, oldest first.

        Called on the GUI thread. Clearing the announcement here is what allows
        the next arrival to raise it again: without it, one signal would be
        delivered for the whole run and the timeline would fall further and
        further behind.
        """
        with self._lock:
            events = tuple(self._pending)
            self._pending.clear()
            self._announced = False
        return events

    def _on_change(self, revision: int) -> None:
        """Runs on the asyncio thread: queue the newest event and announce once."""
        events = self._state.events
        if not events:
            return
        with self._lock:
            self._pending.append(events[-1])
            if self._announced:
                return
            self._announced = True
        self.events_pending.emit()


class EngineBridge(QObject):
    """One object that owns the loop, the pump and the feed.

    This is what the UI holds. It can submit work to the engine and it can be
    told when the world changed; it cannot make a widget touch a socket.

    Args:
        session: The session the bridge drives.
        state: The state the bridge reads; defaults to the session's own.
        interval_ms: How often to read it.
    """

    def __init__(
        self,
        session: Session,
        *,
        state: AppState | None = None,
        interval_ms: int = DEFAULT_PUMP_INTERVAL_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._state = state or AppState(session)
        self._loop = EngineLoop()
        self._pump = StatePump(self._state, interval_ms=interval_ms, parent=self)
        self._feed = EventFeed(self._state, parent=self)

    # ------------------------------------------------------------------ wiring

    @property
    def session(self) -> Session:
        return self._session

    @property
    def state(self) -> AppState:
        return self._state

    @property
    def pump(self) -> StatePump:
        return self._pump

    @property
    def feed(self) -> EventFeed:
        return self._feed

    def start(self) -> None:
        """Start the engine loop, the pump and the event feed."""
        self._loop.start()
        self._pump.start()
        self._feed.start()

    def stop(self) -> None:
        """Stop everything, in the order that loses nothing."""
        self._pump.stop()
        self._feed.stop()
        self._loop.stop()

    # ----------------------------------------------------------------- actions

    def submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """Run a coroutine on the engine loop. The UI never awaits it."""
        return self._loop.submit(coro)

    def refresh(self) -> AppSnapshot:
        """Read the world now, out of band. Used when an action must be visible
        immediately rather than at the next tick."""
        return self._pump.tick()
