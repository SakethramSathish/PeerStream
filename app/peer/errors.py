"""Peer protocol exceptions.

The split here matters for recovery, not for style. In M5 the connection
manager has to react very differently depending on what failed:

* :class:`PeerDisconnected` is **normal**. Peers hang up constantly — when they
  finish, when they hit a limit, when a NAT drops the mapping. It is logged and
  forgotten, and the peer stays eligible for reconnection.
* :class:`PeerTimeoutError` is **transient**. A peer that stops answering may
  be slow rather than broken; it is retried, with its failure count recorded.
* :class:`ProtocolError` (and its subclass :class:`MessageError`) is
  **behavioural**. The peer spoke to us and what it said was wrong or hostile.
  The connection is dropped and the peer's reputation is marked down, because
  retrying a peer that sends malformed frames is how you get exploited.
* :class:`PeerConnectionError` is **environmental**: the TCP connect itself
  failed (refused, unreachable, DNS). Nothing about the peer protocol was
  learned, so it is the cheapest failure to retry.
* :class:`HandshakeError` covers the handshake specifically, since it has its
  own failure modes — wrong torrent, wrong protocol — that are terminal.
"""

from __future__ import annotations


class PeerError(Exception):
    """Base class for every peer-protocol failure."""


class PeerConnectionError(PeerError):
    """The TCP connection could not be established."""


class PeerTimeoutError(PeerError):
    """The peer did not send expected data within the deadline."""


class PeerDisconnected(PeerError):
    """The peer closed the connection, or the stream ended mid-message.

    This is a normal end-of-connection signal, not a fault: it is how every
    finished transfer ends.
    """


class HandshakeError(PeerError):
    """The handshake failed: bad length, unknown protocol, or wrong info hash."""


class ProtocolError(PeerError):
    """A peer violated the wire protocol (framing, sizes, ordering)."""


class MessageError(ProtocolError):
    """A single decoded message was malformed, unknown, or out of range."""


class MetadataError(PeerError):
    """Metadata exchange failed (BEP 9).

    Raised for every way a peer can fail to supply an info dictionary: it does
    not speak the extension protocol, it has no metadata, it refuses, it goes
    quiet, or — the case worth naming — it sends bytes that do not hash to the
    info hash we asked for. None of these are fatal to the swarm; they are
    reasons to ask someone else.
    """
