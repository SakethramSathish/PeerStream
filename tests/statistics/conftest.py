"""Fakes for the statistics tests.

The collector is built to read whatever the engines expose, so these stand-ins
only have to reproduce the shapes it looks at: counters, progress, pieces and
connections. Every number a test asserts on is therefore one the collector
computed, not one a fake handed back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.peer.bitfield import Bitfield


@dataclass(slots=True)
class FakeDownloadStats:
    """The counters :class:`DownloadManager` keeps."""

    blocks_received: int = 0
    blocks_duplicate: int = 0
    blocks_unsolicited: int = 0
    wasted_bytes: int = 0
    pieces_verified: int = 0
    pieces_failed: int = 0


@dataclass(slots=True)
class FakeUploadStats:
    """The counters :class:`UploadManager` keeps."""

    blocks_served: int = 0
    bytes_uploaded: int = 0
    queue_depth: int = 0
    requests_rejected: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class FakePeerStats:
    """The counters :class:`PeerManager` keeps."""

    discovered: int = 0
    candidates: int = 0
    connected: int = 0


@dataclass(slots=True)
class FakeSession:
    """Just the two flags the collector reads off a peer session."""

    peer_choking: bool = True
    peer_interested: bool = False


@dataclass(slots=True)
class FakeConnection:
    """One peer connection, as the collector sees it."""

    name: str = "peer"
    connected: bool = True
    choked: bool = True
    session: FakeSession = field(default_factory=FakeSession)


@dataclass(slots=True)
class FakeDownload:
    """A download manager reduced to what statistics reads."""

    stats: FakeDownloadStats = field(default_factory=FakeDownloadStats)
    verified: list[int] = field(default_factory=list)
    complete: bool = False

    @property
    def verified_pieces(self) -> tuple[int, ...]:
        return tuple(self.verified)


@dataclass(slots=True)
class FakeUpload:
    """An upload manager reduced to what statistics reads."""

    stats: FakeUploadStats = field(default_factory=FakeUploadStats)
    pieces: int = 1
    have: Any = None

    def __post_init__(self) -> None:
        if self.have is None:
            self.have = Bitfield(self.pieces)


@dataclass(slots=True)
class FakePeers:
    """A peer manager reduced to what statistics reads."""

    connections: list[Any] = field(default_factory=list)
    candidates: list[Any] = field(default_factory=list)
    stats: FakePeerStats = field(default_factory=FakePeerStats)

    def unchoked_peers(self) -> list[Any]:
        return [peer for peer in self.connections if not getattr(peer, "choked", True)]


def connection(
    name: str = "peer",
    *,
    connected: bool = True,
    choked: bool = False,
    interested: bool = False,
) -> FakeConnection:
    """One connection: named, because the UI shows names, not objects."""
    return FakeConnection(
        name=name,
        connected=connected,
        choked=choked,
        session=FakeSession(peer_choking=choked, peer_interested=interested),
    )
