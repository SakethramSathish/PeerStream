"""Qt fixtures for the interface tests.

The platform is forced to ``offscreen`` at import time — before PySide6 is
imported anywhere — so the whole UI can be built and inspected on a machine with
no display. Everything here is loopback or in-memory: no test in this package
needs the internet.

One ``QApplication`` is shared by the whole session. Qt allows exactly one, and
creating it per test is both slow and a good way to leak a widget tree.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

# Must happen before any PySide6 import in any module.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from app.ui.theme import icons
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    """The one QApplication the interface tests share."""
    existing = QApplication.instance()
    app = existing or QApplication(["pytest"])
    yield app  # type: ignore[misc]
    icons.clear_cache()


@pytest.fixture
def icon_cache_is_cold() -> None:
    """Start a test with an empty icon cache, so caching can be measured."""
    icons.clear_cache()


@pytest.fixture
def screenshot_dir(tmp_path: Path) -> Path:
    """A directory for captured PNGs."""
    target = tmp_path / "screenshots"
    target.mkdir(parents=True, exist_ok=True)
    return target
