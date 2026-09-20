"""Application services: the only API surface the UI and the CLI talk to.

Layers above this one — Qt widgets, CLI commands — may read state and ask for
actions, but they never open a socket, touch a piece, or await a protocol
message. That boundary is the whole point of the package.

    Session ──▶ TorrentService ──▶ Engine ──▶ {peers, storage, trackers}
         │                                        │
         └──────────▶ EventBus ◀──────────────────┘
                          │
                          ▼
                       AppState ──▶ UI
"""

from app.services.app_state import DEFAULT_EVENT_CAPACITY, AppSnapshot, AppState
from app.services.engine import (
    DEFAULT_WAIT_TIMEOUT,
    Engine,
    ResumeSummary,
    TorrentState,
    build_engine,
    build_have,
)
from app.services.session import Session, SessionTotals
from app.services.torrent_service import TorrentService, TorrentView

__all__ = [
    "DEFAULT_EVENT_CAPACITY",
    "DEFAULT_WAIT_TIMEOUT",
    "AppSnapshot",
    "AppState",
    "Engine",
    "ResumeSummary",
    "Session",
    "SessionTotals",
    "TorrentService",
    "TorrentState",
    "TorrentView",
    "build_engine",
    "build_have",
]
