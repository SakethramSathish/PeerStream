"""Unit tests for logging configuration.

These tests deliberately do not use ``caplog``: :func:`configure_logging`
attaches handlers to the ``app`` logger with ``propagate = False``, which is
the correct behaviour for a library-style package but bypasses pytest's root
handler. Behaviour is asserted against real handler output instead.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest
from app.core.event_bus import EventBus
from app.core.events import EventType
from app.core.logging_setup import (
    LOGGER_ROOT,
    EventBusHandler,
    JsonFormatter,
    StructuredFormatter,
    configure_logging,
    get_logger,
    reset_logging,
    resolve_level,
)


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    """Ensure no test leaks log configuration into another."""
    yield
    reset_logging()


class TestResolveLevel:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("DEBUG", logging.DEBUG),
            ("info", logging.INFO),
            ("Warning", logging.WARNING),
            ("ERROR", logging.ERROR),
            (logging.CRITICAL, logging.CRITICAL),
        ],
    )
    def test_resolves_names_and_numbers(self, value: object, expected: int) -> None:
        assert resolve_level(value) == expected  # type: ignore[arg-type]

    def test_unknown_level_falls_back_to_info(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert resolve_level("VERBOSE") == logging.INFO
        assert "unknown log level" in caplog.text


class TestConfiguration:
    def test_installs_a_console_handler(self) -> None:
        stream = io.StringIO()
        logger = configure_logging(stream=stream)
        logger.info("hello")

        assert logger.name == LOGGER_ROOT
        assert "hello" in stream.getvalue()

    def test_respects_the_configured_level(self) -> None:
        stream = io.StringIO()
        logger = configure_logging(stream=stream)
        logger.setLevel(logging.WARNING)
        logger.info("suppressed")
        logger.warning("shown")

        output = stream.getvalue()
        assert "suppressed" not in output
        assert "shown" in output

    def test_reconfiguration_does_not_duplicate_handlers(self) -> None:
        first = io.StringIO()
        second = io.StringIO()

        configure_logging(stream=first)
        logger = configure_logging(stream=second)
        logger.warning("once")

        assert "once" not in first.getvalue()
        assert second.getvalue().count("once") == 1

    def test_does_not_propagate_to_the_root_logger(self) -> None:
        configure_logging(stream=io.StringIO())
        assert logging.getLogger(LOGGER_ROOT).propagate is False

    def test_child_loggers_are_covered(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("app.peer.connection").info("from a child logger")
        assert "from a child logger" in stream.getvalue()

    def test_reset_restores_propagation(self) -> None:
        configure_logging(stream=io.StringIO())
        reset_logging()
        assert logging.getLogger(LOGGER_ROOT).propagate is True


class TestFormatters:
    def test_structured_format_includes_time_level_and_logger(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("app.tracker").info("announce sent")
        line = stream.getvalue().strip()

        assert "INFO" in line
        assert "app.tracker" in line
        assert "announce sent" in line
        assert line.count(":") >= 2  # HH:MM:SS

    def test_json_format_emits_one_object_per_line(self, tmp_path: Path) -> None:
        from app.core.config import LoggingConfig

        directory = tmp_path / "logs"
        configure_logging(LoggingConfig(level="INFO", directory=directory, json_format=True))
        logging.getLogger("app.peer").info("handshake complete")
        reset_logging()

        lines = [line for line in (directory / "client.log").read_text().splitlines() if line]
        payload = json.loads(lines[-1])
        assert payload["message"] == "handshake complete"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.peer"

    def test_json_format_includes_extra_fields(self, tmp_path: Path) -> None:
        from app.core.config import LoggingConfig

        directory = tmp_path / "logs"
        configure_logging(LoggingConfig(level="DEBUG", directory=directory, json_format=True))
        logging.getLogger("app.peer").warning("slow peer", extra={"peer": "10.0.0.1"})
        reset_logging()

        payload = json.loads((directory / "client.log").read_text().splitlines()[-1])
        assert payload["peer"] == "10.0.0.1"

    def test_formatters_can_be_used_directly(self) -> None:
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="value %s",
            args=(42,),
            exc_info=None,
        )
        assert "value 42" in StructuredFormatter().format(record)
        assert json.loads(JsonFormatter().format(record))["message"] == "value 42"


class TestFileLogging:
    def test_creates_a_rotating_log_file(self, tmp_path: Path) -> None:
        from app.core.config import LoggingConfig

        directory = tmp_path / "nested" / "logs"
        configure_logging(LoggingConfig(level="DEBUG", directory=directory))
        logging.getLogger("app.storage").debug("piece written")
        reset_logging()

        content = (directory / "client.log").read_text()
        assert "piece written" in content

    def test_no_file_handler_without_a_directory(self, tmp_path: Path) -> None:
        from app.core.config import LoggingConfig

        configure_logging(LoggingConfig(level="INFO"), stream=io.StringIO())
        logger = logging.getLogger(LOGGER_ROOT)
        assert not any(isinstance(handler, logging.FileHandler) for handler in logger.handlers)
        assert not any(tmp_path.iterdir())


class TestEventForwarding:
    async def test_records_are_published_as_events(self) -> None:
        from app.core.config import LoggingConfig

        bus = EventBus()
        received: list[str] = []

        async def collect(event):  # type: ignore[no-untyped-def]
            received.append(event.message)

        bus.subscribe(EventType.LOG, collect)
        configure_logging(LoggingConfig(level="INFO"), event_bus=bus, stream=io.StringIO())
        logging.getLogger("app.peer").info("handshake complete")
        await bus.drain()
        reset_logging()

        assert "handshake complete" in received

    async def test_forwarding_does_not_recurse(self) -> None:
        """A handler that logs must not create an infinite event loop."""
        from app.core.config import LoggingConfig

        bus = EventBus()
        seen: list[str] = []

        async def logs_back(event):  # type: ignore[no-untyped-def]
            seen.append(event.message)
            logging.getLogger("app.core.event_bus").warning("handler ran: %s", event.message)

        bus.subscribe(EventType.LOG, logs_back)
        configure_logging(LoggingConfig(level="INFO"), event_bus=bus, stream=io.StringIO())
        logging.getLogger("app.peer").info("origin")
        await bus.drain()
        reset_logging()

        assert seen == ["origin"]  # the handler's own log was not re-forwarded

    def test_tolerates_a_bus_without_a_running_loop(self) -> None:
        """Logging during startup or shutdown must not raise into the caller."""
        bus = EventBus()

        async def handler(event: object) -> None:
            pass  # pragma: no cover

        bus.subscribe(EventType.LOG, handler)  # type: ignore[arg-type]
        forwarding = EventBusHandler(bus)
        record = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="no loop here",
            args=(),
            exc_info=None,
        )
        forwarding.emit(record)  # must not raise
        forwarding.close()

    def test_tolerates_an_unformattable_record(self) -> None:
        """A record that cannot be converted must not break logging."""
        forwarding = EventBusHandler(EventBus())
        broken = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="count: %d",
            args=("not a number",),
            exc_info=None,
        )
        forwarding.emit(broken)  # must not raise
        forwarding.close()

    def test_json_format_includes_exceptions(self, tmp_path: Path) -> None:
        from app.core.config import LoggingConfig

        directory = tmp_path / "logs"
        configure_logging(LoggingConfig(level="ERROR", directory=directory, json_format=True))
        try:
            raise ValueError("disk full")
        except ValueError:
            logging.getLogger("app.storage").exception("write failed")
        reset_logging()

        payload = json.loads((directory / "client.log").read_text().splitlines()[-1])
        assert payload["message"] == "write failed"
        assert "ValueError: disk full" in payload["exception"]

    def test_handler_exposes_the_bus(self) -> None:
        bus = EventBus()
        handler = EventBusHandler(bus)
        assert handler.bus is bus
        handler.close()


class TestGetLogger:
    def test_prefixes_bare_names(self) -> None:
        assert get_logger("peer").name == "app.peer"

    def test_leaves_qualified_names_alone(self) -> None:
        assert get_logger("app.peer.connection").name == "app.peer.connection"
