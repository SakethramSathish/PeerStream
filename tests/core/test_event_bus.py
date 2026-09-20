"""Unit tests for the event bus.

The two properties under test are the ones the engine's reliability depends on:
a broken or slow subscriber must never break the emitter, and emission must be
non-blocking but awaitable when ordering matters.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from app.core.event_bus import EventBus, Subscription
from app.core.events import Event, EventType, make_event


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


class TestSubscription:
    async def test_handler_receives_matching_events(self, bus: EventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EventType.PIECE_VERIFIED, handler)
        event = make_event(EventType.PIECE_VERIFIED, message="piece ok")
        assert bus.emit(event) == 1
        await bus.drain()

        assert received == [event]

    async def test_handler_is_filtered_by_type(self, bus: EventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EventType.PIECE_VERIFIED, handler)
        bus.emit(make_event(EventType.PEER_CONNECTED))
        await bus.drain()

        assert received == []

    async def test_subscribe_all_receives_every_event(self, bus: EventBus) -> None:
        received: list[EventType] = []

        async def handler(event: Event) -> None:
            received.append(event.type)

        bus.subscribe_all(handler)
        await bus.emit_and_wait(make_event(EventType.PEER_CONNECTED))
        await bus.emit_and_wait(make_event(EventType.PIECE_VERIFIED))

        assert received == [EventType.PEER_CONNECTED, EventType.PIECE_VERIFIED]

    async def test_unsubscribe_stops_delivery(self, bus: EventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        subscription = bus.subscribe(EventType.PEER_CONNECTED, handler)
        assert bus.unsubscribe(subscription) is True
        await bus.emit_and_wait(make_event(EventType.PEER_CONNECTED))
        assert received == []

    async def test_unsubscribe_is_idempotent(self, bus: EventBus) -> None:
        subscription = bus.subscribe(EventType.PEER_CONNECTED, lambda event: None)
        assert bus.unsubscribe(subscription) is True
        assert bus.unsubscribe(subscription) is False

    async def test_multiple_handlers_all_run(self, bus: EventBus) -> None:
        calls: list[str] = []

        async def first(event: Event) -> None:
            calls.append("first")

        async def second(event: Event) -> None:
            calls.append("second")

        bus.subscribe(EventType.PEER_CONNECTED, first)
        bus.subscribe(EventType.PEER_CONNECTED, second)
        assert bus.emit(make_event(EventType.PEER_CONNECTED)) == 2
        await bus.drain()

        assert sorted(calls) == ["first", "second"]

    def test_subscribing_a_non_callable_is_rejected(self, bus: EventBus) -> None:
        with pytest.raises(TypeError):
            bus.subscribe(EventType.PEER_CONNECTED, "not callable")  # type: ignore[arg-type]

    def test_subscription_exposes_a_readable_name(self, bus: EventBus) -> None:
        async def my_handler(event: Event) -> None:
            pass

        subscription = bus.subscribe(EventType.PEER_CONNECTED, my_handler)
        assert "my_handler" in subscription.name
        assert isinstance(subscription, Subscription)

    def test_subscriber_count(self, bus: EventBus) -> None:
        bus.subscribe(EventType.PEER_CONNECTED, lambda event: None)
        bus.subscribe(EventType.PIECE_VERIFIED, lambda event: None)
        assert bus.subscriber_count == 2


class TestEmission:
    async def test_emit_returns_the_number_of_handlers(self, bus: EventBus) -> None:
        assert bus.emit(make_event(EventType.PEER_CONNECTED)) == 0
        bus.subscribe(EventType.PEER_CONNECTED, lambda event: None)
        bus.subscribe_all(lambda event: None)
        assert bus.emit(make_event(EventType.PEER_CONNECTED)) == 2

    async def test_sync_handlers_run_immediately(self, bus: EventBus) -> None:
        calls: list[str] = []

        def handler(event: Event) -> None:
            calls.append("sync")

        bus.subscribe(EventType.PEER_CONNECTED, handler)
        bus.emit(make_event(EventType.PEER_CONNECTED))
        assert calls == ["sync"]  # no await needed

    async def test_sync_handler_arguments_are_passed(self, bus: EventBus) -> None:
        seen: list[Event] = []
        bus.subscribe(EventType.PIECE_VERIFIED, seen.append)
        event = make_event(EventType.PIECE_VERIFIED, message="ok")
        bus.emit(event)
        assert seen == [event]

    async def test_async_handlers_run_concurrently(self, bus: EventBus) -> None:
        started = asyncio.Event()
        order: list[str] = []

        async def slow(event: Event) -> None:
            order.append("slow:start")
            await asyncio.sleep(0.02)
            order.append("slow:end")

        async def fast(event: Event) -> None:
            order.append("fast")

        bus.subscribe(EventType.PEER_CONNECTED, slow)
        bus.subscribe(EventType.PEER_CONNECTED, fast)
        bus.emit(make_event(EventType.PEER_CONNECTED))
        await bus.drain()

        # "fast" completing before "slow:end" proves they overlapped.
        assert order == ["slow:start", "fast", "slow:end"]
        started.set()

    async def test_drain_waits_for_pending_handlers(self, bus: EventBus) -> None:
        completed = False

        async def handler(event: Event) -> None:
            nonlocal completed
            await asyncio.sleep(0.01)
            completed = True

        bus.subscribe(EventType.PEER_CONNECTED, handler)
        bus.emit(make_event(EventType.PEER_CONNECTED))
        assert bus.pending_count == 1
        await bus.drain()
        assert completed is True
        assert bus.pending_count == 0

    async def test_emit_and_wait_is_ordered(self, bus: EventBus) -> None:
        calls: list[int] = []

        async def handler(event: Event) -> None:
            await asyncio.sleep(0)
            calls.append(event.data["index"])

        bus.subscribe(EventType.PIECE_VERIFIED, handler)
        for index in range(5):
            await bus.emit_and_wait(make_event(EventType.PIECE_VERIFIED, data={"index": index}))

        assert calls == [0, 1, 2, 3, 4]

    def test_async_handler_without_a_loop_raises(self, bus: EventBus) -> None:
        async def handler(event: Event) -> None:
            pass  # pragma: no cover

        bus.subscribe(EventType.PEER_CONNECTED, handler)
        with pytest.raises(RuntimeError, match="running event loop"):
            bus.emit(make_event(EventType.PEER_CONNECTED))


class TestIsolation:
    async def test_async_handler_exception_does_not_propagate(
        self, bus: EventBus, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def broken(event: Event) -> None:
            raise RuntimeError("boom")

        bus.subscribe(EventType.PEER_CONNECTED, broken)
        with caplog.at_level(logging.ERROR):
            bus.emit(make_event(EventType.PEER_CONNECTED))
            await bus.drain()

        assert "boom" in caplog.text

    async def test_other_handlers_still_run_after_a_failure(self, bus: EventBus) -> None:
        calls: list[str] = []

        async def broken(event: Event) -> None:
            raise RuntimeError("boom")

        async def healthy(event: Event) -> None:
            calls.append("healthy")

        bus.subscribe(EventType.PEER_CONNECTED, broken)
        bus.subscribe(EventType.PEER_CONNECTED, healthy)
        bus.emit(make_event(EventType.PEER_CONNECTED))
        await bus.drain()

        assert calls == ["healthy"]

    async def test_sync_handler_exception_does_not_propagate(
        self, bus: EventBus, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken(event: Event) -> None:
            raise ValueError("nope")

        bus.subscribe(EventType.PEER_CONNECTED, broken)
        with caplog.at_level(logging.ERROR):
            bus.emit(make_event(EventType.PEER_CONNECTED))

        assert "nope" in caplog.text

    async def test_a_slow_subscriber_does_not_block_the_emitter(self, bus: EventBus) -> None:
        """Emission must return before handlers finish - this is the whole point."""
        release = asyncio.Event()

        async def very_slow(event: Event) -> None:
            await release.wait()

        bus.subscribe(EventType.PEER_CONNECTED, very_slow)
        bus.emit(make_event(EventType.PEER_CONNECTED))  # returns immediately
        release.set()
        await bus.drain()


class TestLifecycle:
    async def test_aclose_cancels_pending_handlers(self, bus: EventBus) -> None:
        entered = False

        async def hanging(event: Event) -> None:
            nonlocal entered
            entered = True
            await asyncio.sleep(30)

        bus.subscribe(EventType.PEER_CONNECTED, hanging)
        bus.emit(make_event(EventType.PEER_CONNECTED))
        await asyncio.sleep(0)  # let the handler reach its first await
        assert bus.pending_count == 1
        await bus.aclose()

        assert entered is True
        assert bus.pending_count == 0
        assert bus.subscriber_count == 0

    async def test_context_manager_closes(self) -> None:
        async with EventBus() as bus:
            bus.subscribe(EventType.PEER_CONNECTED, lambda event: None)
            assert bus.subscriber_count == 1
        assert bus.subscriber_count == 0

    async def test_bus_identity(self) -> None:
        async with EventBus(name="test") as bus:
            assert isinstance(bus, EventBus)
