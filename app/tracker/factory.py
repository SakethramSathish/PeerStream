"""Choosing a tracker client from a URL.

A torrent's announce list is a list of *URLs*, and each one names a protocol as
much as an address: ``http://`` means BEP 3 over TCP, ``udp://`` means BEP 15
over datagrams, and anything else means this client cannot talk to it yet. Every
place that turns a URL into a client — the tracker manager, the CLI, the
screenshot and probe tools — should ask here rather than hard-coding a class,
because a second dispatch table is a second place to forget UDP exists.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import aiohttp

from app.core.config import TrackerConfig
from app.core.event_bus import EventBus
from app.tracker.errors import UnsupportedTrackerError
from app.tracker.http_tracker import HttpTracker
from app.tracker.udp_tracker import UdpTracker

logger = logging.getLogger(__name__)


def build_tracker(
    url: str,
    *,
    config: TrackerConfig | None = None,
    session: aiohttp.ClientSession | None = None,
    event_bus: EventBus | None = None,
    timeout: float | None = None,
) -> HttpTracker | UdpTracker:
    """Build the client that can speak to this URL.

    Args:
        url: Announce URL.
        config: Tracker settings; its ``http_timeout`` and ``udp_timeout`` are
            used when ``timeout`` is not given.
        session: Optional shared HTTP session, used for HTTP(S) only.
        event_bus: Optional bus for tracker events.
        timeout: Explicit per-request timeout, overriding the config.

    Returns:
        An :class:`~app.tracker.udp_tracker.UdpTracker` for ``udp://`` and an
        :class:`~app.tracker.http_tracker.HttpTracker` for ``http://`` and
        ``https://``.

    Raises:
        UnsupportedTrackerError: Any other scheme, or a URL with no scheme.
    """
    settings = config or TrackerConfig()
    scheme = urlsplit(url).scheme.lower()
    if scheme == "udp":
        return UdpTracker(
            url,
            timeout=timeout if timeout is not None else settings.udp_timeout,
            event_bus=event_bus,
        )
    if scheme in ("http", "https"):
        return HttpTracker(
            url,
            session=session,
            timeout=timeout if timeout is not None else settings.http_timeout,
            event_bus=event_bus,
        )
    raise UnsupportedTrackerError(f"this client cannot speak {scheme or 'a scheme-less URL'}://")
