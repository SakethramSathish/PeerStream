"""The piece view model: what the matrix draws, and nothing it cannot count.

The engine knows seven piece states; the matrix shows five (PRD §10.5). The
collapse is done here, once, in the open: "downloaded" and "verifying" are both
still in flight to a human eye, and a cell that changed colour twice in a
millisecond would be noise, not information.

Two things this view model refuses to do:

* **It does not invent availability.** Rarest-first depends on how many
  connected peers hold each piece, and that number is counted by the engine.
  Where no peer holds a piece, the matrix says zero — which is exactly the
  piece that will stall the download, so it is the one worth seeing.
* **It does not smooth progress.** The completion series is a record of what
  was counted, at the moments it was counted.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal

from app.services.torrent_service import (
    PIECE_STATE_NAMES,
    PieceMap,
    PieceMapState,
)
from app.ui.theme.tokens import TOKENS
from app.ui.viewmodels.base import SeriesBuffer

# How many completion samples to remember, and how far apart: the chart token's
# own window, so the matrix's history and the rate graph's cover the same
# stretch of time.
SAMPLE_GAP_SECONDS: float = TOKENS.chart.update_interval_ms / 1000.0


class PiecesViewModel(QObject):
    """One torrent's pieces, in the shapes the matrix draws.

    Args:
        parent: Qt parent.
    """

    changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._map: PieceMap | None = None
        self._history = SeriesBuffer(min_gap_seconds=SAMPLE_GAP_SECONDS)

    # ----------------------------------------------------------------- updates

    def update(self, piece_map: PieceMap) -> None:
        """Fold in a fresh read of the pieces."""
        self._map = piece_map
        self._history.append(float(piece_map.verified))
        self.changed.emit(self)

    def clear(self) -> None:
        """Forget the torrent's pieces."""
        self._map = None
        self._history.clear()
        self.changed.emit(self)

    # ------------------------------------------------------------------ access

    @property
    def piece_map(self) -> PieceMap | None:
        """The last read, or ``None`` before the first one."""
        return self._map

    @property
    def known(self) -> bool:
        return self._map is not None

    @property
    def piece_count(self) -> int:
        return 0 if self._map is None else self._map.piece_count

    @property
    def counts(self) -> dict[str, int]:
        """How many pieces are in each state, by name."""
        if self._map is None:
            return dict.fromkeys(PIECE_STATE_NAMES, 0)
        return dict(self._map.counts)

    @property
    def verified(self) -> int:
        return self._map.verified if self._map else 0

    @property
    def failed(self) -> int:
        return self.counts.get("failed", 0)

    @property
    def in_flight(self) -> int:
        """Pieces requested or downloading right now."""
        counts = self.counts
        return counts.get("requested", 0) + counts.get("downloading", 0)

    @property
    def missing(self) -> int:
        return self.counts.get("missing", 0)

    @property
    def progress(self) -> float:
        if self._map is None or self._map.piece_count <= 0:
            return 0.0
        return self._map.verified / self._map.piece_count

    @property
    def history(self) -> tuple[tuple[float, float], ...]:
        """Verified-piece count over time, oldest first, bounded."""
        return self._history.samples

    @property
    def gained(self) -> int:
        """How many pieces were verified over the recorded history."""
        samples = self._history.samples
        if len(samples) < 2:
            return 0
        return int(samples[-1][1] - samples[0][1])

    # ----------------------------------------------------------------- queries

    def state_name(self, index: int) -> str:
        """The state of one piece, as a word. Empty if unknown."""
        if self._map is None or not 0 <= index < self._map.piece_count:
            return ""
        return self._map.name_of(index)

    def availability(self, index: int) -> int:
        """How many connected peers hold this piece."""
        if self._map is None or not 0 <= index < self._map.piece_count:
            return 0
        return self._map.availability_of(index)

    def filled(self, index: int) -> float:
        """How much of this piece has arrived, ``0.0``-``1.0``."""
        if self._map is None or not 0 <= index < self._map.piece_count:
            return 0.0
        return self._map.filled_of(index)

    def orphaned(self) -> tuple[int, ...]:
        """Pieces we still need that no connected peer holds.

        A piece with zero availability cannot be downloaded, and a torrent
        waiting on one is stalled for a reason the progress bar cannot show.
        Bounded: at most 64 are reported, because a list of ten thousand is not
        an answer.
        """
        piece_map = self._map
        if piece_map is None:
            return ()
        found: list[int] = []
        for index in range(piece_map.piece_count):
            if piece_map.availability[index] == 0 and piece_map.states[index] != (
                PieceMapState.VERIFIED
            ):
                found.append(index)
                if len(found) >= 64:
                    break
        return tuple(found)

    def as_dict(self) -> dict[str, Any]:
        """The pieces, for tests and for the CLI."""
        return {
            "piece_count": self.piece_count,
            "counts": self.counts,
            "progress": round(self.progress, 5),
            "orphaned": len(self.orphaned()),
            "samples": len(self._history),
        }
