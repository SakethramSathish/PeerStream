"""Fixtures for the download engine.

The scheduler and manager are tested against stand-in peers that expose the
same three attributes the real :class:`PeerConnection` does — bitfield, choked,
connected — plus an ``address`` for a stable identity. No sockets are involved
until the integration test, which uses the real :class:`MockPeer` seeder.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from app.core.config import StorageConfig
from app.peer.bitfield import Bitfield
from app.peer.messages import Interested, NotInterested
from app.storage.manager import StorageManager
from app.torrent import Torrent


@dataclass(slots=True)
class FakeAddress:
    """Enough of a peer address to give a peer a stable identity."""

    host: str
    port: int


@dataclass(slots=True)
class FakeSession:
    """The bit of PeerSession the download manager looks at."""

    am_interested: bool = False


@dataclass(slots=True)
class FakePeer:
    """A peer stand-in for scheduling tests.

    It exposes what the real :class:`~app.peer.connection.PeerConnection`
    does — bitfield, choke state, session — so the manager's interest logic
    runs exactly as it does against a socket.
    """

    address: FakeAddress
    bitfield: Bitfield
    choked: bool = False
    connected: bool = True
    sent: list[object] = field(default_factory=list)
    session: FakeSession = field(default_factory=FakeSession)
    closed: bool = False

    @property
    def key(self) -> str:
        return f"{self.address.host}:{self.address.port}"

    async def send(self, message: object) -> None:
        self.sent.append(message)

    async def send_interested(self, interested: bool = True) -> None:
        self.session.am_interested = interested
        self.sent.append(Interested() if interested else NotInterested())


def make_peer(name: str, *, pieces: int, holding: list[int] | None = None) -> FakePeer:
    """A connected, unchoked peer holding ``holding`` (or everything)."""
    bitfield = Bitfield(pieces)
    for index in holding if holding is not None else range(pieces):
        bitfield.set(index)
    host, _, port = name.partition(":")
    return FakePeer(address=FakeAddress(host=host, port=int(port or 6881)), bitfield=bitfield)


@pytest.fixture
def make_storage(tmp_path: Path) -> Iterator[Callable[..., StorageManager]]:
    """Factory: storage for a torrent in a fresh temporary directory.

    Everything the factory builds is closed when the test ends, so a test that
    does not tidy up cannot leave a hash-worker thread pool behind.
    """
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
