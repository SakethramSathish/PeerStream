"""Choking policy (PRD FR-13).

Uploading to everybody is not generosity, it is a denial of service against
yourself: a client has one upstream pipe and dozens of peers who would like a
share of it. Deciding who gets a share — and changing your mind as the swarm
behaves — is what this module is for.

The policy is deliberately **pure**. It is handed a snapshot of what every
peer has done and returns who should be unchoked; it holds no sockets, no
tasks and no clock beyond the timestamp it is given, so:

* the same snapshot always produces the same answer, and
* a test can run an hour of swarm behaviour in a millisecond by handing it
  timestamps of its own.

Three ideas, all of them old and all of them necessary:

**Tit-for-tat.** The peers we upload to are the peers that uploaded to us,
ranked by how many bytes they have actually sent. Not promised, not estimated:
counted.

**Optimistic unchoking.** One slot is given away on a rotation, regardless of
merit. Without it a new peer — one that has given us nothing because we have
given it nothing — could never earn a place, and the swarm would ossify around
whoever happened to be first.

**Anti-snubbing.** A peer that chokes us and has sent nothing for
``snub_seconds`` has stopped reciprocating. It keeps whatever it earned only
if there is nobody better, and it is still eligible for the optimistic slot,
because the way to find out whether a peer has changed its mind is to give it
one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.core.config import UploadConfig


@dataclass(frozen=True, slots=True)
class PeerAccounting:
    """What one peer has done, as far as the upload side can count it.

    Every field is measured. Nothing here is a reputation score or a guess:
    the ranking is arithmetic on bytes that really crossed a socket.

    Args:
        key: Stable identity of the peer (``host:port``).
        interested: Whether the peer has told us it wants something.
        choking_us: Whether the peer is refusing to upload to us.
        bytes_up: Bytes we have sent this peer.
        bytes_down: Bytes this peer has sent us.
        last_download_at: When this peer last sent us data, if ever.
        optimistic_rounds: How many times this peer has held the optimistic
            slot; the rotation prefers whoever has had it least.
    """

    key: str
    interested: bool = False
    choking_us: bool = True
    bytes_up: int = 0
    bytes_down: int = 0
    last_download_at: float | None = None
    optimistic_rounds: int = 0

    def snubbed(self, now: float, *, snub_seconds: float) -> bool:
        """Whether this peer has stopped reciprocating.

        A peer that is *not* choking us is never snubbed, whatever the clock
        says: it may simply have nothing we asked for.
        """
        if not self.choking_us:
            return False
        if self.last_download_at is None:
            return True
        return now - self.last_download_at >= snub_seconds


@dataclass(frozen=True, slots=True)
class ChokeDecision:
    """Who may download from us, right now.

    Args:
        unchoked: Peers that earned a regular slot, best first.
        optimistic: The peer holding the give-away slot, if any.
    """

    unchoked: tuple[str, ...] = ()
    optimistic: str | None = None

    @property
    def allowed(self) -> tuple[str, ...]:
        """Every peer that may be served, regular slots and optimistic alike."""
        if self.optimistic is None or self.optimistic in self.unchoked:
            return self.unchoked
        return (*self.unchoked, self.optimistic)

    def is_unchoked(self, key: str) -> bool:
        """Whether ``key`` may download from us."""
        return key in self.allowed


@dataclass
class ChokePolicy:
    """Decides which peers we upload to.

    Args:
        config: Slot count, rotation interval and snub threshold.

    Example:
        >>> policy = ChokePolicy()                              # doctest: +SKIP
        >>> decision = policy.evaluate({"a:1": accounting}, now=0.0)
        >>> decision.allowed                                    # doctest: +SKIP
        ('a:1',)
    """

    config: UploadConfig = field(default_factory=UploadConfig)

    def __post_init__(self) -> None:
        self._optimistic: str | None = None
        self._optimistic_since: float | None = None
        self._rounds: dict[str, int] = {}

    # ------------------------------------------------------------------ state

    @property
    def config_slots(self) -> int:
        """How many peers earn a regular slot."""
        return self.config.slots

    @property
    def optimistic_peer(self) -> str | None:
        """Who currently holds the give-away slot."""
        return self._optimistic

    def forget(self, key: str) -> None:
        """Stop tracking a peer that has gone away."""
        self._rounds.pop(key, None)
        if self._optimistic == key:
            self._optimistic = None
            self._optimistic_since = None

    # --------------------------------------------------------------- decision

    def evaluate(self, peers: Mapping[str, PeerAccounting], *, now: float) -> ChokeDecision:
        """Choose who we upload to.

        Args:
            peers: Every connected peer and what it has done.
            now: Current time, on the same clock as the accountings' timestamps.

        Returns:
            The peers allowed to download: up to ``config.slots`` of them by
            merit, plus the current optimistic pick if one is due.
        """
        alive = set(peers)
        for key in list(self._rounds):
            if key not in alive:
                self._rounds.pop(key, None)

        wanted = [peer for peer in peers.values() if peer.interested]
        regular = self._regular_slots(wanted, now=now)
        optimistic = self._optimistic_slot(wanted, regular, now=now)
        return ChokeDecision(unchoked=regular, optimistic=optimistic)

    def _regular_slots(self, wanted: list[PeerAccounting], *, now: float) -> tuple[str, ...]:
        """The peers that earned a slot: whoever gave us the most, first."""
        if self.config.slots <= 0 or not wanted:
            return ()
        ranked = sorted(wanted, key=lambda peer: self._rank(peer, now=now))
        # Snubbing is a preference, not a ban: with capacity to spare, giving
        # bytes to a peer that has gone quiet beats giving them to nobody.
        return tuple(peer.key for peer in ranked[: self.config.slots])

    def _rank(self, peer: PeerAccounting, *, now: float) -> tuple[int, int, int, str]:
        """Sort key for regular slots: best peers first."""
        return (
            0 if not peer.snubbed(now, snub_seconds=self.config.snub_seconds) else 1,
            -peer.bytes_down,  # gave us more → ranked higher
            peer.bytes_up,  # got less from us → ranked higher (fairness)
            peer.key,  # deterministic: equal peers must not shuffle
        )

    def _optimistic_slot(
        self,
        wanted: list[PeerAccounting],
        regular: tuple[str, ...],
        *,
        now: float,
    ) -> str | None:
        """Pick (and rotate) the one slot that is given away regardless of merit."""
        if not self.config.optimistic_unchoke or self.config.slots <= 0:
            # "Upload to nobody" has to mean nobody: an optimistic slot that
            # survives zero regular slots would be a quiet exception to a
            # setting the user made on purpose.
            self._optimistic = None
            self._optimistic_since = None
            return None

        candidates = [peer for peer in wanted if peer.key not in regular]
        if not candidates:
            # Everyone interested already has a slot; there is nobody to try.
            self._optimistic = None
            self._optimistic_since = None
            return None

        current = self._optimistic
        # A peer that has left cannot keep the slot until the interval is up:
        # the give-away exists to meet new peers, not to wait for old ones.
        still_interested = current is not None and any(peer.key == current for peer in candidates)
        fresh = (
            self._optimistic_since is None
            or now - self._optimistic_since >= self.config.optimistic_interval
        )
        if not still_interested or fresh:
            self._optimistic = self._rotate(candidates, now=now)
            self._optimistic_since = now
        if self._optimistic is not None:
            self._rounds[self._optimistic] = self._rounds.get(self._optimistic, 0)
        return self._optimistic

    def _rotate(self, candidates: list[PeerAccounting], *, now: float) -> str:
        """Hand the slot to whoever has had it least, then given least."""
        ranked = sorted(
            candidates,
            key=lambda peer: (
                self._rounds.get(peer.key, 0),
                peer.bytes_up,
                peer.key,
            ),
        )
        chosen = ranked[0]
        self._rounds[chosen.key] = self._rounds.get(chosen.key, 0) + 1
        return chosen.key
