"""The number formatters: one shape for every number in the interface.

If these disagree with each other, the same torrent shows different sizes on
different tabs, which is worse than any individual rounding choice. So the rules
are pinned here: binary units, an unknown value is "--" and not zero, and a
percentage never exceeds 100.
"""

from __future__ import annotations

import pytest
from app.ui.format import (
    UNKNOWN,
    human_bytes,
    human_count,
    human_duration,
    human_percent,
    human_rate,
    human_ratio,
)


class TestBytes:
    def test_bytes_stay_bytes(self) -> None:
        assert human_bytes(0) == "0 B"
        assert human_bytes(1023) == "1023 B"

    def test_units_are_binary(self) -> None:
        assert human_bytes(1024) == "1.00 KiB"
        assert human_bytes(1024**2) == "1.00 MiB"
        assert human_bytes(1536 * 1024) == "1.50 MiB"

    def test_a_real_torrent_size_reads_well(self) -> None:
        assert human_bytes(791_674_880) == "755.00 MiB"

    def test_unmeasured_is_not_zero(self) -> None:
        assert human_bytes(None) == UNKNOWN

    def test_a_negative_count_is_refused(self) -> None:
        assert human_bytes(-1) == UNKNOWN


class TestRates:
    def test_idle_reads_as_a_measured_zero(self) -> None:
        assert human_rate(0.0) == "0 B/s"

    def test_a_rate_carries_its_per_second(self) -> None:
        assert human_rate(1024 * 256).startswith("256.00 KiB")
        assert human_rate(1024 * 256).endswith("/s")

    def test_an_unmeasured_rate_is_unknown(self) -> None:
        assert human_rate(None) == UNKNOWN


class TestDurations:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s"),
            (45, "45s"),
            (60, "1m 00s"),
            (252, "4m 12s"),
            (3600, "1h 00m"),
            (86400, "1d 00h"),
        ],
    )
    def test_durations_read_the_way_a_user_says_them(self, seconds: float, expected: str) -> None:
        assert human_duration(seconds) == expected

    def test_an_unknown_eta_is_not_due_now(self) -> None:
        assert human_duration(None) == UNKNOWN
        assert human_duration(-5) == UNKNOWN


class TestPercents:
    def test_a_fraction_becomes_a_percentage(self) -> None:
        assert human_percent(0.634) == "63.4%"

    def test_more_than_the_whole_is_clamped(self) -> None:
        assert human_percent(1.4) == "100.0%"
        assert human_percent(-0.2) == "0.0%"

    def test_places_are_honoured(self) -> None:
        assert human_percent(0.5, places=0) == "50%"


class TestOthers:
    def test_a_ratio_before_anything_is_received(self) -> None:
        assert human_ratio(None) == UNKNOWN
        assert human_ratio(1.5) == "1.50"

    def test_counts_are_grouped(self) -> None:
        assert human_count(3020) == "3,020"
        assert human_count(None) == UNKNOWN
