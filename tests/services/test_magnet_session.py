"""End-to-end: a magnet link becomes bytes on disk.

Nothing here is stubbed except the two peers, and they are not stubs either —
they are the other end of the protocol, over real TCP:

* one peer holds only the **metadata** and serves it over BEP 9;
* another holds only the **data** and serves it over BEP 3.

The client is given nothing but the magnet: 20 bytes of hash and the address of
the first peer. It has to fetch the info dictionary, verify it against the
hash, build a torrent from a stranger's bytes, and then download the payload
from a peer it was told about by the link. The check at the end is the only one
that matters: the file on disk matches the payload, byte for byte.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path

import pytest
from app.bencode import encode
from app.core.config import Config
from app.core.constants import DEFAULT_PIECE_LENGTH
from app.services import Session
from app.torrent.magnet import MagnetUri
from app.tracker.base import PeerAddress

from tests.mocks.mock_peer import MockPeer
from tests.peer.test_metadata_exchange import MetadataPeer

pytestmark = pytest.mark.integration

PIECE_LENGTH = DEFAULT_PIECE_LENGTH
PIECE_COUNT = 4


@dataclass(frozen=True, slots=True)
class Fixture:
    """Everything the swarm is built from, and the hash that names it."""

    payload: bytes
    raw_info: bytes
    info_hash: bytes


def build_fixture() -> Fixture:
    """A payload and the info dictionary that describes it."""
    payload = os.urandom(PIECE_LENGTH * PIECE_COUNT)
    pieces = b"".join(
        sha1(payload[index * PIECE_LENGTH : (index + 1) * PIECE_LENGTH]).digest()
        for index in range(PIECE_COUNT)
    )
    info: dict[bytes, object] = {
        b"name": b"magnet-demo.bin",
        b"piece length": PIECE_LENGTH,
        b"pieces": pieces,
        b"length": len(payload),
    }
    raw = encode(info)
    return Fixture(payload=payload, raw_info=raw, info_hash=sha1(raw).digest())


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[Session]:
    """A session that writes into the test's own temporary directory."""
    async with Session(Config(), download_directory=tmp_path) as started:
        yield started


class TestMagnet:
    async def test_a_magnet_becomes_a_torrent_then_a_file(self, tmp_path, session: Session) -> None:
        fixture = build_fixture()

        # Two peers: one knows the torrent's shape, one knows its contents.
        metadata_peer = MetadataPeer(fixture.raw_info)
        await metadata_peer.start()
        seeder = MockPeer(fixture.payload, info_hash=fixture.info_hash, piece_length=PIECE_LENGTH)
        await seeder.start()
        try:
            magnet = MagnetUri(
                info_hash=fixture.info_hash,
                display_name="magnet demo",
                peers=(("127.0.0.1", metadata_peer.port),),
            )
            engine, resolution = await session.add_magnet(
                magnet,
                peers=[PeerAddress("127.0.0.1", seeder.port, source="manual")],
                listen=False,
                resume=False,
            )

            assert resolution.torrent.info_hash == fixture.info_hash
            assert resolution.torrent.name == "magnet-demo.bin"
            assert resolution.torrent.piece_count == PIECE_COUNT
            assert resolution.metadata.raw == fixture.raw_info
            assert "x.pe" in resolution.sources
            assert session.get(resolution.hex_info_hash) is not None

            # The display name in the link was a claim; the real name came from
            # the metadata, and that is the one we show.
            assert resolution.magnet.display_name == "magnet demo"

            complete = await _wait_for_completion(engine, timeout=30.0)
            assert complete, "the payload never finished downloading"
            assert engine.download.stats.pieces_verified == PIECE_COUNT

            written = _payload_path(tmp_path, "magnet-demo.bin")
            assert written.read_bytes() == fixture.payload
        finally:
            await metadata_peer.stop()
            await seeder.stop()

    async def test_a_magnet_nobody_can_serve_leaves_nothing_behind(
        self, tmp_path, session: Session
    ) -> None:
        from app.peer.errors import MetadataError

        fixture = build_fixture()
        magnet = MagnetUri(info_hash=fixture.info_hash, peers=(("127.0.0.1", 1),))

        with pytest.raises(MetadataError):
            await session.add_magnet(magnet, listen=False, resume=False)

        assert len(session) == 0, "a failed magnet must not leave a half-torrent in the session"


def _payload_path(directory: Path, name: str) -> Path:
    """Where the single-file torrent's payload ended up."""
    direct = Path(directory) / name
    if direct.is_file():
        return direct
    nested = Path(directory) / name / name
    if nested.is_file():
        return nested
    matches = list(Path(directory).rglob(name))
    assert matches, f"{name} was never written under {directory}"
    return matches[0]


async def _wait_for_completion(engine: object, *, timeout: float) -> bool:
    """Poll until the engine reports the download complete."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if engine.complete:  # type: ignore[attr-defined]
            return True
        await asyncio.sleep(0.05)
    return False
