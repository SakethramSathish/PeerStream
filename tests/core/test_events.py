"""Unit tests for event definitions."""

from __future__ import annotations

import logging
import time

import pytest
from app.core.events import (
    EVENT_CATEGORIES,
    EventCategory,
    EventType,
    log_event,
    make_event,
)


class TestEventTypes:
    def test_every_event_type_has_a_category(self) -> None:
        """A missing mapping would silently drop events from the UI's filters."""
        missing = [event.name for event in EventType if event not in EVENT_CATEGORIES]
        assert missing == []

    def test_categories_cover_the_expected_groups(self) -> None:
        assert set(EVENT_CATEGORIES.values()) == set(EventCategory)

    def test_error_events_are_categorised_as_errors(self) -> None:
        assert EVENT_CATEGORIES[EventType.PIECE_FAILED] is EventCategory.ERROR
        assert EVENT_CATEGORIES[EventType.TRACKER_FAILED] is EventCategory.ERROR
        assert EVENT_CATEGORIES[EventType.PEER_FAILED] is EventCategory.ERROR
        assert EVENT_CATEGORIES[EventType.DISK_ERROR] is EventCategory.ERROR

    def test_piece_events_are_categorised_as_pieces(self) -> None:
        for event_type in (
            EventType.PIECE_REQUESTED,
            EventType.PIECE_BLOCK_RECEIVED,
            EventType.PIECE_DOWNLOADED,
            EventType.PIECE_VERIFIED,
            EventType.PIECE_CANCELLED,
        ):
            assert EVENT_CATEGORIES[event_type] is EventCategory.PIECE

    def test_protocol_timeline_events_exist(self) -> None:
        """The events named in PRD §10.10 must all be representable."""
        for name in (
            "TRACKER_REQUEST",
            "TRACKER_RESPONSE",
            "PEER_CONNECTED",
            "PEER_HANDSHAKE",
            "PEER_BITFIELD",
            "PIECE_REQUESTED",
            "PIECE_VERIFIED",
            "DISK_WRITE",
        ):
            assert hasattr(EventType, name)


class TestMakeEvent:
    def test_category_is_derived_automatically(self) -> None:
        event = make_event(EventType.PIECE_VERIFIED, message="piece 42 verified")
        assert event.category is EventCategory.PIECE

    def test_defaults(self) -> None:
        before = time.time()
        event = make_event(EventType.SYSTEM_STARTED)
        assert event.message == ""
        assert event.torrent_id is None
        assert event.level == logging.INFO
        assert event.data == {}
        assert before <= event.timestamp <= time.time()

    def test_carries_structured_data(self) -> None:
        event = make_event(
            EventType.PEER_CONNECTED,
            message="connected to 10.0.0.1:6881",
            torrent_id="a" * 40,
            data={"ip": "10.0.0.1", "port": 6881},
        )
        assert event.data["ip"] == "10.0.0.1"
        assert event.torrent_id == "a" * 40

    def test_data_is_copied(self) -> None:
        payload = {"index": 1}
        event = make_event(EventType.PIECE_REQUESTED, data=payload)
        payload["index"] = 2
        assert event.data["index"] == 1

    def test_error_levels_are_supported(self) -> None:
        event = make_event(EventType.PIECE_FAILED, level=logging.ERROR, message="hash mismatch")
        assert event.level == logging.ERROR


class TestEventObject:
    def test_events_are_immutable(self) -> None:
        event = make_event(EventType.SYSTEM_STARTED)
        with pytest.raises(AttributeError):
            event.message = "changed"  # type: ignore[misc]

    def test_as_dict_is_serialisable(self) -> None:
        import json

        event = make_event(
            EventType.PIECE_VERIFIED,
            message="ok",
            torrent_id="b" * 40,
            data={"index": 7},
        )
        payload = json.loads(json.dumps(event.as_dict()))
        assert payload["type"] == "piece_verified"
        assert payload["category"] == "piece"
        assert payload["level"] == "INFO"
        assert payload["data"] == {"index": 7}
        assert payload["torrent_id"] == "b" * 40


class TestLogEvent:
    def _record(self, message: str = "hello", level: int = logging.WARNING) -> logging.LogRecord:
        return logging.LogRecord(
            name="app.peer.connection",
            level=level,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )

    def test_converts_a_record(self) -> None:
        event = log_event(self._record("handshake complete"))
        assert event.type is EventType.LOG
        assert event.category is EventCategory.SYSTEM
        assert event.message == "handshake complete"
        assert event.level == logging.WARNING
        assert event.data["logger"] == "app.peer.connection"

    def test_interpolates_message_arguments(self) -> None:
        record = logging.LogRecord(
            name="app.tracker",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="got %d peers",
            args=(26,),
            exc_info=None,
        )
        assert log_event(record).message == "got 26 peers"
