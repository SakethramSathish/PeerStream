"""Tests for the rolling-window rate meters.

These tests do not wait: the clock is injected, so an hour of swarm behaviour
is a few microseconds of arithmetic. What they check is the part that is easy
to get wrong — that a rate is about the *last* few seconds, that it forgets,
and that it never invents a number when nothing happened.
"""

from __future__ import annotations

import pytest
from app.statistics.speed import Counter, CounterSet, RateMeter, SpeedMeter, SpeedRates

# --------------------------------------------------------------------- windows


class TestRateMeter:
    def test_a_meter_with_nothing_in_it_reads_zero(self) -> None:
        assert RateMeter(5.0).rate(now=10.0) == 0.0

    def test_bytes_over_a_window_is_a_rate(self) -> None:
        meter = RateMeter(window=5.0)

        meter.add(5_000, now=1.0)

        assert meter.rate(now=1.0) == pytest.approx(1_000.0)

    def test_the_window_is_the_denominator_not_the_elapsed_time(self) -> None:
        """Two seconds of traffic is still averaged over the whole window."""
        meter = RateMeter(window=10.0)

        meter.add(2_000, now=8.0)
        meter.add(2_000, now=9.0)

        assert meter.rate(now=10.0) == pytest.approx(400.0)

    def test_samples_fall_out_of_the_window(self) -> None:
        meter = RateMeter(window=5.0)

        meter.add(1_000, now=0.0)
        meter.add(1_000, now=100.0)

        assert meter.sample_count == 1
        assert meter.rate(now=100.0) == pytest.approx(200.0)

    def test_an_idle_meter_decays_to_zero(self) -> None:
        """A stalled download shows 0 B/s, not the speed it used to have."""
        meter = RateMeter(window=5.0)

        meter.add(5_000, now=0.0)
        assert meter.rate(now=0.0) > 0.0

        assert meter.rate(now=10.0) == 0.0

    def test_the_total_survives_the_window(self) -> None:
        meter = RateMeter(window=1.0)

        for stamp in range(10):
            meter.add(100, now=float(stamp))

        assert meter.total == 1_000
        assert meter.rate(now=9.0) == pytest.approx(100.0)  # only the last second

    def test_nothing_is_counted_when_nothing_moved(self) -> None:
        meter = RateMeter(window=5.0)

        meter.add(0, now=1.0)
        meter.add(-50, now=1.0)

        assert meter.total == 0
        assert meter.sample_count == 0

    def test_a_window_has_to_be_a_length_of_time(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RateMeter(0.0)
        with pytest.raises(ValueError, match="positive"):
            RateMeter(-1.0)

    def test_reset_forgets_the_window_and_the_total(self) -> None:
        meter = RateMeter(window=5.0)
        meter.add(1_000, now=0.0)

        meter.reset()

        assert meter.total == 0
        assert meter.rate(now=1.0) == 0.0

    def test_prune_reports_how_much_it_forgot(self) -> None:
        meter = RateMeter(window=5.0)
        meter.add(1_000, now=0.0)
        meter.add(1_000, now=1.0)

        assert meter.prune(now=10.0) == 2
        assert meter.prune(now=10.0) == 0


# ---------------------------------------------------------------------- speeds


class TestSpeedMeter:
    def test_every_window_sees_the_same_bytes(self) -> None:
        meter = SpeedMeter((1.0, 5.0, 30.0))

        meter.add(1_000, now=0.0)

        assert meter.rate(window=1.0, now=0.0) == pytest.approx(1_000.0)
        assert meter.rate(window=5.0, now=0.0) == pytest.approx(200.0)
        assert meter.rate(window=30.0, now=0.0) == pytest.approx(33.333, rel=1e-2)

    def test_a_short_window_reacts_faster_than_a_long_one(self) -> None:
        """The long window remembers the burst; the short one has moved on."""
        meter = SpeedMeter((1.0, 5.0, 30.0))

        meter.add(10_000, now=0.0)  # a burst
        for stamp in range(1, 5):
            meter.add(100, now=float(stamp))

        rates = meter.rates(now=4.0)

        assert rates.instant < rates.long
        assert rates.instant == pytest.approx(100.0)

    def test_omitting_the_window_gives_the_longest(self) -> None:
        meter = SpeedMeter((1.0, 30.0))

        meter.add(300, now=0.0)

        assert meter.rate(now=0.0) == pytest.approx(10.0)

    def test_the_session_average_includes_the_idle_stretches(self) -> None:
        """Honest session speed: total bytes over the time since byte one."""
        meter = SpeedMeter((1.0, 5.0))

        meter.add(1_000, now=0.0)
        meter.add(1_000, now=10.0)

        assert meter.total == 2_000
        assert meter.rates(now=10.0).average == pytest.approx(200.0)
        assert meter.elapsed(now=10.0) == pytest.approx(10.0)

    def test_nothing_has_elapsed_before_the_first_byte(self) -> None:
        meter = SpeedMeter((1.0,))

        assert meter.started_at is None
        assert meter.elapsed(now=100.0) == 0.0
        assert meter.rates(now=100.0).average == 0.0

    def test_the_average_follows_the_clock_when_no_time_is_given(self) -> None:
        """A meter with an injected clock reads the same numbers twice."""
        ticks = [0.0]
        meter = SpeedMeter((1.0,), clock=lambda: ticks[-1])

        meter.add(1_000)  # at t=0
        ticks.append(2.0)

        assert meter.elapsed() == pytest.approx(2.0)
        assert meter.average == pytest.approx(500.0)

    def test_a_burst_reads_higher_than_the_average(self) -> None:
        meter = SpeedMeter((1.0, 5.0))

        meter.add(5_000, now=0.0)
        rates = meter.rates(now=0.0)
        assert rates.instant > rates.average or rates.instant == rates.average

        for stamp in range(1, 60):
            meter.add(10, now=float(stamp))
        later = meter.rates(now=59.0)

        assert later.average < rates.instant

    def test_unknown_windows_are_rejected_rather_than_guessed(self) -> None:
        meter = SpeedMeter((1.0, 5.0))

        with pytest.raises(ValueError, match="no meter for window"):
            meter.rate(window=7.0, now=0.0)

    def test_a_meter_needs_a_window(self) -> None:
        with pytest.raises(ValueError, match="at least one window"):
            SpeedMeter(())

    def test_a_single_window_still_fills_the_snapshot(self) -> None:
        meter = SpeedMeter((5.0,))

        meter.add(500, now=0.0)
        rates = meter.rates(now=1.0)

        assert rates == SpeedRates(instant=100.0, short=100.0, long=100.0, average=500.0, total=500)

    def test_the_displayed_rate_is_the_short_window(self) -> None:
        meter = SpeedMeter((1.0, 5.0, 30.0))
        meter.add(1_000, now=0.0)

        assert meter.rates(now=0.0).displayed == meter.rates(now=0.0).short

    def test_reset_forgets_when_we_started_too(self) -> None:
        meter = SpeedMeter((1.0,))
        meter.add(100, now=0.0)

        meter.reset()

        assert meter.started_at is None
        assert meter.total == 0
        assert meter.rates(now=1.0).average == 0.0


# -------------------------------------------------------------------- counters


class TestCounters:
    def test_a_counter_remembers_when_it_last_moved(self) -> None:
        counter = Counter(name="pieces_verified")

        moved = counter.bump(2, now=10.0)

        assert moved.value == 2
        assert moved.seconds_since(now=13.0) == pytest.approx(3.0)

    def test_a_counter_that_never_moved_has_no_age(self) -> None:
        assert Counter(name="x").seconds_since(now=5.0) is None

    def test_bumping_by_nothing_changes_nothing(self) -> None:
        counter = Counter(name="x", value=3)

        assert counter.bump(0, now=1.0) is counter

    def test_a_set_hands_out_names_on_demand(self) -> None:
        counters = CounterSet(clock=lambda: 1.0)

        counters.bump("pieces_verified")
        counters.bump("pieces_verified")
        counters.bump("pieces_failed")

        assert counters.as_dict() == {"pieces_verified": 2, "pieces_failed": 1}
        assert counters.names() == ("pieces_verified", "pieces_failed")

    def test_an_unknown_counter_is_zero_not_an_error(self) -> None:
        assert CounterSet().get("never_seen") == 0


class TestTheMeterItself:
    def test_the_windows_are_visible(self) -> None:
        assert SpeedMeter((1.0, 5.0)).windows == (1.0, 5.0)

    def test_snapshot_is_another_word_for_rates(self) -> None:
        """Two names for one read, because callers sample on a schedule."""
        meter = SpeedMeter((1.0, 5.0))
        meter.add(1_000, now=0.0)

        assert meter.snapshot(now=1.0) == meter.rates(now=1.0)
