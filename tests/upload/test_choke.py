"""Tests for the choking policy.

The policy is pure, so these tests are not about sockets or timing: they hand
it a snapshot of what peers have done and check who it decides to feed. An
hour of swarm behaviour takes a millisecond.
"""

from __future__ import annotations

from app.core.config import UploadConfig
from app.upload.choke import ChokeDecision, ChokePolicy, PeerAccounting


def peer(
    key: str,
    *,
    interested: bool = True,
    choking_us: bool = False,
    down: int = 0,
    up: int = 0,
    last: float | None = 0.0,
) -> PeerAccounting:
    return PeerAccounting(
        key=key,
        interested=interested,
        choking_us=choking_us,
        bytes_up=up,
        bytes_down=down,
        last_download_at=last,
    )


def policy(**kwargs: object) -> ChokePolicy:
    return ChokePolicy(config=UploadConfig(**kwargs))  # type: ignore[arg-type]


class TestSlots:
    def test_nobody_interested_nobody_unchoked(self) -> None:
        peers = {"a:1": peer("a:1", interested=False), "b:2": peer("b:2", interested=False)}

        decision = policy().evaluate(peers, now=0.0)

        assert decision.allowed == ()

    def test_a_peer_that_wants_nothing_is_never_unchoked(self) -> None:
        peers = {"a:1": peer("a:1", interested=False, down=10_000), "b:2": peer("b:2")}

        decision = policy().evaluate(peers, now=0.0)

        assert decision.allowed == ("b:2",)

    def test_slots_are_filled_by_what_a_peer_gave_us(self) -> None:
        peers = {
            "slow:1": peer("slow:1", down=1_000),
            "fast:2": peer("fast:2", down=9_000),
            "idle:3": peer("idle:3", down=0),
        }

        decision = policy(slots=2).evaluate(peers, now=0.0)

        assert decision.unchoked == ("fast:2", "slow:1")

    def test_equal_peers_are_ranked_deterministically(self) -> None:
        peers = {key: peer(key, down=5) for key in ("c:3", "a:1", "b:2")}

        first = policy(slots=3).evaluate(peers, now=0.0)
        second = policy(slots=3).evaluate(dict(reversed(list(peers.items()))), now=0.0)

        assert first.unchoked == second.unchoked == ("a:1", "b:2", "c:3")

    def test_a_peer_we_have_served_least_wins_a_tie(self) -> None:
        """Two peers gave us the same; the one we have fed less goes first."""
        peers = {"fed:1": peer("fed:1", down=100, up=5_000), "hungry:2": peer("hungry:2", down=100)}

        decision = policy(slots=1).evaluate(peers, now=0.0)

        assert decision.unchoked == ("hungry:2",)

    def test_zero_slots_means_upload_to_nobody(self) -> None:
        peers = {"a:1": peer("a:1")}

        decision = policy(slots=0).evaluate(peers, now=0.0)

        assert decision.allowed == ()

    def test_more_peers_than_slots_means_someone_misses_out(self) -> None:
        peers = {key: peer(key, down=index) for index, key in enumerate(("a:1", "b:2", "c:3"))}

        decision = policy(slots=1).evaluate(peers, now=0.0)

        assert decision.unchoked == ("c:3",)


class TestSnubbing:
    def test_a_snubbed_peer_loses_to_a_reciprocal_one(self) -> None:
        peers = {
            "greedy:1": peer("greedy:1", choking_us=True, last=None, down=9_000),
            "fair:2": peer("fair:2", choking_us=False, down=1_000),
        }

        decision = policy(slots=1).evaluate(peers, now=10.0)

        assert decision.unchoked == ("fair:2",)

    def test_a_peer_that_gave_us_something_recently_is_not_snubbed(self) -> None:
        peers = {"a:1": peer("a:1", choking_us=True, last=0.0, down=100)}

        decision = policy(slots=1).evaluate(peers, now=10.0)

        assert decision.unchoked == ("a:1",)

    def test_snubbing_expires_after_the_threshold(self) -> None:
        peers = {"a:1": peer("a:1", choking_us=True, last=0.0, down=100)}

        fresh = policy(slots=1, snub_seconds=60.0).evaluate(peers, now=10.0)
        stale = policy(slots=1, snub_seconds=60.0).evaluate(peers, now=120.0)

        assert fresh.unchoked == ("a:1",)
        # Nobody else is asking, so even a snubbed peer gets the spare capacity.
        assert stale.unchoked == ("a:1",)

    def test_snubbing_is_a_preference_not_a_ban(self) -> None:
        """With capacity to spare, feeding a quiet peer beats feeding nobody."""
        peers = {
            "greedy:1": peer("greedy:1", choking_us=True, last=None),
            "greedy:2": peer("greedy:2", choking_us=True, last=None),
        }

        decision = policy(slots=2).evaluate(peers, now=10.0)

        assert decision.unchoked == ("greedy:1", "greedy:2")


class TestOptimisticRotation:
    def test_a_peer_outside_the_slots_gets_the_optimistic_one(self) -> None:
        peers = {key: peer(key, down=index) for index, key in enumerate(("a:1", "b:2", "c:3"))}

        decision = policy(slots=1).evaluate(peers, now=0.0)

        assert decision.unchoked == ("c:3",)
        assert decision.optimistic in {"a:1", "b:2"}

    def test_the_slot_stays_put_before_the_interval(self) -> None:
        peers = {key: peer(key, down=index) for index, key in enumerate(("a:1", "b:2", "c:3"))}
        chooser = policy(slots=1, optimistic_interval=30.0)

        first = chooser.evaluate(peers, now=0.0)
        second = chooser.evaluate(peers, now=10.0)

        assert first.optimistic == second.optimistic

    def test_the_slot_moves_after_the_interval(self) -> None:
        peers = {key: peer(key, down=index) for index, key in enumerate(("a:1", "b:2", "c:3"))}
        chooser = policy(slots=1, optimistic_interval=30.0)

        first = chooser.evaluate(peers, now=0.0)
        later = chooser.evaluate(peers, now=31.0)

        assert first.optimistic != later.optimistic

    def test_the_rotation_prefers_whoever_has_had_it_least(self) -> None:
        """Everybody outside the regular slots gets a turn before anyone repeats."""
        peers = {
            key: peer(key, down=index) for index, key in enumerate(("a:1", "b:2", "c:3", "d:4"))
        }
        chooser = policy(slots=1, optimistic_interval=1.0)
        seen: list[str | None] = []

        for tick in range(3):
            decision = chooser.evaluate(peers, now=float(tick))
            seen.append(decision.optimistic)

        assert len(set(seen)) == 3  # the three who missed out each get a turn

    def test_optimistic_unchoke_can_be_switched_off(self) -> None:
        peers = {key: peer(key, down=index) for index, key in enumerate(("a:1", "b:2", "c:3"))}
        chooser = policy(slots=1, optimistic_unchoke=False)

        decision = chooser.evaluate(peers, now=0.0)

        assert decision.optimistic is None
        assert decision.allowed == ("c:3",)

    def test_a_peer_that_earned_a_slot_needs_no_give_away(self) -> None:
        peers = {"a:1": peer("a:1")}

        decision = policy(slots=4).evaluate(peers, now=0.0)

        assert decision.optimistic is None
        assert decision.allowed == ("a:1",)


class TestBookkeeping:
    def test_a_departed_peer_is_forgotten(self) -> None:
        chooser = policy(slots=1, optimistic_interval=1.0)
        peers = {"a:1": peer("a:1", down=0), "b:2": peer("b:2", down=100)}
        chooser.evaluate(peers, now=0.0)

        chooser.forget("a:1")

        assert chooser.evaluate(peers, now=2.0).optimistic == "a:1"

    def test_leaving_frees_the_optimistic_slot_immediately(self) -> None:
        chooser = policy(slots=1, optimistic_interval=100.0)
        peers = {"a:1": peer("a:1", down=0), "b:2": peer("b:2", down=100)}
        first = chooser.evaluate(peers, now=0.0)
        assert first.optimistic == "a:1"

        chooser.forget("a:1")
        second = chooser.evaluate(peers, now=1.0)

        assert second.optimistic == "a:1"


class TestDecision:
    def test_allowed_includes_both_kinds_of_slot(self) -> None:
        decision = ChokeDecision(unchoked=("a:1",), optimistic="b:2")

        assert decision.allowed == ("a:1", "b:2")
        assert decision.is_unchoked("b:2") is True
        assert decision.is_unchoked("c:3") is False

    def test_the_optimistic_peer_is_not_counted_twice(self) -> None:
        decision = ChokeDecision(unchoked=("a:1",), optimistic="a:1")

        assert decision.allowed == ("a:1",)

    def test_an_empty_decision_allows_nobody(self) -> None:
        assert ChokeDecision().allowed == ()
        assert ChokeDecision().is_unchoked("a:1") is False


class TestPolicyInternals:
    """Smaller truths about the policy, worth stating out loud."""

    def test_the_slot_count_comes_from_the_config(self) -> None:
        assert policy(slots=7).config_slots == 7

    def test_the_optimistic_peer_is_visible(self) -> None:
        """A test elsewhere checks who it picks; this one checks we can ask."""
        peers = {key: peer(key, down=index) for index, key in enumerate(("a:1", "b:2", "c:3"))}
        chooser = policy(slots=1, optimistic_interval=30.0)

        chooser.evaluate(peers, now=0.0)

        assert chooser.optimistic_peer in {None, "a:1", "b:2", "c:3"}

    def test_a_peer_that_vanishes_leaves_no_trace(self) -> None:
        """Accounting is forgotten even when a peer never said goodbye."""
        peers = {key: peer(key, down=index) for index, key in enumerate(("a:1", "b:2", "c:3"))}
        chooser = policy(slots=1, optimistic_interval=30.0)

        first = chooser.evaluate(peers, now=0.0)
        assert first.optimistic in chooser._rounds

        survivors = {key: value for key, value in peers.items() if key != first.optimistic}
        chooser.evaluate(survivors, now=1.0)

        assert first.optimistic not in chooser._rounds
        assert chooser.optimistic_peer != first.optimistic  # the slot moved on
