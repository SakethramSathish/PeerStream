"""In-process mock tracker fixtures.

The mock speaks the real protocol over real HTTP on a loopback socket, so tests
exercise URL encoding, connection handling, timeouts and response parsing —
the parts a stubbed "tracker client" would bypass entirely.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tools.mock_tracker import MockTracker


@asynccontextmanager
async def running_tracker(**options: Any) -> AsyncIterator[MockTracker]:
    """Start a mock tracker on a free port and stop it afterwards.

    Args:
        **options: Passed to :class:`~tools.mock_tracker.MockTracker`.

    Yields:
        The running tracker; use :attr:`~tools.mock_tracker.MockTracker.url`
        or :attr:`~tools.mock_tracker.MockTracker.announce_url`.
    """
    tracker = MockTracker(port=0, **options)
    try:
        await tracker.start()
        yield tracker
    finally:
        await tracker.stop()


@pytest.fixture
async def mock_tracker() -> AsyncIterator[MockTracker]:
    """A running mock tracker for the duration of one test."""
    async with running_tracker() as tracker:
        yield tracker


@pytest.fixture
def tracker_server() -> Any:
    """Factory fixture for tests that need more than one tracker."""
    return running_tracker
