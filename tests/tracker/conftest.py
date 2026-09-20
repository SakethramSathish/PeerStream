"""Shared fixtures for tracker tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from app.core.peer_id import generate_peer_id
from app.torrent import Torrent, parse_torrent


@pytest.fixture
def peer_id() -> bytes:
    """A fixed peer id, so announces are reproducible in assertions."""
    return generate_peer_id(rng=_seeded())


def _seeded() -> object:
    import random

    return random.Random(4242)


@pytest.fixture
def torrent_factory(build_torrent: Callable[..., bytes]) -> Callable[..., Torrent]:
    """Build a parsed torrent with the given torrent-builder overrides."""

    def factory(**kwargs: object) -> Torrent:
        return parse_torrent(build_torrent(**kwargs))  # type: ignore[arg-type]

    return factory
