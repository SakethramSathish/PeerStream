"""Tests for the upload rate limiter.

The bucket is pure, so these tests drive it with a stopwatch they control
instead of waiting for real time to pass: an hour of seeding is one addition.
"""

from __future__ import annotations

import pytest
from app.upload.rate import TokenBucket


class TestConstruction:
    def test_an_unlimited_bucket_lets_everything_through(self) -> None:
        bucket = TokenBucket()

        assert bucket.unlimited is True
        assert bucket.take(10_000_000, now=0.0) == (0.0, bucket)

    def test_a_negative_rate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="rate must not be negative"):
            TokenBucket(rate=-1)

    def test_a_negative_capacity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="capacity must not be negative"):
            TokenBucket(capacity=-1)

    def test_a_negative_length_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="length must not be negative"):
            TokenBucket(rate=100).take(-1, now=0.0)


class TestRefill:
    def test_tokens_grow_with_time(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=2000.0, tokens=0.0, updated_at=0.0)

        assert bucket.refill(now=1.5).tokens == 1500.0

    def test_tokens_never_exceed_capacity(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=0.0, updated_at=0.0)

        assert bucket.refill(now=60.0).tokens == 1000.0

    def test_capacity_defaults_to_one_second_of_rate(self) -> None:
        bucket = TokenBucket(rate=500.0, tokens=0.0, updated_at=0.0)

        assert bucket.refill(now=60.0).tokens == 500.0

    def test_time_going_backwards_changes_nothing(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=100.0, updated_at=10.0)

        assert bucket.refill(now=5.0).tokens == 100.0


class TestDelays:
    def test_a_request_within_budget_goes_now(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=1000.0, updated_at=0.0)

        assert bucket.delay_for(400, now=0.0) == 0.0

    def test_a_request_over_budget_waits_for_the_difference(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=200.0, updated_at=0.0)

        assert bucket.delay_for(700, now=0.0) == pytest.approx(0.5)

    def test_an_empty_bucket_matches_the_data(self) -> None:
        """The numbers the tool prints are the numbers the bucket computes."""
        bucket = TokenBucket(rate=256 * 1024, capacity=256 * 1024, tokens=0.0, updated_at=0.0)
        length = 16 * 1024  # one block

        assert bucket.delay_for(length, now=0.0) == pytest.approx(1 / 16)

    def test_asking_costs_nothing(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=1000.0, updated_at=0.0)

        before = bucket.delay_for(400, now=0.0)

        assert bucket.tokens == 1000.0  # asking is free
        assert before == 0.0


class TestSpending:
    def test_spending_removes_tokens(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=1000.0, updated_at=0.0)

        delay, after = bucket.take(400, now=0.0)

        assert delay == 0.0
        assert after.tokens == pytest.approx(600.0)

    def test_an_over_budget_send_is_delayed_and_accounted_for(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=100.0, updated_at=0.0)

        delay, after = bucket.take(600, now=0.0)

        assert delay == pytest.approx(0.5)
        # The bytes are spent even though they were not there yet: the bucket
        # is 500 in debt, and waiting exactly the delay is what clears it.
        assert after.tokens == pytest.approx(-500.0)
        assert after.refill(now=0.5).tokens == pytest.approx(0.0)

    def test_a_bucket_in_debt_does_not_lend_twice(self) -> None:
        """Skipping the wait does not make the debt disappear."""
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=100.0, updated_at=0.0)

        _, bucket = bucket.take(600, now=0.0)
        delay, _ = bucket.take(600, now=0.0)

        assert delay == pytest.approx(1.1)  # 500 owed plus 600 more

    def test_sending_nothing_changes_nothing(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=1000.0, updated_at=0.0)

        delay, after = bucket.take(0, now=1.0)

        assert delay == 0.0
        assert after.tokens == 1000.0

    def test_successive_sends_add_up(self) -> None:
        bucket = TokenBucket(rate=1000.0, capacity=1000.0, tokens=1000.0, updated_at=0.0)

        _, bucket = bucket.take(600, now=0.0)
        delay, bucket = bucket.take(600, now=0.0)

        assert delay == pytest.approx(0.2)  # 200 left, 400 short at 1000 B/s


class TestEdgeCases:
    def test_an_empty_need_costs_nothing(self) -> None:
        bucket = TokenBucket(rate=100_000)

        assert bucket.delay_for(0, now=1.0) == 0.0
        assert bucket.delay_for(-1, now=1.0) == 0.0
