"""The view model primitives: bounded series, throttling, honest ratios.

These three are the load-bearing parts of every chart in the client, and each
one exists because of a specific failure:

* A series that grows forever is a memory leak with a nice interface.
* A chart that redraws on every sample is a busy loop at swarm speed.
* A health of 100 % computed from zero peers is a number nobody can trust.
"""

from __future__ import annotations

import pytest
from app.ui.viewmodels.base import Health, SeriesBuffer, Throttle, ratio


class TestSeriesBuffer:
    def test_it_remembers_in_order(self) -> None:
        buffer = SeriesBuffer(capacity=5)
        for index in range(5):
            assert buffer.append(float(index), now=float(index))
        assert [value for _stamp, value in buffer.samples] == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_it_forgets_the_oldest_rather_than_growing(self) -> None:
        buffer = SeriesBuffer(capacity=3)
        for index in range(10):
            buffer.append(float(index), now=float(index))
        assert len(buffer) == 3
        assert [value for _stamp, value in buffer.samples] == [7.0, 8.0, 9.0]

    def test_a_minimum_gap_drops_samples_not_needed(self) -> None:
        buffer = SeriesBuffer(capacity=10, min_gap_seconds=0.5)
        assert buffer.append(1.0, now=0.0)
        assert not buffer.append(2.0, now=0.1)
        assert buffer.append(3.0, now=0.6)
        assert [value for _stamp, value in buffer.samples] == [1.0, 3.0]

    def test_a_sample_taken_exactly_on_time_survives_a_large_clock(self) -> None:
        """The gap is a floor with slack in it, not a knife edge.

        ``time.monotonic()`` grows with uptime, so real stamps are large floats,
        and for roughly one uptime in sixty ``base + 1.0 - base`` rounds to a
        hair under 1.0 instead of to it. Each base below is one that did: before
        :data:`~app.ui.viewmodels.base.INTERVAL_TOLERANCE_SECONDS` each dropped a
        sample that arrived exactly on time, which is how a five-minute chart
        window became a five-minute-and-one-second one on some machines and not
        others.
        """
        for base in (9.9, 777.7372940440583, 1925.8549559671494, 16324.469312085103):
            buffer = SeriesBuffer(capacity=400, min_gap_seconds=1.0)
            kept = [buffer.append(1.0, now=base + index) for index in range(350)]

            assert all(kept), f"base {base}: dropped {kept.count(False)} on-time samples"
            assert len(buffer) == 350
            assert buffer.span == pytest.approx(349.0, abs=0.001)

    def test_an_empty_buffer_has_no_opinions(self) -> None:
        buffer = SeriesBuffer(capacity=4)
        assert buffer.latest is None
        assert buffer.maximum == 0.0
        assert buffer.span == 0.0
        assert buffer.samples == ()

    def test_span_is_measured_not_assumed(self) -> None:
        buffer = SeriesBuffer(capacity=4)
        buffer.append(1.0, now=10.0)
        buffer.append(2.0, now=14.0)
        assert buffer.span == 4.0

    def test_clearing_forgets_everything(self) -> None:
        buffer = SeriesBuffer(capacity=4)
        buffer.append(1.0, now=0.0)
        buffer.clear()
        assert len(buffer) == 0

    def test_a_capacity_of_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            SeriesBuffer(capacity=0)

    def test_a_negative_gap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="gap"):
            SeriesBuffer(capacity=2, min_gap_seconds=-1.0)


class TestThrottle:
    def test_the_first_call_is_allowed(self) -> None:
        assert Throttle(interval_ms=50).allows(now=0.0)

    def test_a_call_inside_the_interval_is_refused(self) -> None:
        throttle = Throttle(interval_ms=50)
        assert throttle.allows(now=0.0)
        assert not throttle.allows(now=0.01)

    def test_a_call_after_the_interval_is_allowed(self) -> None:
        throttle = Throttle(interval_ms=50)
        throttle.allows(now=0.0)
        assert throttle.allows(now=0.2)

    def test_a_refused_call_does_not_consume_the_allowance(self) -> None:
        throttle = Throttle(interval_ms=50)
        throttle.allows(now=0.0)
        for offset in (0.001, 0.002, 0.003):
            assert not throttle.allows(now=offset)
        assert throttle.allows(now=0.06)

    def test_a_call_exactly_one_interval_later_is_allowed_on_a_large_clock(self) -> None:
        # ``(base + 0.05 - base) * 1000`` is 49.999999999272404 for this base, so
        # the frame used to be refused. Roughly two monotonic bases in three did
        # it, which is to say the 20 fps repaint budget depended on how long the
        # machine had been up.
        base = 11112.469893271062
        throttle = Throttle(interval_ms=50)

        assert throttle.allows(now=base)
        assert throttle.allows(now=base + 0.05)

    def test_reset_forgets_the_last_call(self) -> None:
        throttle = Throttle(interval_ms=1000)
        throttle.allows(now=0.0)
        throttle.reset()
        assert throttle.allows(now=0.001)

    def test_the_default_interval_is_the_frame_budget(self) -> None:
        assert Throttle().interval_ms == 50  # 1000 / 20 fps


class TestHealth:
    def test_a_ratio_of_counts(self) -> None:
        health = ratio(3, 8)
        assert health.fraction == pytest.approx(0.375)
        assert health.note == "3/8"

    def test_a_note_can_explain_the_counts(self) -> None:
        assert ratio(3, 8, note="3/8 unchoked").note == "3/8 unchoked"

    def test_nothing_to_measure_is_not_perfect(self) -> None:
        health = ratio(0, 0)
        assert health.fraction is None
        assert not health.known

    def test_more_than_the_whole_is_clamped(self) -> None:
        assert ratio(9, 8).fraction == 1.0

    def test_a_known_health_is_known(self) -> None:
        assert Health(0.5).known
