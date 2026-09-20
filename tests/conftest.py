"""Shared pytest fixtures.

Torrent fixtures are generated deterministically at test time
(:mod:`tools.make_test_torrent`) rather than committed as binary files: the
payload and its hashes are reproducible from a seed, so fixtures stay in sync
with the code and the repository stays free of opaque blobs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from app.torrent import Torrent, parse_torrent
from tools.make_test_torrent import (
    build_torrent_bytes,
    default_paths,
    generate_payload,
    split_payload,
)

# Fixture modules must be listed explicitly for pytest to load them.
pytest_plugins = [
    "tests.mocks.mock_tracker",
]

PAYLOAD_SIZE: int = 512 * 1024  # 512 KiB → 32 pieces at 16 KiB
PIECE_LENGTH: int = 16 * 1024
PAYLOAD_SEED: int = 2024

TorrentBuilder = Callable[..., bytes]


@pytest.fixture(scope="session")
def payload() -> bytes:
    """Deterministic 512 KiB payload shared by the session's torrent fixtures."""
    return generate_payload(PAYLOAD_SIZE, seed=PAYLOAD_SEED)


@pytest.fixture(scope="session")
def single_file_torrent_bytes(payload: bytes) -> bytes:
    """A valid single-file torrent for :func:`payload`."""
    return build_torrent_bytes(
        payload,
        name="payload.bin",
        piece_length=PIECE_LENGTH,
        announce="http://127.0.0.1:8000/announce",
        announce_list=[["http://127.0.0.1:8000/announce"], ["udp://127.0.0.1:9000/announce"]],
    )


@pytest.fixture(scope="session")
def multi_file_torrent_bytes(payload: bytes) -> bytes:
    """A valid multi-file torrent (4 files, one in a subdirectory) for :func:`payload`."""
    return build_torrent_bytes(
        payload,
        name="bundle",
        piece_length=PIECE_LENGTH,
        file_count=4,
        announce="http://127.0.0.1:8000/announce",
    )


@pytest.fixture
def sample_torrent(single_file_torrent_bytes: bytes) -> Torrent:
    """Parsed single-file torrent."""
    return parse_torrent(single_file_torrent_bytes)


@pytest.fixture
def multi_file_torrent(multi_file_torrent_bytes: bytes) -> Torrent:
    """Parsed multi-file torrent."""
    return parse_torrent(multi_file_torrent_bytes)


@pytest.fixture
def build_torrent(payload: bytes) -> TorrentBuilder:
    """Factory fixture: build torrent bytes for the shared payload with overrides."""

    def _build(**kwargs: object) -> bytes:
        return build_torrent_bytes(payload, **kwargs)  # type: ignore[arg-type]

    return _build


@pytest.fixture
def payload_directory(tmp_path: Path, payload: bytes) -> Path:
    """A directory containing the payload laid out as a finished download.

    Mirrors what the storage layer will produce: ``<dir>/<name>/<paths...>``
    for multi-file torrents.
    """
    layout = default_paths(4)
    chunks = split_payload(payload, len(layout))
    for parts, chunk in zip(layout, chunks, strict=True):
        target = tmp_path / "bundle" / Path(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(chunk)
    return tmp_path


# --------------------------------------------------------------- network tests


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--network`` for tests that reach the public internet."""
    parser.addoption(
        "--network",
        action="store_true",
        default=False,
        help="run tests that contact public trackers (off by default)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``network``-marked tests unless ``--network`` is passed.

    Offline runs must stay green: the default suite talks only to loopback.
    """
    if config.getoption("--network"):
        return
    skip_network = pytest.mark.skip(reason="needs --network to contact a public tracker")
    for item in items:
        # ``get_closest_marker`` and not ``item.keywords``: a parameter id is a
        # keyword too, so a test parametrised over a thing *called* network
        # would otherwise be skipped for having nothing to do with the network.
        if item.get_closest_marker("network") is not None:
            item.add_marker(skip_network)
