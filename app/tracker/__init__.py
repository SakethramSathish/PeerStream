"""Tracker clients: peer discovery via HTTP/HTTPS (BEP 3) and UDP (BEP 15).

A tracker is a rendezvous point: we tell it which torrent we want and which port
we listen on, and it replies with peers plus how long to wait before asking
again. The tracker manager (M3) handles tiers, retries, backoff and health; the
peer manager (M5) consumes the resulting :class:`PeerAddress` values without
caring which tracker produced them.

Quick start::

    from app.tracker import TrackerManager

    manager = TrackerManager(torrent, port=6881, event_bus=bus)
    outcome = await manager.announce(event=TrackerEvent.STARTED)
    print(outcome.new_peers, outcome.next_announce_in)

Exports:
    HttpTracker, UdpTracker, TrackerManager, build_tracker_tiers, build_tracker
    AnnounceRequest, AnnounceResponse, ScrapeResponse, PeerAddress
    TrackerEvent, TrackerStatus, TrackerState
    TrackerError and subclasses
"""

from __future__ import annotations

from app.tracker.base import (
    DEFAULT_ANNOUNCE_INTERVAL,
    DEFAULT_NUM_WANT,
    AnnounceRequest,
    AnnounceResponse,
    PeerAddress,
    ScrapeResponse,
    Tracker,
    TrackerEvent,
    TrackerState,
    TrackerStatus,
    append_query,
    build_query_string,
)
from app.tracker.errors import (
    TrackerConnectionError,
    TrackerError,
    TrackerProtocolError,
    TrackerTimeoutError,
    UnsupportedTrackerError,
)
from app.tracker.factory import build_tracker
from app.tracker.http_tracker import (
    HttpTracker,
    parse_announce_response,
    parse_compact_peers_ipv4,
    parse_compact_peers_ipv6,
    parse_dictionary_peers,
    parse_peers,
    parse_scrape_response,
)
from app.tracker.manager import AnnounceOutcome, TrackerManager, build_tracker_tiers
from app.tracker.udp_tracker import UdpTracker

__all__ = [
    "DEFAULT_ANNOUNCE_INTERVAL",
    "DEFAULT_NUM_WANT",
    "AnnounceOutcome",
    "AnnounceRequest",
    "AnnounceResponse",
    "HttpTracker",
    "PeerAddress",
    "ScrapeResponse",
    "Tracker",
    "TrackerConnectionError",
    "TrackerError",
    "TrackerEvent",
    "TrackerManager",
    "TrackerProtocolError",
    "TrackerState",
    "TrackerStatus",
    "TrackerTimeoutError",
    "UdpTracker",
    "UnsupportedTrackerError",
    "append_query",
    "build_query_string",
    "build_tracker",
    "build_tracker_tiers",
    "parse_announce_response",
    "parse_compact_peers_ipv4",
    "parse_compact_peers_ipv6",
    "parse_dictionary_peers",
    "parse_peers",
    "parse_scrape_response",
]
