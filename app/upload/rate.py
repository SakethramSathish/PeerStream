"""Upload rate limiting (PRD FR-12).

A seeder that sends as fast as the socket allows will crowd out everything
else on the user's connection, so the upload manager paces itself. The pacer
is a token bucket: it holds at most ``capacity`` bytes of permission and
refills at ``rate`` bytes per second, which is "a ceiling on the average with
room for short bursts" in six lines of arithmetic.

The bucket is deliberately pure — it knows nothing about sockets, peers or the
clock source beyond the timestamps it is handed — so a test can drive it with
a stopwatch it controls instead of waiting for real time to pass.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class TokenBucket:
    """A pacer that answers "may I send this much, and if not, how long must
    I wait".

    Args:
        rate: Bytes per second allowed on average. Zero means unlimited, which
            is the default configuration and the case every test starts from.
        capacity: Most bytes that may be sent at once. Defaults to one second
            worth of ``rate``, so a client can burst a little without ever
            exceeding its average.
        tokens: Bytes currently available.
        updated_at: When ``tokens`` was last calculated.
    """

    rate: float = 0.0
    capacity: float = 0.0
    tokens: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.rate < 0:
            raise ValueError(f"rate must not be negative, got {self.rate}")
        if self.capacity < 0:
            raise ValueError(f"capacity must not be negative, got {self.capacity}")

    # ---------------------------------------------------------------- queries

    @property
    def unlimited(self) -> bool:
        """Whether this bucket lets everything through without waiting."""
        return self.rate <= 0

    def refill(self, now: float) -> TokenBucket:
        """Bring the bucket up to date with the clock.

        Refilling is lazy: nothing ticks in the background, and the bucket is
        only ever correct at the moment someone asks about it.
        """
        if self.unlimited:
            return replace(self, tokens=self.capacity, updated_at=now)
        if now <= self.updated_at:
            return self
        capacity = self.capacity or self.rate
        added = (now - self.updated_at) * self.rate
        return replace(self, tokens=min(capacity, self.tokens + added), updated_at=now)

    def delay_for(self, length: int, now: float) -> float:
        """Seconds to wait before ``length`` bytes may be sent.

        Zero means "go now". A request larger than the whole bucket still gets
        a finite answer, because it is the bucket's ceiling that is finite, not
        its patience.
        """
        if self.unlimited or length <= 0:
            return 0.0
        current = self.refill(now)
        if current.tokens >= length:
            return 0.0
        return (length - current.tokens) / current.rate

    # -------------------------------------------------------------- mutations

    def take(self, length: int, now: float) -> tuple[float, TokenBucket]:
        """Spend ``length`` bytes, returning the delay owed and the new bucket.

        Spending happens immediately, even when the bytes are not there yet: a
        bucket may go into debt, and the debt is what the returned delay pays
        off. Modelling it that way — rather than "wait, then spend" — means a
        caller that never waits still cannot send for free, and a caller that
        waits exactly the delay finds its balance at zero and its send paid for.

        The delay is returned rather than awaited, so the caller decides how to
        wait; blocking here would stall every other peer behind this one.
        """
        if length < 0:
            raise ValueError(f"length must not be negative, got {length}")
        current = self.refill(now)
        if current.unlimited or length == 0:
            return 0.0, current
        balance = current.tokens - length
        if balance >= 0:
            return 0.0, replace(current, tokens=balance, updated_at=now)
        return -balance / current.rate, replace(current, tokens=balance, updated_at=now)
