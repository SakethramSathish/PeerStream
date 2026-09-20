"""Fixtures for the storage layer.

The payload comes from the root ``conftest.py`` (a deterministic 512 KiB
buffer), so every test hashes the same bytes the torrent's piece hashes were
computed over: writing "valid" data means writing the real payload slice, not
a stand-in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from app.storage import StorageManager
from app.torrent import Torrent, parse_torrent

# 100 KiB pieces over four 128 KiB files: every piece except the first and the
# last straddles a file boundary, which is the case TRD §29 calls out.
CROSSING_PIECE_LENGTH: int = 100 * 1024

PieceData = Callable[[Torrent, int], bytes]


@pytest.fixture
def piece_of(payload: bytes) -> PieceData:
    """Return the real payload bytes for a piece of a torrent."""

    def _piece(torrent: Torrent, index: int) -> bytes:
        start = torrent.piece_offset(index)
        return payload[start : start + torrent.piece_size(index)]

    return _piece


@pytest.fixture
def crossing_torrent(build_torrent: Callable[..., bytes]) -> Torrent:
    """A multi-file torrent whose pieces cross file boundaries."""
    return parse_torrent(
        build_torrent(name="bundle", piece_length=CROSSING_PIECE_LENGTH, file_count=4)
    )


@pytest.fixture
def make_storage():
    """Factory: a StorageManager rooted in a temporary download directory.

    Everything the factory builds is closed when the test ends, so a test that
    does not tidy up cannot leave a hash-worker thread pool behind.
    """
    opened: list[StorageManager] = []

    def _make(
        torrent: Torrent,
        directory: Path,
        **kwargs: object,
    ) -> StorageManager:
        storage = StorageManager(torrent, directory, **kwargs)  # type: ignore[arg-type]
        opened.append(storage)
        return storage

    yield _make

    for storage in opened:
        asyncio.run(storage.aclose())
