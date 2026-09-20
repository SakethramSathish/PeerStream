"""The torrent view model: everything one screen needs about one torrent.

The command centre is about the session; this is about a single torrent, and it
is deliberately assembled from three smaller view models rather than being one
large one:

* **the torrent itself** (:class:`~app.services.torrent_service.TorrentView`)
  — state, progress, ETA, share ratio. Pulled every pump tick, because those
  are measurements that go stale immediately.
* **the swarm** (:class:`~app.ui.viewmodels.peers_vm.PeersViewModel`) — peers,
  per-peer rates, pulses. Read on its own slower cadence.
* **the pieces** (:class:`~app.ui.viewmodels.pieces_vm.PiecesViewModel`) — the
  matrix, availability, completion history. Same slower cadence.

Both slower views come from coroutines submitted to the engine loop; neither is
read on the Qt thread. A torrent with no data yet reports ``None`` and the
charts say so, rather than drawing an empty grid that looks like a working
download of zero pieces.
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.services.torrent_service import PeerView, PieceMap, TorrentView
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.base import SeriesBuffer
from app.ui.viewmodels.peers_vm import PeersViewModel
from app.ui.viewmodels.pieces_vm import PiecesViewModel

# Rate history for the torrent's own graph. The capacity and the cadence are
# the chart token's own (five minutes at one sample per second), read from the
# design system rather than restated here: a graph that remembered a different
# window than the tokens describe would be a second, quieter bug.
SAMPLE_GAP_SECONDS: float = TOKENS.chart.update_interval_ms / 1000.0


class TorrentViewModel(QObject):
    """One torrent, for the detail screen and its three charts.

    Args:
        parent: Qt parent.
    """

    changed = Signal(object)
    """Emitted after any part of this torrent is updated."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._view: TorrentView | None = None
        self._peers = PeersViewModel(self)
        self._pieces = PiecesViewModel(self)
        self._download = SeriesBuffer(min_gap_seconds=SAMPLE_GAP_SECONDS)
        self._upload = SeriesBuffer(min_gap_seconds=SAMPLE_GAP_SECONDS)
        self._progress = SeriesBuffer(min_gap_seconds=SAMPLE_GAP_SECONDS)
        self._peers.changed.connect(lambda _vm: self.changed.emit(self))
        self._pieces.changed.connect(lambda _vm: self.changed.emit(self))

    # ----------------------------------------------------------------- updates

    def update_torrent(self, view: TorrentView | None, *, now: float | None = None) -> None:
        """Fold in a fresh :class:`TorrentView` (every pump tick).

        Args:
            view: The torrent, or ``None`` when nothing is selected.
            now: The clock reading to stamp the samples with. Tests pass it;
                the window does not, and gets the monotonic clock.
        """
        self._view = view
        if view is None:
            self.changed.emit(self)
            return
        metrics = view.metrics
        now = time.monotonic() if now is None else now
        self._download.append(metrics.download.displayed if metrics else 0.0, now=now)
        self._upload.append(metrics.upload.displayed if metrics else 0.0, now=now)
        self._progress.append(view.progress, now=now)
        self.changed.emit(self)

    def update_peers(self, peers: tuple[PeerView, ...]) -> None:
        """Fold in a fresh read of the swarm (on the slower cadence)."""
        self._peers.update(peers)

    def update_pieces(self, piece_map: PieceMap) -> None:
        """Fold in a fresh read of the pieces (on the slower cadence)."""
        self._pieces.update(piece_map)

    def clear(self) -> None:
        """Forget the torrent entirely: used when the selection changes."""
        self._view = None
        self._peers.clear()
        self._pieces.clear()
        self._download.clear()
        self._upload.clear()
        self._progress.clear()
        self.changed.emit(self)

    # ------------------------------------------------------------------ access

    @property
    def view(self) -> TorrentView | None:
        """The torrent, or ``None`` when nothing is selected."""
        return self._view

    @property
    def info_hash(self) -> str | None:
        return None if self._view is None else self._view.info_hash

    @property
    def selected(self) -> bool:
        return self._view is not None

    @property
    def peers(self) -> PeersViewModel:
        return self._peers

    @property
    def pieces(self) -> PiecesViewModel:
        return self._pieces

    # ------------------------------------------------------------------ series

    @property
    def download_series(self) -> tuple[tuple[float, float], ...]:
        return self._download.samples

    @property
    def upload_series(self) -> tuple[tuple[float, float], ...]:
        return self._upload.samples

    @property
    def progress_series(self) -> tuple[tuple[float, float], ...]:
        return self._progress.samples

    def clear_series(self) -> None:
        """Forget the rate and progress history, keeping the torrent."""
        self._download.clear()
        self._upload.clear()
        self._progress.clear()

    # ------------------------------------------------------------------ totals

    @property
    def download_rate(self) -> float:
        metrics = self._view.metrics if self._view else None
        return metrics.download.displayed if metrics else 0.0

    @property
    def upload_rate(self) -> float:
        metrics = self._view.metrics if self._view else None
        return metrics.upload.displayed if metrics else 0.0

    @property
    def eta_seconds(self) -> float | None:
        metrics = self._view.metrics if self._view else None
        return metrics.eta_seconds if metrics else None

    @property
    def progress(self) -> float:
        return 0.0 if self._view is None else self._view.progress

    def as_dict(self) -> dict[str, Any]:
        """Everything one screen shows, for tests and for the CLI."""
        return {
            "info_hash": self.info_hash,
            "name": self._view.name if self._view else None,
            "state": self._view.state.value if self._view else None,
            "progress": round(self.progress, 5),
            "download_rate": round(self.download_rate, 3),
            "upload_rate": round(self.upload_rate, 3),
            "peers": self._peers.as_dict(),
            "pieces": self._pieces.as_dict(),
            "samples": len(self._download),
        }
