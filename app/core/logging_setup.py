"""Structured logging configuration.

Two audiences read the logs: humans (console, rotating file) and the UI's log
view. Both are served from one configuration so the protocol timeline shows
exactly what the engine logged, rather than a parallel logging system that can
drift.

Deliberate choices:

* **Only the ``app`` package logger is configured.** A library-style client
  must not reconfigure the root logger of whatever process hosts it — doing so
  hijacks the host application's logging.
* **``propagate = False``** once our handlers are installed, so records are
  emitted once, not twice.
* **Log forwarding is re-entrancy safe.** The UI log handler pushes records
  into the event bus, and the bus logs its own errors; a context guard stops
  that cycle from becoming infinite recursion.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any, ClassVar, Final

from app.core.config import LoggingConfig
from app.core.event_bus import EventBus
from app.core.events import log_event

LOGGER_ROOT: Final[str] = "app"
"""Root logger name for the package; only this tree is configured."""

LOG_FILENAME: Final[str] = "client.log"
DEFAULT_LOG_FORMAT: Final[str] = "%(asctime)s.%(msecs)03d  %(levelname)-7s %(name)-26s %(message)s"
DEFAULT_DATE_FORMAT: Final[str] = "%H:%M:%S"

_LEVELS: Final[dict[str, int]] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}

_STANDARD_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

_forwarding = contextvars.ContextVar("bittorrent_log_forwarding", default=False)


class ManagedHandler:
    """Marker for handlers installed by :func:`configure_logging`.

    Reconfiguration removes only these, leaving any handler a host application
    may have attached to the logger alone.
    """

    managed: ClassVar[bool] = True


class ConsoleHandler(ManagedHandler, logging.StreamHandler[IO[str]]):
    """stderr handler owned by this module."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        super().__init__(stream or sys.stderr)


class RotatingLogFileHandler(ManagedHandler, RotatingFileHandler):
    """Rotating file handler owned by this module."""


class EventBusHandler(ManagedHandler, logging.Handler):
    """Forwards log records into the event bus for the UI log view.

    Attributes:
        bus: The bus that records are published to.
    """

    def __init__(self, bus: EventBus, level: int = logging.NOTSET) -> None:
        super().__init__(level=level)
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        """Publish the record as a :class:`Event`, guarding against recursion."""
        if _forwarding.get():
            return
        token = _forwarding.set(True)
        try:
            self.bus.emit(log_event(record))
        except RuntimeError as exc:
            # No running loop (e.g. logging during interpreter shutdown).
            self.handleError(record)
            logging.getLogger(__name__).debug("log forwarding skipped: %s", exc)
        except Exception:  # noqa: BLE001 - logging must never raise into callers
            self.handleError(record)
        finally:
            _forwarding.reset(token)


class StructuredFormatter(logging.Formatter):
    """Human-readable formatter with millisecond timestamps.

    Example::

        12:41:03.184  INFO     app.peer.connection   handshake complete
    """

    def __init__(self) -> None:
        super().__init__(fmt=DEFAULT_LOG_FORMAT, datefmt=DEFAULT_DATE_FORMAT)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shipping and machine analysis."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def resolve_level(value: str | int) -> int:
    """Resolve a log level from a name, number, or anything else.

    Unknown values fall back to ``INFO`` rather than raising: a typo in the
    config file should not stop the client from starting.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        level = _LEVELS.get(value.strip().upper())
        if level is not None:
            return level
    logging.getLogger(__name__).warning("unknown log level %r; falling back to INFO", value)
    return logging.INFO


def configure_logging(
    config: LoggingConfig | None = None,
    *,
    event_bus: EventBus | None = None,
    stream: IO[str] | None = None,
) -> logging.Logger:
    """Configure logging for the ``app`` package.

    Args:
        config: Logging settings. Defaults are used when omitted.
        event_bus: When provided, log records are also published as events so
            the UI can display them.
        stream: Override the console stream (used by tests).

    Returns:
        The configured package logger.
    """
    settings = config or LoggingConfig()
    package_logger = logging.getLogger(LOGGER_ROOT)

    for handler in list(package_logger.handlers):
        if isinstance(handler, ManagedHandler):
            package_logger.removeHandler(handler)
            handler.close()

    level = resolve_level(settings.level)
    package_logger.setLevel(level)
    package_logger.propagate = False

    formatter: logging.Formatter = (
        JsonFormatter() if settings.json_format else StructuredFormatter()
    )

    console = ConsoleHandler(stream)
    console.setFormatter(formatter)
    console.setLevel(level)
    package_logger.addHandler(console)

    if settings.directory is not None:
        directory = Path(settings.directory).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingLogFileHandler(
            directory / LOG_FILENAME,
            maxBytes=max(settings.max_bytes, 1024),
            backupCount=max(settings.backup_count, 0),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        package_logger.addHandler(file_handler)

    if event_bus is not None:
        bus_handler = EventBusHandler(event_bus)
        bus_handler.setLevel(level)
        package_logger.addHandler(bus_handler)

    return package_logger


def reset_logging() -> None:
    """Remove handlers installed by this module (used in tests)."""
    package_logger = logging.getLogger(LOGGER_ROOT)
    for handler in list(package_logger.handlers):
        if isinstance(handler, ManagedHandler):
            package_logger.removeHandler(handler)
            handler.close()
    package_logger.setLevel(logging.NOTSET)
    package_logger.propagate = True  # restore default so pytest's caplog works


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``app`` package tree."""
    return logging.getLogger(name if name.startswith(LOGGER_ROOT) else f"{LOGGER_ROOT}.{name}")
