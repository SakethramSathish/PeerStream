"""Event definitions for the internal event bus (TRD §33).

Every subsystem publishes what it is doing; nothing publishes to a specific
consumer. That is what keeps the networking engine and the UI decoupled: the
engine never imports ``app.ui``, the UI never touches a socket, and the event
bus is the only thing that knows both exist (it doesn't — it only knows
:class:`Event`).

Events are immutable, timestamped records. They are also what the UI's protocol
timeline renders, so they carry a human-readable ``message`` alongside
structured ``data``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final


class EventCategory(StrEnum):
    """Coarse grouping used by the UI's log filters (PRD §10.10)."""

    NETWORK = "network"
    TRACKER = "tracker"
    PEER = "peer"
    PIECE = "piece"
    DISK = "disk"
    DHT = "dht"
    TORRENT = "torrent"
    STATISTICS = "statistics"
    SYSTEM = "system"
    ERROR = "error"


class EventType(StrEnum):
    """Every event the engine can emit."""

    # ---- torrent lifecycle
    TORRENT_ADDED = "torrent_added"
    TORRENT_STARTED = "torrent_started"
    TORRENT_PAUSED = "torrent_paused"
    TORRENT_STOPPED = "torrent_stopped"
    TORRENT_REMOVED = "torrent_removed"
    TORRENT_COMPLETED = "torrent_completed"
    TORRENT_VERIFYING = "torrent_verifying"
    TORRENT_VERIFIED = "torrent_verified"

    # ---- trackers
    TRACKER_REQUEST = "tracker_request"
    TRACKER_RESPONSE = "tracker_response"
    TRACKER_FAILED = "tracker_failed"
    TRACKER_WARNING = "tracker_warning"

    # ---- peers
    PEER_DISCOVERED = "peer_discovered"
    PEER_CONNECTING = "peer_connecting"
    PEER_CONNECTED = "peer_connected"
    PEER_HANDSHAKE = "peer_handshake"
    PEER_BITFIELD = "peer_bitfield"
    PEER_CHOKED = "peer_choked"
    PEER_UNCHOKED = "peer_unchoked"
    PEER_INTERESTED = "peer_interested"
    PEER_DISCONNECTED = "peer_disconnected"
    PEER_FAILED = "peer_failed"

    # ---- pieces
    PIECE_REQUESTED = "piece_requested"
    PIECE_BLOCK_RECEIVED = "piece_block_received"
    PIECE_DOWNLOADED = "piece_downloaded"
    PIECE_VERIFIED = "piece_verified"
    PIECE_FAILED = "piece_failed"
    PIECE_CANCELLED = "piece_cancelled"
    PIECE_UPLOADED = "piece_uploaded"

    # ---- disk / storage
    DISK_ALLOCATED = "disk_allocated"
    DISK_WRITE = "disk_write"
    DISK_DELETED = "disk_deleted"
    DISK_ERROR = "disk_error"
    RESUME_SAVED = "resume_saved"
    RESUME_LOADED = "resume_loaded"

    # ---- DHT
    DHT_STARTED = "dht_started"
    DHT_NODE_DISCOVERED = "dht_node_discovered"
    DHT_QUERY = "dht_query"
    DHT_ANNOUNCED = "dht_announced"
    DHT_RESPONSE = "dht_response"
    DHT_FAILED = "dht_failed"

    # ---- statistics / system
    STATS_SAMPLE = "stats_sample"
    SYSTEM_STARTED = "system_started"
    SYSTEM_STOPPED = "system_stopped"
    LOG = "log"


EVENT_CATEGORIES: Final[Mapping[EventType, EventCategory]] = {
    EventType.TORRENT_ADDED: EventCategory.TORRENT,
    EventType.TORRENT_STARTED: EventCategory.TORRENT,
    EventType.TORRENT_PAUSED: EventCategory.TORRENT,
    EventType.TORRENT_STOPPED: EventCategory.TORRENT,
    EventType.TORRENT_REMOVED: EventCategory.TORRENT,
    EventType.TORRENT_COMPLETED: EventCategory.TORRENT,
    EventType.TORRENT_VERIFYING: EventCategory.TORRENT,
    EventType.TORRENT_VERIFIED: EventCategory.TORRENT,
    EventType.TRACKER_REQUEST: EventCategory.TRACKER,
    EventType.TRACKER_RESPONSE: EventCategory.TRACKER,
    EventType.TRACKER_FAILED: EventCategory.ERROR,
    EventType.TRACKER_WARNING: EventCategory.TRACKER,
    EventType.PEER_DISCOVERED: EventCategory.PEER,
    EventType.PEER_CONNECTING: EventCategory.NETWORK,
    EventType.PEER_CONNECTED: EventCategory.NETWORK,
    EventType.PEER_HANDSHAKE: EventCategory.PEER,
    EventType.PEER_BITFIELD: EventCategory.PEER,
    EventType.PEER_CHOKED: EventCategory.PEER,
    EventType.PEER_UNCHOKED: EventCategory.PEER,
    EventType.PEER_INTERESTED: EventCategory.PEER,
    EventType.PEER_DISCONNECTED: EventCategory.NETWORK,
    EventType.PEER_FAILED: EventCategory.ERROR,
    EventType.PIECE_REQUESTED: EventCategory.PIECE,
    EventType.PIECE_BLOCK_RECEIVED: EventCategory.PIECE,
    EventType.PIECE_DOWNLOADED: EventCategory.PIECE,
    EventType.PIECE_VERIFIED: EventCategory.PIECE,
    EventType.PIECE_FAILED: EventCategory.ERROR,
    EventType.PIECE_CANCELLED: EventCategory.PIECE,
    EventType.PIECE_UPLOADED: EventCategory.PIECE,
    EventType.DISK_ALLOCATED: EventCategory.DISK,
    EventType.DISK_WRITE: EventCategory.DISK,
    EventType.DISK_DELETED: EventCategory.DISK,
    EventType.DISK_ERROR: EventCategory.ERROR,
    EventType.RESUME_SAVED: EventCategory.DISK,
    EventType.RESUME_LOADED: EventCategory.DISK,
    EventType.DHT_STARTED: EventCategory.DHT,
    EventType.DHT_NODE_DISCOVERED: EventCategory.DHT,
    EventType.DHT_QUERY: EventCategory.DHT,
    EventType.DHT_ANNOUNCED: EventCategory.DHT,
    EventType.DHT_RESPONSE: EventCategory.DHT,
    EventType.DHT_FAILED: EventCategory.ERROR,
    EventType.STATS_SAMPLE: EventCategory.STATISTICS,
    EventType.SYSTEM_STARTED: EventCategory.SYSTEM,
    EventType.SYSTEM_STOPPED: EventCategory.SYSTEM,
    EventType.LOG: EventCategory.SYSTEM,
}
"""Category of every event type, for log filtering and UI colour-coding."""


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable, timestamped record of something that happened.

    Attributes:
        type: What happened.
        category: Coarse grouping derived from ``type``.
        timestamp: Wall-clock time (``time.time()``) when the event was created.
        level: Logging severity, so the UI can style errors distinctly.
        torrent_id: Hex info-hash of the torrent this event belongs to, if any.
        message: One-line human-readable description for the protocol timeline.
        data: Structured payload; contents depend on ``type``.
    """

    type: EventType
    category: EventCategory
    timestamp: float = field(default_factory=time.time)
    level: int = logging.INFO
    torrent_id: str | None = None
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Render as a plain dictionary (for logging and the REST API)."""
        return {
            "type": self.type.value,
            "category": self.category.value,
            "timestamp": self.timestamp,
            "level": logging.getLevelName(self.level),
            "torrent_id": self.torrent_id,
            "message": self.message,
            "data": dict(self.data),
        }


def make_event(
    event_type: EventType,
    *,
    message: str = "",
    torrent_id: str | None = None,
    level: int = logging.INFO,
    data: Mapping[str, Any] | None = None,
) -> Event:
    """Create an event with its category filled in automatically.

    Args:
        event_type: The type of event.
        message: Human-readable one-liner shown in the protocol timeline.
        torrent_id: Hex info-hash this event relates to, if any.
        level: Logging severity (default ``INFO``).
        data: Structured payload for programmatic subscribers.

    Returns:
        The constructed :class:`Event`.
    """
    return Event(
        type=event_type,
        category=EVENT_CATEGORIES[event_type],
        message=message,
        torrent_id=torrent_id,
        level=level,
        data=dict(data) if data else {},
    )


def log_event(record: logging.LogRecord) -> Event:
    """Convert a log record into an event for the UI's log view.

    Lets the standard ``logging`` output from every module reach the interface
    without those modules knowing the event bus exists.
    """
    return Event(
        type=EventType.LOG,
        category=EventCategory.SYSTEM,
        timestamp=record.created,
        level=record.levelno,
        message=record.getMessage(),
        data={"logger": record.name},
    )
