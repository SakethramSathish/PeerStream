"""Tests for the bounded history buffers.

The promise these tests hold the module to is a memory promise: a graph that
has been filling for six hours costs exactly as much as one filling for six
seconds. Everything else — span, peak, mean — is arithmetic on what is left.
"""

from __future__ import annotations

import pytest
from app.statistics.history import History, HistoryBook, Sample


class TestHistory:
    def test_a_new_history_is_empty(self) -> None:
        history = History(capacity=4)

        assert len(history) == 0
        assert history.latest is None
        assert history.oldest is None
        assert history.series() == ()

    def test_samples_are_kept_oldest_first(self) -> None:
        history = History(capacity=4)

        history.add(1.0, now=0.0)
        history.add(2.0, now=1.0)

        assert history.values() == (1.0, 2.0)
        assert history.timestamps() == (0.0, 1.0)

    def test_the_buffer_never_grows_past_its_capacity(self) -> None:
        """The whole point: an hour of samples costs the same as a minute."""
        history = History(capacity=3)

        for value in range(100):
            history.add(float(value), now=float(value))

        assert len(history) == 3
        assert history.values() == (97.0, 98.0, 99.0)

    def test_what_fell_off_the_end_is_counted_not_kept(self) -> None:
        history = History(capacity=2)

        for value in range(5):
            history.add(float(value), now=float(value))

        assert history.added == 5
        assert history.dropped == 3
        assert history.full is True

    def test_a_span_is_the_time_between_the_ends(self) -> None:
        history = History(capacity=5)

        history.add(1.0, now=10.0)
        history.add(1.0, now=12.0)
        history.add(1.0, now=15.0)

        assert history.span == pytest.approx(5.0)

    def test_peak_and_floor_and_mean_describe_what_is_left(self) -> None:
        history = History(capacity=4)

        for value in (1.0, 5.0, 3.0):
            history.add(value, now=float(value))

        assert history.peak == 5.0
        assert history.floor == 1.0
        assert history.mean == pytest.approx(3.0)

    def test_an_empty_history_describes_itself_as_zero(self) -> None:
        history = History(capacity=3)

        assert history.peak == 0.0
        assert history.floor == 0.0
        assert history.mean == 0.0
        assert history.span == 0.0

    def test_several_values_can_share_one_instant(self) -> None:
        history = History(capacity=4)

        assert history.extend([1.0, 2.0], now=7.0) == 2
        assert [sample.timestamp for sample in history.series()] == [7.0, 7.0]

    def test_clearing_keeps_the_count_of_what_was_added(self) -> None:
        history = History(capacity=4)
        history.add(1.0, now=0.0)

        history.clear()

        assert history.series() == ()
        assert history.added == 1

    def test_a_history_needs_room_for_at_least_one_sample(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            History(capacity=0)

    def test_a_history_can_be_iterated(self) -> None:
        history = History(capacity=2)
        history.add(1.0, now=0.0)

        assert [sample.value for sample in history] == [1.0]
        assert history.series()[0] == Sample(timestamp=0.0, value=1.0)


class TestHistoryBook:
    def test_a_series_appears_when_it_is_recorded(self) -> None:
        book = HistoryBook(capacity=2)

        book.record("download_rate", 100.0, now=0.0)

        assert book.names == ("download_rate",)
        assert "download_rate" in book
        assert len(book) == 1

    def test_every_series_is_bounded_on_its_own(self) -> None:
        book = HistoryBook(capacity=2)

        for value in range(10):
            book.record("download_rate", float(value), now=float(value))
            book.record("upload_rate", float(value) * 2, now=float(value))

        assert book.values("download_rate") == (8.0, 9.0)
        assert book.values("upload_rate") == (16.0, 18.0)

    def test_asking_for_a_series_does_not_create_one_on_the_sly(self) -> None:
        """A graph should not appear because somebody looked at it."""
        book = HistoryBook(capacity=2)

        assert book.series("never_recorded") == ()
        assert book.latest("never_recorded") is None
        assert book.names == ()

    def test_reading_a_series_by_name_is_the_same_series(self) -> None:
        book = HistoryBook(capacity=2)
        book.record("peers", 1.0, now=0.0)

        book["peers"].add(2.0, now=1.0)

        assert book.values("peers") == (1.0, 2.0)

    def test_the_book_hands_over_every_series_at_once(self) -> None:
        book = HistoryBook(capacity=4)
        book.record("peers", 3.0, now=0.0)
        book.record("progress", 0.5, now=0.0)

        exported = book.as_dict()

        assert sorted(exported) == ["peers", "progress"]
        assert exported["peers"] == (Sample(timestamp=0.0, value=3.0),)

    def test_clearing_empties_the_series_but_keeps_them(self) -> None:
        book = HistoryBook(capacity=2)
        book.record("peers", 1.0, now=0.0)

        book.clear()

        assert book.names == ("peers",)
        assert book.series("peers") == ()

    def test_a_series_can_be_forgotten(self) -> None:
        book = HistoryBook(capacity=2)
        book.record("peers", 1.0, now=0.0)

        assert book.forget("peers") is True
        assert book.forget("peers") is False
        assert book.names == ()


class TestTheBufferItself:
    def test_the_capacity_is_visible(self) -> None:
        assert History(capacity=7).capacity == 7
        assert HistoryBook(capacity=7).capacity == 7
