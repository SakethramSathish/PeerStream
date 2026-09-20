"""Fixtures for the upload engine.

The peers here are stand-ins that expose exactly what
:class:`~app.peer.connection.PeerConnection` does as far as uploading is
concerned — an address, a session with interest and byte counters, and the
three calls the upload manager makes (`set_choking`, `send_piece`,
`send_have`). Everything they "send" is recorded, so a test can assert on
messages without a socket.

Storage is real, because "we served the right bytes" is only true if they
came off disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from app.core.config import StorageConfig, UploadConfig
from app.core.event_bus import EventBus
from app.peer.bitfield import Bitfield
from app.peer.messages import Request
from app.storage.manager import StorageManager
from app.torrent import Torrent
from app.upload.manager import UploadManager

BLOCK = 16 * 1024


@dataclass(slots=True)
class FakeAddress:
    """Enough of a peer address to give a peer a stable identity."""

    host: str
    port: int


@dataclass(slots=True)
class FakeSession:
    """The part of :class:`PeerSession` the upload side reads and writes."""

    piece_count: int = 1
    am_choking: bool = True
    peer_choking: bool = True
    peer_interested: bool = False
    uploaded: int = 0
    downloaded: int = 0

    def note_upload(self, length: int) -> None:
        self.uploaded += length


@dataclass(slots=True)
class FakePeer:
    """A peer stand-in for upload tests."""

    address: FakeAddress
    session: FakeSession = field(default_factory=FakeSession)
    connected: bool = True
    sent: list[object] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.address.host}:{self.address.port}"

    @property
    def am_choking(self) -> bool:
        return self.session.am_choking

    async def set_choking(self, choking: bool) -> None:
        if not self.connected:
            raise ConnectionError("peer is gone")
        self.session.am_choking = choking
        self.sent.append(("choke" if choking else "unchoke",))

    async def send_piece(self, index: int, begin: int, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("peer is gone")
        self.sent.append(("piece", index, begin, data))

    async def send_have(self, index: int) -> None:
        if not self.connected:
            raise ConnectionError("peer is gone")
        self.sent.append(("have", index))

    @property
    def blocks(self) -> list[tuple[int, int, bytes]]:
        """Every ``(index, begin, data)`` this peer was sent."""
        return [(item[1], item[2], item[3]) for item in self.sent if item[0] == "piece"]


@dataclass(slots=True)
class FakeSwarm:
    """Stand-in for the peer manager: just the connections it exposes."""

    connections: list[object] = field(default_factory=list)


def make_peer(name: str, *, pieces: int = 1, interested: bool = True) -> FakePeer:
    """A connected peer that wants what we have (or does not)."""
    host, _, port = name.partition(":")
    session = FakeSession(piece_count=pieces, peer_interested=interested, peer_choking=False)
    return FakePeer(address=FakeAddress(host=host, port=int(port or 6881)), session=session)


@pytest.fixture
def make_storage(tmp_path: Path) -> Iterator[Callable[..., StorageManager]]:
    """Factory: real storage in a temporary directory, closed after the test."""
    opened: list[StorageManager] = []

    def _make(torrent: Torrent, *, name: str = "dl", **config: object) -> StorageManager:
        directory = tmp_path / name
        settings = StorageConfig(
            download_directory=directory,
            state_directory=tmp_path / "state",
            **config,  # type: ignore[arg-type]
        )
        storage = StorageManager(torrent, directory, config=settings)
        opened.append(storage)
        return storage

    yield _make

    for storage in opened:
        asyncio.run(storage.aclose())


@pytest.fixture
def make_upload():
    """Factory: an UploadManager over the given storage and peers."""

    def _make(
        torrent: Torrent,
        *,
        storage: StorageManager,
        peers: FakeSwarm | None = None,
        have: Bitfield | None = None,
        config: UploadConfig | None = None,
        bus: EventBus | None = None,
    ) -> UploadManager:
        return UploadManager(
            torrent,
            storage=storage,
            peers=peers if peers is not None else FakeSwarm(),
            config=config or UploadConfig(choke_interval=0.2),
            event_bus=bus,
            have=have,
        )

    return _make


def raw_request(index: int, begin: int, length: int) -> Request:
    """A request the wire layer would never have built.

    ``Request`` validates itself on construction, which is why a message like
    this cannot arrive from a real socket. The upload manager checks anyway —
    defence in depth, because "the request came from a stranger" is not a
    reason to trust the layer below it — and this is how that second check is
    tested.
    """
    request = Request.__new__(Request)
    object.__setattr__(request, "index", index)
    object.__setattr__(request, "begin", begin)
    object.__setattr__(request, "length", length)
    return request
