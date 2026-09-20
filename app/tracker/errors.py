"""Exceptions raised by tracker clients.

The split matters for recovery (TRD §43): a :class:`TrackerTimeoutError` or
:class:`TrackerConnectionError` is transient — retry with backoff and the
torrent keeps running on the peers it already has. A
:class:`TrackerProtocolError` means the tracker answered but nonsensically;
retrying immediately is pointless, though a later tier may still work.
:class:`UnsupportedTrackerError` is permanent: the URL names a protocol this
client cannot speak yet.
"""

from __future__ import annotations


class TrackerError(Exception):
    """Base class for tracker failures."""


class UnsupportedTrackerError(TrackerError):
    """The tracker URL uses a scheme this client cannot speak (e.g. ``wss://``)."""


class TrackerConnectionError(TrackerError):
    """The tracker could not be reached, or answered with a non-success status."""


class TrackerTimeoutError(TrackerConnectionError):
    """The tracker did not answer within the configured timeout."""


class TrackerProtocolError(TrackerError):
    """The tracker answered, but the response was malformed or a failure."""
