"""Asynchronous publish/subscribe event bus.

Why not Qt signals or plain callbacks: the engine runs on the asyncio event
loop, often with dozens of concurrent peer tasks, while consumers (UI, REST
API, statistics, logging) have wildly different latencies. A bus lets producers
fire and forget, and lets consumers be added without touching producer code.

Two properties matter for correctness:

**A slow or broken subscriber must never affect the engine.** Handlers run as
independent tasks; an exception in one is logged and isolated, and no handler
can block the loop that emitted the event. This is why the download engine can
emit thousands of events per second without a UI hiccup turning into a stall.

**Emission is non-blocking but trackable.** :meth:`emit` schedules handlers and
returns immediately. :meth:`drain` awaits everything in flight, which is what
shutdown and tests use to establish ordering.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import count
from typing import Final

from app.core.events import Event, EventType

logger = logging.getLogger(__name__)

SyncHandler = Callable[[Event], None]
AsyncHandler = Callable[[Event], Awaitable[None]]
Handler = SyncHandler | AsyncHandler

_LOGGER_PREFIXES_TO_IGNORE: Final[tuple[str, ...]] = (__name__, "asyncio")


@dataclass(frozen=True, slots=True)
class Subscription:
    """Handle returned by :meth:`EventBus.subscribe`, used to unsubscribe.

    Attributes:
        token: Unique identifier for this subscription.
        event_type: The subscribed event type, or ``None`` for all events.
        handler: The registered callable.
    """

    token: int
    event_type: EventType | None
    handler: Handler

    @property
    def name(self) -> str:
        """Readable name of the handler, for logs."""
        return getattr(self.handler, "__qualname__", repr(self.handler))


class EventBus:
    """Fan-out event dispatcher with isolated async handlers.

    Handlers may be synchronous functions (called immediately — they must be
    cheap) or coroutine functions (scheduled as tasks). Sync handlers are for
    bookkeeping such as counters; anything that might block should be async.
    """

    __slots__ = ("_name", "_next_token", "_subscriptions", "_tasks")

    def __init__(self, *, name: str = "event-bus") -> None:
        self._name = name
        self._subscriptions: list[Subscription] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._next_token = count(1)

    # ------------------------------------------------------------ subscribe

    def subscribe(
        self,
        event_type: EventType | None,
        handler: Handler,
    ) -> Subscription:
        """Register a handler for one event type (or all, when ``None``).

        Args:
            event_type: The event type to receive, or ``None`` for every event.
            handler: A sync callable or a coroutine function taking an
                :class:`~app.core.events.Event`.

        Returns:
            A :class:`Subscription` to pass to :meth:`unsubscribe`.
        """
        if not callable(handler):
            raise TypeError(f"handler must be callable, got {type(handler).__name__}")
        subscription = Subscription(next(self._next_token), event_type, handler)
        self._subscriptions.append(subscription)
        return subscription

    def subscribe_all(self, handler: Handler) -> Subscription:
        """Register a handler that receives every event."""
        return self.subscribe(None, handler)

    def unsubscribe(self, subscription: Subscription) -> bool:
        """Remove a subscription. Returns True if it was registered."""
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            return False
        return True

    # ----------------------------------------------------------------- emit

    def emit(self, event: Event) -> int:
        """Publish an event without waiting for handlers.

        Args:
            event: The event to publish.

        Returns:
            The number of handlers that were notified.

        Raises:
            RuntimeError: If called with no running event loop and an async
                handler is registered.
        """
        matched = [
            subscription
            for subscription in self._subscriptions
            if subscription.event_type is None or subscription.event_type == event.type
        ]
        for subscription in matched:
            self._dispatch(subscription, event)
        return len(matched)

    async def emit_and_wait(self, event: Event) -> int:
        """Publish an event and wait for all triggered handlers to finish."""
        count = self.emit(event)
        if count:
            await self.drain()
        return count

    async def drain(self) -> None:
        """Wait for every in-flight handler task to complete."""
        while self._tasks:
            tasks = list(self._tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

    # -------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        """Cancel pending handlers and clear subscriptions."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._subscriptions.clear()

    async def __aenter__(self) -> EventBus:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------ properties

    @property
    def subscriber_count(self) -> int:
        """Number of registered subscriptions (all types)."""
        return len(self._subscriptions)

    @property
    def pending_count(self) -> int:
        """Number of handler tasks currently in flight."""
        return len(self._tasks)

    # -------------------------------------------------------------- internals

    def _dispatch(self, subscription: Subscription, event: Event) -> None:
        """Run or schedule one handler, isolating failures."""
        handler = subscription.handler
        if inspect.iscoroutinefunction(handler):
            self._schedule(subscription, handler, event)
            return

        try:
            handler(event)
        except Exception:
            # A broken subscriber must never break the emitter.
            logger.exception("event handler %s failed for %s", subscription.name, event.type.value)

    def _schedule(self, subscription: Subscription, handler: AsyncHandler, event: Event) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            logger.error(
                "cannot dispatch %s to async handler %s outside a running loop",
                event.type.value,
                subscription.name,
            )
            raise RuntimeError(
                f"async handler {subscription.name} requires a running event loop"
            ) from exc

        task = loop.create_task(self._run(subscription, handler, event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, subscription: Subscription, handler: AsyncHandler, event: Event) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event handler %s failed for %s", subscription.name, event.type.value)
