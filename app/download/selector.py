"""Piece selection strategies (FR-07, TRD §26).

Given the pieces we still need, in what order should we ask for them?

* **Sequential** is the debugging strategy: predictable, and useless on a real
  swarm, because every peer ends up fighting over the same rare early pieces.
* **Rarest-first** is the real answer. A piece held by one peer is one peer
  away from being unobtainable, so it is fetched while it still can be; common
  pieces can be picked up any time. The effect is that a swarm self-balances:
  rare pieces propagate before they can be lost.
* **Random** is the control case, and what a client falls back to when it
  cannot tell rarity (no availability data, e.g. a private torrent with one
  peer).

Selection is a pure function of ``(wanted, availability)`` so it can be tested
without a socket in sight.
"""

from __future__ import annotations

import logging
from random import Random

from app.core.config import PieceStrategy
from app.peer.bitfield import Bitfield, PieceAvailability

logger = logging.getLogger(__name__)


class PieceSelector:
    """Chooses the order in which to request the pieces we still need.

    Args:
        piece_count: Number of pieces in the torrent.
        strategy: Which ordering to use.
        rng: Random source for the ``RANDOM`` strategy. Injected so tests are
            deterministic and the strategy never surprises its caller.
    """

    def __init__(
        self,
        piece_count: int,
        *,
        strategy: PieceStrategy = PieceStrategy.RAREST_FIRST,
        rng: Random | None = None,
    ) -> None:
        if piece_count < 0:
            raise ValueError(f"piece count must not be negative, got {piece_count}")
        self._piece_count = piece_count
        self._strategy = strategy
        self._rng = rng or Random()

    @property
    def piece_count(self) -> int:
        """Number of pieces in the torrent."""
        return self._piece_count

    @property
    def strategy(self) -> PieceStrategy:
        """The ordering in force."""
        return self._strategy

    def select(
        self,
        wanted: Bitfield,
        *,
        availability: PieceAvailability | None = None,
        limit: int | None = None,
    ) -> tuple[int, ...]:
        """Order the pieces we want.

        Args:
            wanted: Bitfield of pieces still needed (gaps, not owned pieces).
            availability: How many peers hold each piece. Required for
                rarest-first; the other strategies ignore it.
            limit: Optional maximum number of indices to return. Schedulers
                ask for more than they need only rarely — planning one round
                of requests at a time keeps the pipeline small.

        Returns:
            Piece indices, best to request first.
        """
        if wanted.piece_count != self._piece_count:
            raise ValueError(
                f"wanted bitfield covers {wanted.piece_count} pieces, "
                f"but the selector was built for {self._piece_count}"
            )

        match self._strategy:
            case PieceStrategy.SEQUENTIAL:
                ordered = self._sequential(wanted)
            case PieceStrategy.RANDOM:
                ordered = self._random(wanted)
            case _:
                ordered = self._rarest_first(wanted, availability)

        return tuple(ordered[:limit]) if limit is not None else tuple(ordered)

    def _sequential(self, wanted: Bitfield) -> list[int]:
        """Lowest index first: simple, predictable, and easy to debug."""
        return [index for index in range(self._piece_count) if wanted.has(index)]

    def _random(self, wanted: Bitfield) -> list[int]:
        """Shuffled, seeded by the injected RNG so runs are reproducible."""
        candidates = self._sequential(wanted)
        self._rng.shuffle(candidates)
        return candidates

    def _rarest_first(self, wanted: Bitfield, availability: PieceAvailability | None) -> list[int]:
        """Fewest holders first, ties broken by index.

        Without availability data there is no rarity to sort by, so this falls
        back to sequential order and says so once.
        """
        if availability is None:
            logger.debug("no availability data; falling back to sequential order")
            return self._sequential(wanted)
        return availability.rarest(wanted)
