"""The session view model: what the whole client is doing, in shapes a widget can bind.

This is the ViewModel half of MVVM. It takes an :class:`AppSnapshot`, reduces it
to the handful of things the shell actually draws, and remembers the rate history
in bounded buffers. It owns no engine, opens no socket, and never blocks: it is
handed data and it answers questions about it.

Everything it reports is either measured or counted. Where a number cannot be
known — a share ratio before anything has been received, a peer health with no
peers — it reports ``None``, and the widget shows "--" instead of a confident
zero.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.services.app_state import AppSnapshot
from app.services.torrent_service import TorrentView
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.base import Health, SeriesBuffer, Throttle, ratio

# Charts redraw at a fixed cadence no matter how fast the snapshots arrive
# (PRD 11: 500-1000 ms, with a bounded buffer). 20 fps is the ceiling.
_CHART_INTERVAL_MS: int = 1000 // TOKENS.chart.max_fps

# The pump runs every 200 ms; sampling the rate graph every 500 ms gives a
# readable curve rather than a 5-sample hairball.
_SAMPLE_GAP_SECONDS: float = TOKENS.chart.update_interval_ms / 1000.0


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """The numbers the header and the command centre show.

    All of it measured; ``None`` where nothing has been measured yet.
    """

    torrent_count: int = 0
    active_count: int = 0
    seeding_count: int = 0
    download_rate: float = 0.0
    upload_rate: float = 0.0
    downloaded_bytes: int = 0
    uploaded_bytes: int = 0
    peers_connected: int = 0
    peers_unchoked: int = 0
    progress: float = 0.0
    share_ratio: float | None = None
    eta_seconds: float | None = None
    wasted_bytes: int = 0

    @property
    def idle(self) -> bool:
        """Whether the client has anything at all in it."""
        return self.torrent_count == 0


class SessionViewModel(QObject):
    """Reduces snapshots into the shapes the shell draws.

    Args:
        parent: Qt parent.
    """

    changed = Signal(object)
    """Emitted with this view model after every update, so views can refresh."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._summary = SessionSummary()
        self._torrents: tuple[TorrentView, ...] = ()
        self._snapshot: AppSnapshot | None = None
        self._download = SeriesBuffer(min_gap_seconds=_SAMPLE_GAP_SECONDS)
        self._upload = SeriesBuffer(min_gap_seconds=_SAMPLE_GAP_SECONDS)
        self._chart = Throttle(_CHART_INTERVAL_MS)
        self._started = time.monotonic()
        self._revision = -1

    # ------------------------------------------------------------------ updates

    @property
    def snapshot(self) -> AppSnapshot | None:
        """The most recent snapshot, or ``None`` before the first one."""
        return self._snapshot

    @property
    def summary(self) -> SessionSummary:
        """The reduced numbers."""
        return self._summary

    @property
    def torrents(self) -> tuple[TorrentView, ...]:
        """Every torrent, most recently active first."""
        return self._torrents

    @property
    def revision(self) -> int:
        """The snapshot revision last applied."""
        return self._revision

    def update(self, snapshot: AppSnapshot) -> bool:
        """Fold in a new snapshot.

        Returns:
            Whether the view model changed enough to be worth redrawing. The
            charts are throttled, so a snapshot that only refreshes the rate
            buffers inside the frame budget reports ``False``.
        """
        self._snapshot = snapshot
        self._revision = snapshot.revision
        self._summary = _summarise(snapshot)
        self._torrents = _order(snapshot.torrents)

        now = time.monotonic()
        self._download.append(self._summary.download_rate, now=now)
        self._upload.append(self._summary.upload_rate, now=now)
        redraw = self._chart.allows(now=now)
        self.changed.emit(self)
        return redraw

    # ------------------------------------------------------------------- charts

    @property
    def download_series(self) -> tuple[tuple[float, float], ...]:
        """Recent download rates, oldest first, bounded."""
        return self._download.samples

    @property
    def upload_series(self) -> tuple[tuple[float, float], ...]:
        """Recent upload rates, oldest first, bounded."""
        return self._upload.samples

    @property
    def uptime(self) -> float:
        """Seconds since this view model was created."""
        return time.monotonic() - self._started

    def clear_series(self) -> None:
        """Forget the rate history (used when the session is reset)."""
        self._download.clear()
        self._upload.clear()

    # ------------------------------------------------------------------- health

    @property
    def peer_health(self) -> Health:
        """How many connected peers are actually willing to serve us.

        This is the number that matters: a swarm of forty peers that all have
        us choked is not a swarm, it is a waiting room.
        """
        summary = self._summary
        return ratio(
            summary.peers_unchoked,
            summary.peers_connected,
            note=f"{summary.peers_unchoked}/{summary.peers_connected} unchoked",
        )

    @property
    def piece_health(self) -> Health:
        """How much of the wanted data is verified, across every torrent."""
        verified = sum(view.verified_pieces for view in self._torrents)
        total = sum(view.piece_count for view in self._torrents)
        return ratio(verified, total, note=f"{verified}/{total} pieces")

    @property
    def activity_health(self) -> Health:
        """How many of the client's torrents are doing something."""
        summary = self._summary
        return ratio(
            summary.active_count,
            summary.torrent_count,
            note=f"{summary.active_count}/{summary.torrent_count} active",
        )

    # --------------------------------------------------------------- convenience

    def torrent(self, hex_info_hash: str) -> TorrentView | None:
        """The view for one torrent, if the session has it."""
        for view in self._torrents:
            if view.info_hash == hex_info_hash:
                return view
        return None

    def as_dict(self) -> dict[str, Any]:
        """Everything the shell shows, for tests and for the CLI's ``--json``."""
        summary = self._summary
        return {
            "torrents": summary.torrent_count,
            "active": summary.active_count,
            "seeding": summary.seeding_count,
            "download_rate": summary.download_rate,
            "upload_rate": summary.upload_rate,
            "downloaded_bytes": summary.downloaded_bytes,
            "uploaded_bytes": summary.uploaded_bytes,
            "wasted_bytes": summary.wasted_bytes,
            "peers_connected": summary.peers_connected,
            "peers_unchoked": summary.peers_unchoked,
            "progress": summary.progress,
            "share_ratio": summary.share_ratio,
            "eta_seconds": summary.eta_seconds,
            "samples": len(self._download),
        }


def _summarise(snapshot: AppSnapshot) -> SessionSummary:
    """Reduce a snapshot to the numbers the shell draws."""
    totals = snapshot.totals
    peers_connected = 0
    peers_unchoked = 0
    wasted = 0
    verified_pieces = 0
    total_pieces = 0
    seeding = 0
    best_eta: float | None = None

    for view in snapshot.torrents:
        if view.state.value == "seeding":
            seeding += 1
        total_pieces += view.piece_count
        verified_pieces += view.verified_pieces
        metrics = view.metrics
        if metrics is None:
            continue
        peers_connected += metrics.peers_connected
        peers_unchoked += metrics.peers_unchoked
        wasted += metrics.wasted_bytes
        eta = metrics.eta_seconds
        if eta is not None and eta > 0:
            # The slowest torrent is the one that defines "not finished yet",
            # so it is the ETA worth showing.
            best_eta = eta if best_eta is None else max(best_eta, eta)

    downloaded = totals.downloaded_bytes
    share = (totals.uploaded_bytes / downloaded) if downloaded > 0 else None
    progress = (verified_pieces / total_pieces) if total_pieces else 0.0

    return SessionSummary(
        torrent_count=len(snapshot.torrents),
        active_count=len(snapshot.active_torrents),
        seeding_count=seeding,
        download_rate=totals.download_rate,
        upload_rate=totals.upload_rate,
        downloaded_bytes=downloaded,
        uploaded_bytes=totals.uploaded_bytes,
        peers_connected=peers_connected,
        peers_unchoked=peers_unchoked,
        progress=progress,
        share_ratio=share,
        eta_seconds=best_eta,
        wasted_bytes=wasted,
    )


def _order(torrents: tuple[TorrentView, ...]) -> tuple[TorrentView, ...]:
    """Sort the list the way a user scans it: doing things first, then by name.

    A library that reorders itself every time a rate changes is unusable, so
    the ordering key is stable: active-ness, then progress, then name.
    """
    return tuple(
        sorted(
            torrents,
            key=lambda view: (
                not view.active,
                -view.progress,
                view.name.lower(),
            ),
        )
    )
