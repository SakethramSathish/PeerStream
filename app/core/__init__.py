"""Application core: configuration, events, logging, identity and lifecycle.

Everything in here is infrastructure that the domain, networking and UI layers
build on. Nothing in here knows about torrents or peers.

Quick start::

    from app.core import Application

    async with Application.create() as app:
        app.event_bus.emit(make_event(EventType.SYSTEM_STARTED, message="hello"))

Exports:
    Config and its sections, ConfigError, default_config_path
    EventBus, Subscription, Event, EventType, EventCategory, make_event
    configure_logging, get_logger, resolve_level
    generate_peer_id, identify_peer, user_agent
    Application
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config import (
    Config,
    ConfigError,
    DhtConfig,
    DownloadConfig,
    LoggingConfig,
    NetworkConfig,
    PieceStrategy,
    StatsConfig,
    StorageConfig,
    TrackerConfig,
    UiConfig,
    default_config_path,
    replace_section,
)
from app.core.event_bus import EventBus, Handler, Subscription
from app.core.events import (
    EVENT_CATEGORIES,
    Event,
    EventCategory,
    EventType,
    log_event,
    make_event,
)
from app.core.logging_setup import (
    LOGGER_ROOT,
    ConsoleHandler,
    EventBusHandler,
    JsonFormatter,
    StructuredFormatter,
    configure_logging,
    get_logger,
    reset_logging,
    resolve_level,
)
from app.core.peer_id import (
    CLIENT_CODE,
    CLIENT_NAME,
    CLIENT_NAMES,
    client_version_string,
    generate_peer_id,
    identify_peer,
    is_valid_peer_id,
    user_agent,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.application import Application


def __getattr__(name: str) -> object:
    """Import :class:`Application` only when it is asked for.

    ``Application`` composes :mod:`app.services`, which imports the download,
    upload and peer layers, which import this package for their constants. A
    plain top-level import would therefore make ``app.core`` depend on
    everything that depends on it — a circle that only resolves by luck of
    import order. The composition root is the one thing that may know about
    every layer, so it is the one thing imported lazily.
    """
    if name == "Application":
        from app.core.application import Application as _Application

        return _Application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CLIENT_CODE",
    "CLIENT_NAME",
    "CLIENT_NAMES",
    "EVENT_CATEGORIES",
    "LOGGER_ROOT",
    "Application",
    "Config",
    "ConfigError",
    "ConsoleHandler",
    "DhtConfig",
    "DownloadConfig",
    "Event",
    "EventBus",
    "EventBusHandler",
    "EventCategory",
    "EventType",
    "Handler",
    "JsonFormatter",
    "LoggingConfig",
    "NetworkConfig",
    "PieceStrategy",
    "StatsConfig",
    "StorageConfig",
    "StructuredFormatter",
    "Subscription",
    "TrackerConfig",
    "UiConfig",
    "client_version_string",
    "configure_logging",
    "default_config_path",
    "generate_peer_id",
    "get_logger",
    "identify_peer",
    "is_valid_peer_id",
    "log_event",
    "make_event",
    "replace_section",
    "reset_logging",
    "resolve_level",
    "user_agent",
]
