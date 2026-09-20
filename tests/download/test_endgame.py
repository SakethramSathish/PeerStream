"""Tests for endgame mode (TRD §27).

The rule: below the threshold, the same block may be given to more than one
peer; above it, only one peer may hold a request for a block. Everything else
follows from that.
"""

from __future__ import annotations

import pytest
from app.download.endgame import EndgameTracker

KEY = (0, 0)


class TestActivation:
    def test_endgame_activates_at_the_threshold(self) -> None:
        tracker = EndgameTracker(threshold=20)

        assert tracker.active(21) is False
        assert tracker.active(20) is True
        assert tracker.active(1) is True

    def test_it_can_be_disabled(self) -> None:
        tracker = EndgameTracker(enabled=False, threshold=20)

        assert tracker.active(1) is False

    def test_a_negative_threshold_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            EndgameTracker(threshold=-1)


class TestDuplication:
    def test_a_second_peer_is_allowed_in_endgame(self) -> None:
        tracker = EndgameTracker(threshold=20)

        assert tracker.allows(KEY, "a:1", outstanding_blocks=50) is True
        tracker.note_request(KEY, "a:1")

        assert tracker.allows(KEY, "b:2", outstanding_blocks=50) is False
        assert tracker.allows(KEY, "b:2", outstanding_blocks=5) is True

    def test_a_peer_never_gets_the_same_block_twice(self) -> None:
        tracker = EndgameTracker(threshold=20)
        tracker.note_request(KEY, "a:1")

        assert tracker.allows(KEY, "a:1", outstanding_blocks=1) is False

    def test_duplicates_are_counted(self) -> None:
        tracker = EndgameTracker(threshold=20)
        tracker.note_request(KEY, "a:1")
        tracker.note_request(KEY, "b:2")

        assert tracker.duplicates == 1
        # A repeat from the same peer is not a new race.
        tracker.note_request(KEY, "b:2")
        assert tracker.duplicates == 1


class TestBookkeeping:
    def test_requesters_are_listed_in_order(self) -> None:
        tracker = EndgameTracker()
        tracker.note_request(KEY, "b:2")
        tracker.note_request(KEY, "a:1")

        assert tracker.requesters(KEY) == ("a:1", "b:2")
        assert tracker.in_flight == 1

    def test_the_winner_cancels_the_losers(self) -> None:
        tracker = EndgameTracker()
        tracker.note_request(KEY, "a:1")
        tracker.note_request(KEY, "b:2")
        tracker.note_request(KEY, "c:3")

        assert tracker.note_received(KEY, "a:1") == ("b:2", "c:3")
        assert tracker.requesters(KEY) == ()
        assert tracker.in_flight == 0

    def test_a_solo_request_cancels_nobody(self) -> None:
        tracker = EndgameTracker()
        tracker.note_request(KEY, "a:1")

        assert tracker.note_received(KEY, "a:1") == ()

    def test_a_lost_request_frees_the_block(self) -> None:
        tracker = EndgameTracker()
        tracker.note_request(KEY, "a:1")
        tracker.note_request(KEY, "b:2")

        assert tracker.note_lost(KEY, "a:1") is False  # b still has it
        assert tracker.note_lost(KEY, "b:2") is True
        assert tracker.in_flight == 0

    def test_losing_an_unknown_request_is_a_no_op(self) -> None:
        assert EndgameTracker().note_lost(KEY, "a:1") is False

    def test_forget_drops_a_block(self) -> None:
        tracker = EndgameTracker()
        tracker.note_request(KEY, "a:1")

        tracker.forget(KEY)

        assert tracker.in_flight == 0

    def test_clear_drops_everything(self) -> None:
        tracker = EndgameTracker()
        tracker.note_request(KEY, "a:1")
        tracker.note_request((1, 0), "a:1")

        tracker.clear()

        assert tracker.in_flight == 0

    def test_properties(self) -> None:
        tracker = EndgameTracker(enabled=False, threshold=7)

        assert tracker.enabled is False
        assert tracker.threshold == 7
