"""The DHT view model: what the panel shows when it has a node, and when it doesn't.

Two states look identical in a table of numbers and must not be presented the
same way: a DHT that is switched off, and a DHT that is listening but has not
reached a soul. The first is a setting; the second is a network fact. These
tests are mostly about keeping those apart, and about the ages and failure
counts being read off the routing table rather than invented here.
"""

from __future__ import annotations

import pytest
from app.discovery.dht.node import DhtNode
from app.ui.viewmodels.dht_vm import DhtViewModel

OWN = bytes.fromhex("11" * 20)


class FakeTable:
    """A routing table with just the two things the panel reads."""

    def __init__(self, contacts: tuple[object, ...] = ()) -> None:
        self._contacts = contacts
        self.buckets = 1

    def contacts(self) -> tuple[object, ...]:
        return self._contacts


class FakeNode:
    """A bound DHT node, stripped to what the view model touches."""

    def __init__(
        self,
        *,
        node_id: bytes = OWN,
        port: int = 6881,
        size: int = 0,
        peers_known: int = 0,
        buckets: int = 1,
        contacts: tuple[object, ...] = (),
    ) -> None:
        self.node_id = node_id
        self.hex_id = node_id.hex()
        self.address = ("0.0.0.0", port)
        self.size = size
        self.peers_known = peers_known
        self.buckets = buckets
        self.table = FakeTable(contacts)
        self.bound = True


def contact(node_id: bytes, *, last_seen: float = 100.0, failures: int = 0, port: int = 6881):
    from app.discovery.dht.routing import DhtContact

    return DhtContact(
        node_id=node_id,
        host="198.51.100.7",
        port=port,
        last_seen=last_seen,
        failures=failures,
        questionable=failures >= 2,
    )


class TestNoNode:
    def test_a_disabled_dht_says_where_to_turn_it_on(self) -> None:
        vm = DhtViewModel()
        vm.update(None, enabled=False)
        assert vm.summary.enabled is False
        assert vm.summary.running is False
        assert "Settings" in vm.summary.note
        assert vm.contacts == ()

    def test_an_enabled_but_unbound_node_is_a_different_sentence(self) -> None:
        # "Off" and "on but not listening" demand different action, so they do
        # not share a message.
        vm = DhtViewModel()
        vm.update(None, enabled=True)
        assert vm.summary.enabled is True
        assert vm.summary.running is False
        assert "not running" in vm.summary.note

    def test_a_real_unstarted_node_reads_as_unbound(self) -> None:
        vm = DhtViewModel()
        node = DhtNode(host="127.0.0.1", port=0)
        assert node.bound is False
        vm.update(node, enabled=True)
        assert vm.summary.running is False


class TestWithNode:
    def test_the_panel_reports_what_the_node_has(self) -> None:
        vm = DhtViewModel()
        vm.update(FakeNode(size=42, peers_known=7, buckets=5, port=51413), enabled=True)

        summary = vm.summary
        assert summary.running is True
        assert summary.contacts == 42
        assert summary.peers_known == 7
        assert summary.buckets == 5
        assert summary.port == 51413
        assert summary.node_id == OWN.hex()
        assert "51413" in summary.note

    def test_a_node_that_knows_nobody_says_so(self) -> None:
        # Zero contacts after bootstrap is a network fact, not a measurement of
        # an empty swarm, and the note has to say which.
        vm = DhtViewModel()
        vm.update(FakeNode(size=0), enabled=True)
        assert "nobody" in vm.summary.note

    def test_ages_are_read_off_the_contacts(self) -> None:
        vm = DhtViewModel()
        vm.update(FakeNode(contacts=(contact(OWN, last_seen=90.0),)), enabled=True, now=100.0)
        assert vm.contacts[0].seen_seconds == pytest.approx(10.0)

    def test_a_negative_age_is_not_shown_as_one(self) -> None:
        # Clocks: the contact's last_seen can land a hair ahead of our read.
        vm = DhtViewModel()
        vm.update(FakeNode(contacts=(contact(OWN, last_seen=100.5),)), enabled=True, now=100.0)
        assert vm.contacts[0].seen_seconds == 0.0

    def test_contacts_are_ordered_closest_first(self) -> None:
        near = OWN[:-1] + bytes((OWN[-1] ^ 0x01,))
        far = bytes((OWN[0] ^ 0xFF,)) + OWN[1:]
        vm = DhtViewModel()
        vm.update(FakeNode(contacts=(contact(far), contact(near))), enabled=True)
        assert [row.hex_id for row in vm.contacts] == [near.hex(), far.hex()]

    def test_failures_and_questionable_travel_together(self) -> None:
        vm = DhtViewModel()
        vm.update(FakeNode(contacts=(contact(OWN, failures=3),)), enabled=True)
        assert vm.contacts[0].failures == 3
        assert vm.contacts[0].questionable is True

    def test_the_address_is_the_one_we_would_dial(self) -> None:
        vm = DhtViewModel()
        vm.update(FakeNode(contacts=(contact(OWN, port=51413),)), enabled=True)
        assert vm.contacts[0].address == "198.51.100.7:51413"


class TestChangeDetection:
    def test_reading_a_node_for_the_first_time_is_a_change(self) -> None:
        vm = DhtViewModel()
        assert vm.update(FakeNode(size=3), enabled=True) is True

    def test_a_disabled_dht_reported_twice_is_not_a_change(self) -> None:
        # The panel starts in this state, so repeating it must not cause a
        # repaint: an idle client polls this page forever.
        vm = DhtViewModel()
        vm.update(None, enabled=False)
        assert vm.update(None, enabled=False) is False

    def test_an_unchanged_node_is_not_a_change(self) -> None:
        # The window repaints on `changed`; a panel that repainted on every
        # poll would burn the GUI thread on a table that did not move.
        vm = DhtViewModel()
        node = FakeNode(size=3)
        assert vm.update(node, enabled=True) is True
        assert vm.update(node, enabled=True) is False

    def test_a_new_contact_is_a_change(self) -> None:
        vm = DhtViewModel()
        vm.update(FakeNode(contacts=()), enabled=True)
        assert vm.update(FakeNode(contacts=(contact(OWN),)), enabled=True) is True

    def test_the_signal_fires_with_the_view_model(self) -> None:
        vm = DhtViewModel()
        seen: list[object] = []
        vm.changed.connect(seen.append)
        vm.update(None, enabled=True)
        assert seen == [vm]


class TestPublished:
    """The other direction: are *we* in the DHT, or only reading it?"""

    def test_nothing_is_published_by_default(self) -> None:
        vm = DhtViewModel()
        vm.update(FakeNode(size=8), enabled=True)
        assert vm.summary.published == 0

    def test_the_count_is_passed_in_not_invented(self) -> None:
        # The view model owns no network state, so the announcer's number has to
        # be handed to it. A zero it made up would look identical to a real one.
        vm = DhtViewModel()
        vm.update(FakeNode(size=8), enabled=True, published=3)
        assert vm.summary.published == 3

    def test_a_panel_with_no_node_publishes_nothing(self) -> None:
        vm = DhtViewModel()
        vm.update(None, enabled=True, published=2)
        assert vm.summary.published == 0, "the summary is rebuilt, so a stale count cannot survive"

    def test_the_count_changes_are_seen_as_changes(self) -> None:
        vm = DhtViewModel()
        node = FakeNode(size=8)
        assert vm.update(node, enabled=True, published=0) is True
        assert vm.update(node, enabled=True, published=0) is False, "nothing moved"
        assert vm.update(node, enabled=True, published=1) is True, "we became findable"
