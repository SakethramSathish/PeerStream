"""The swarm view model: every peer, and how fast each one is moving.

The engine counts bytes per peer; it does not measure rates per peer, because a
rate needs two reads and the engine has no reason to take them. So this view
model takes them: it keeps the previous per-peer counters and the time between
reads, and reports the difference. That is a measurement of the last interval,
not an estimate — and where there is no interval yet (a peer that just
connected), it reports ``None`` rather than zero, because "we have not measured
it yet" and "it is sending nothing" are different claims.

It also keeps a :class:`~app.ui.viewmodels.base.Throttle`-friendly pulse per
peer, so the swarm canvas can glow when bytes actually move and go quiet when
they don't — animation bound to real state, which is the rule.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.services.torrent_service import PeerView

# A rate is only reported once we have two reads at least this far apart.
# Below it, the difference is mostly timer noise.
MIN_SAMPLE_SECONDS: float = 0.15

# How long after activity a peer keeps glowing, in seconds.
PULSE_HALF_LIFE: float = 0.35


@dataclass(frozen=True, slots=True)
class PeerActivity:
    """What one peer did between two reads.

    Attributes:
        down_rate: Bytes per second from this peer, or ``None`` if unmeasured.
        up_rate: Bytes per second to this peer, or ``None`` if unmeasured.
        pulse: ``0.0``-``1.0``, how recently it moved bytes.
    """

    down_rate: float | None = None
    up_rate: float | None = None
    pulse: float = 0.0

    @property
    def measured(self) -> bool:
        """Whether either rate has been measured yet."""
        return self.down_rate is not None or self.up_rate is not None

    @property
    def rate(self) -> float:
        """The larger of the two rates, for sizing an edge. ``0.0`` if unknown."""
        values = [value for value in (self.down_rate, self.up_rate) if value is not None]
        return max(values, default=0.0)


class PeersViewModel(QObject):
    """The swarm behind one torrent.

    Args:
        parent: Qt parent.
    """

    changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._peers: tuple[PeerView, ...] = ()
        self._activity: dict[str, PeerActivity] = {}
        self._pulses: dict[str, tuple[float, float]] = {}  # key -> (level, stamp)
        self._counters: dict[str, tuple[int, int, float]] = {}
        self._read_at: float | None = None

    # ----------------------------------------------------------------- updates

    def update(self, peers: tuple[PeerView, ...], *, now: float | None = None) -> None:
        """Fold in a fresh read of the swarm."""
        stamp = time.monotonic() if now is None else now
        previous = self._counters
        previous_at = self._read_at

        activity: dict[str, PeerActivity] = {}
        counters: dict[str, tuple[int, int, float]] = {}
        for peer in peers:
            counters[peer.key] = (peer.downloaded, peer.uploaded, stamp)
            activity[peer.key] = _activity_for(peer, previous.get(peer.key), previous_at, stamp)

        self._peers = peers
        self._activity = activity
        self._counters = counters
        self._read_at = stamp
        self._pulses = _advance(self._pulses, activity, stamp)
        self.changed.emit(self)

    def clear(self) -> None:
        """Forget the swarm and every rate derived from it."""
        self._peers = ()
        self._activity.clear()
        self._counters.clear()
        self._pulses.clear()
        self._read_at = None
        self.changed.emit(self)

    # ------------------------------------------------------------------ access

    @property
    def peers(self) -> tuple[PeerView, ...]:
        """Every peer known, connected ones first."""
        return self._peers

    @property
    def connected(self) -> tuple[PeerView, ...]:
        """Peers we are actually talking to."""
        return tuple(peer for peer in self._peers if peer.state == "connected")

    @property
    def candidates(self) -> tuple[PeerView, ...]:
        """Peers we know about but are not connected to."""
        return tuple(peer for peer in self._peers if peer.state != "connected")

    def activity_for(self, key: str) -> PeerActivity:
        """What one peer is doing. Unknown peers get an empty activity."""
        return self._activity.get(key, PeerActivity())

    def pulse_for(self, key: str, *, now: float | None = None) -> float:
        """How brightly this peer's node should glow, ``0.0``-``1.0``.

        The pulse is driven by bytes that actually moved, then decays: a node
        that is glowing is a node that just did something, and a canvas that
        animated on a timer alone would be decoration.
        """
        level, stamp = self._pulses.get(key, (0.0, 0.0))
        if level <= 0.0:
            return 0.0
        return level * _decay((time.monotonic() if now is None else now) - stamp)

    # ------------------------------------------------------------------ totals

    @property
    def connected_count(self) -> int:
        return len(self.connected)

    @property
    def seeds(self) -> int:
        """Connected peers that have the whole torrent."""
        return sum(1 for peer in self.connected if peer.complete)

    @property
    def unchoked(self) -> int:
        """Connected peers willing to serve us, which is the number that matters."""
        return sum(1 for peer in self.connected if not peer.choking_us)

    @property
    def interested_in_us(self) -> int:
        return sum(1 for peer in self.connected if peer.interested_in_us)

    @property
    def download_rate(self) -> float:
        """Bytes per second across every measured peer."""
        return sum(activity.down_rate or 0.0 for activity in self._activity.values())

    @property
    def upload_rate(self) -> float:
        return sum(activity.up_rate or 0.0 for activity in self._activity.values())

    @property
    def busiest(self) -> tuple[tuple[PeerView, PeerActivity], ...]:
        """Peers by how fast they are serving us, fastest first."""
        ranked = [(peer, self.activity_for(peer.key)) for peer in self.connected]
        ranked.sort(key=lambda item: item[1].rate, reverse=True)
        return tuple(ranked)

    def as_dict(self) -> dict[str, Any]:
        """The swarm, for tests and for the CLI."""
        return {
            "peers": len(self._peers),
            "connected": self.connected_count,
            "candidates": len(self.candidates),
            "seeds": self.seeds,
            "unchoked": self.unchoked,
            "interested_in_us": self.interested_in_us,
            "download_rate": round(self.download_rate, 3),
            "upload_rate": round(self.upload_rate, 3),
        }


def _activity_for(
    peer: PeerView,
    previous: tuple[int, int, float] | None,
    previous_at: float | None,
    now: float,
) -> PeerActivity:
    """Difference one peer's counters against the previous read."""
    if previous is None or previous_at is None:
        return PeerActivity()
    elapsed = now - previous_at
    if elapsed < MIN_SAMPLE_SECONDS:
        # Not enough time to say anything about a rate. Reporting 0 here would
        # be a claim that the peer went quiet, which is not what we observed.
        return PeerActivity()
    downloaded = max(0, peer.downloaded - previous[0])
    uploaded = max(0, peer.uploaded - previous[1])
    # A peer that disappears and comes back under the same key restarts its
    # counters; a negative delta is that, not an error worth crashing on.
    return PeerActivity(down_rate=downloaded / elapsed, up_rate=uploaded / elapsed)


def _advance(
    pulses: dict[str, tuple[float, float]],
    activity: dict[str, PeerActivity],
    now: float,
) -> dict[str, tuple[float, float]]:
    """Stimulate each peer's pulse by what it just did, then decay it.

    Only peers that moved bytes get a pulse, so the canvas is never animated by
    a timer alone: it moves because something happened.
    """
    updated: dict[str, tuple[float, float]] = {}
    for key, item in activity.items():
        level, stamp = pulses.get(key, (0.0, now))
        decayed = level * _decay(now - stamp)
        moved = (item.rate or 0.0) > 0.0
        updated[key] = (1.0 if moved else decayed, now)
    return updated


def _decay(seconds: float) -> float:
    """Exponential decay with a stated half-life."""
    if seconds <= 0.0:
        return 1.0
    value: float = 0.5 ** (seconds / PULSE_HALF_LIFE)
    return value
