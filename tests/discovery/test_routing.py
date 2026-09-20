"""The routing table: the shape that decides whether lookups converge.

Kademlia's guarantee is that a lookup reaches its target in about ``log2(n)``
hops, and it only holds if the table answers "who is closest to this id?"
correctly. These tests check that answer, and the two judgement calls the table
makes: what to do with a full bucket, and what to do with a node that stops
answering.
"""

from __future__ import annotations

import os
import random

import pytest
from app.core.constants import DHT_K, DHT_NODE_ID_SIZE
from app.discovery.dht.errors import DhtError
from app.discovery.dht.krpc import NodeInfo, encode_nodes
from app.discovery.dht.routing import (
    BUCKET_REFRESH_SECONDS,
    KBucket,
    RoutingTable,
    bucket_index,
    contacts_from,
    distance,
)

OWN = bytes.fromhex("aa" * 20)


def contact(node_id: bytes | None = None, host: str = "10.0.0.1", port: int = 6881) -> object:
    from app.discovery.dht.routing import DhtContact

    return DhtContact(node_id=node_id or os.urandom(DHT_NODE_ID_SIZE), host=host, port=port)


def far_id(own: bytes) -> bytes:
    """An id in the half of the space that does *not* contain ``own``.

    Such ids land in one bucket — the one that can never be split, because
    splitting is only worth doing where our own id lives.
    """
    value = int.from_bytes(own, "big") ^ (1 << 159)
    low = int.from_bytes(os.urandom(DHT_NODE_ID_SIZE), "big") & ((1 << 159) - 1)
    return (value | low).to_bytes(DHT_NODE_ID_SIZE, "big")


def ids_near(own: bytes, count: int, *, bits: int) -> list[bytes]:
    """Node ids sharing ``bits`` leading bits with ``own``.

    Sharing the prefix is what puts them in the same bucket, so this is how the
    tests build a bucket that is full on purpose.
    """
    generated: list[bytes] = []
    while len(generated) < count:
        candidate = bytearray(os.urandom(DHT_NODE_ID_SIZE))
        value = int.from_bytes(own, "big")
        other = int.from_bytes(bytes(candidate), "big")
        mask = ((1 << DHT_NODE_ID_SIZE * 8) - 1) ^ ((1 << (DHT_NODE_ID_SIZE * 8 - bits)) - 1)
        other = (value & mask) | (other & ~mask & ((1 << DHT_NODE_ID_SIZE * 8) - 1))
        generated.append(other.to_bytes(DHT_NODE_ID_SIZE, "big"))
    return generated


class TestDistance:
    def test_an_id_is_zero_from_itself(self) -> None:
        assert distance(OWN, OWN) == 0

    def test_distance_is_symmetric(self) -> None:
        other = os.urandom(DHT_NODE_ID_SIZE)
        assert distance(OWN, other) == distance(other, OWN)

    def test_a_shared_prefix_means_a_small_distance(self) -> None:
        near = int.from_bytes(OWN, "big") + 1
        far = int.from_bytes(OWN, "big") ^ (1 << 159)
        assert distance(OWN, near.to_bytes(20, "big")) < distance(OWN, far.to_bytes(20, "big"))

    def test_a_wrong_sized_id_has_no_distance(self) -> None:
        with pytest.raises(DhtError, match="20 bytes"):
            distance(OWN, b"short")


class TestBuckets:
    def test_a_near_node_and_a_far_one_go_in_different_buckets(self) -> None:
        near = (int.from_bytes(OWN, "big") + 1).to_bytes(20, "big")
        far = (int.from_bytes(OWN, "big") ^ (1 << 159)).to_bytes(20, "big")
        assert bucket_index(OWN, near) < bucket_index(OWN, far)

    def test_a_full_bucket_stays_full(self) -> None:
        bucket = KBucket(capacity=2)
        for node_id in (os.urandom(20), os.urandom(20)):
            bucket.add(contact(node_id))  # type: ignore[arg-type]
        assert bucket.is_full
        assert bucket.size == 2

    def test_adding_a_contact_twice_does_not_duplicate_it(self) -> None:
        node_id = os.urandom(20)
        bucket = KBucket()
        bucket.add(contact(node_id))  # type: ignore[arg-type]
        bucket.add(contact(node_id))  # type: ignore[arg-type]
        assert bucket.size == 1

    def test_a_split_keeps_every_contact(self) -> None:
        bucket = KBucket(capacity=4)
        for node_id in ids_near(OWN, 4, bits=8):
            bucket.add(contact(node_id))  # type: ignore[arg-type]
        left, right = bucket.split()
        assert left.size + right.size == 4
        assert left.high < right.low, "the halves do not overlap"


class TestAdding:
    def test_a_new_contact_is_remembered(self) -> None:
        table = RoutingTable(OWN)
        assert table.add(contact()) is True  # type: ignore[arg-type]
        assert table.size == 1

    def test_we_do_not_route_to_ourselves(self) -> None:
        table = RoutingTable(OWN)
        assert table.add(contact(OWN)) is False  # type: ignore[arg-type]
        assert table.size == 0

    def test_a_full_distant_bucket_drops_the_newcomer(self) -> None:
        # Kademlia prefers the nodes it has spoken to: a bucket of healthy,
        # long-lived nodes is not improved by a stranger, and a distant bucket
        # is never split to make room — resolution near our own id is the only
        # resolution worth paying for.
        table = RoutingTable(OWN)
        far = [far_id(OWN) for _ in range(DHT_K)]
        for node_id in far:
            table.add(contact(node_id))  # type: ignore[arg-type]
        assert table.add(contact(far[0])) is True  # type: ignore[arg-type]  # already known

        assert table.add(contact(far_id(OWN))) is False
        assert table.size == DHT_K

    def test_the_bucket_covering_us_is_split_instead(self) -> None:
        table = RoutingTable(OWN)
        for node_id in ids_near(OWN, DHT_K * 2, bits=12):
            table.add(contact(node_id))  # type: ignore[arg-type]
        assert table.buckets > 1, "resolution near our own id is what pays for itself"
        assert table.size > DHT_K

    def test_a_node_that_stops_answering_can_be_replaced(self) -> None:
        table = RoutingTable(OWN)
        node_ids = [far_id(OWN) for _ in range(DHT_K)]
        for node_id in node_ids:
            table.add(contact(node_id))  # type: ignore[arg-type]
        stale = node_ids[0]
        for _ in range(3):
            table.note_failure(stale)

        newcomer = contact(far_id(OWN))  # type: ignore[arg-type]
        assert table.add(newcomer) is True
        assert table.get(stale) is None

    def test_a_success_clears_a_bad_record(self) -> None:
        table = RoutingTable(OWN)
        node_id = os.urandom(20)
        table.add(contact(node_id))  # type: ignore[arg-type]
        table.note_failure(node_id)
        table.note_failure(node_id)
        table.note_success(node_id)
        assert table.get(node_id) is not None
        assert table.get(node_id).questionable is False  # type: ignore[union-attr]

    def test_a_node_with_a_wrong_sized_id_is_refused(self) -> None:
        with pytest.raises(DhtError):
            contact(b"short")

    def test_a_node_with_an_impossible_port_is_refused(self) -> None:
        with pytest.raises(DhtError, match="port"):
            contact(port=0)  # type: ignore[arg-type]


class TestClosest:
    def test_closest_orders_by_distance_to_the_target(self) -> None:
        table = RoutingTable(OWN)
        target = os.urandom(20)
        rng = random.Random(7)
        for _ in range(60):
            table.add(contact(bytes(rng.randrange(256) for _ in range(20))))  # type: ignore[arg-type]
        closest = table.closest(target, DHT_K)
        assert len(closest) <= DHT_K
        gaps = [distance(c.node_id, target) for c in closest]
        assert gaps == sorted(gaps)

    def test_closest_prefers_nodes_that_answer(self) -> None:
        table = RoutingTable(OWN)
        target = os.urandom(20)
        good = [os.urandom(20) for _ in range(DHT_K)]
        for node_id in good:
            table.add(contact(node_id))  # type: ignore[arg-type]
        flaky = os.urandom(20)
        table.add(contact(flaky))  # type: ignore[arg-type]
        for _ in range(3):
            table.note_failure(flaky)

        assert flaky not in [c.node_id for c in table.closest(target, DHT_K)]

    def test_closest_never_returns_the_target_itself(self) -> None:
        table = RoutingTable(OWN)
        target = os.urandom(20)
        table.add(contact(target))  # type: ignore[arg-type]
        assert target not in [c.node_id for c in table.closest(target)]

    def test_closest_on_an_empty_table_is_empty(self) -> None:
        assert RoutingTable(OWN).closest(os.urandom(20)) == ()


class TestHousekeeping:
    def test_a_contact_can_be_forgotten(self) -> None:
        table = RoutingTable(OWN)
        node_id = os.urandom(20)
        table.add(contact(node_id))  # type: ignore[arg-type]
        assert table.remove(node_id) is True
        assert table.remove(node_id) is False

    def test_a_bucket_that_has_not_changed_is_stale(self) -> None:
        ticks = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])
        table = RoutingTable(OWN, now=lambda: next(ticks))
        table.add(contact())  # type: ignore[arg-type]
        assert len(table.stale_buckets(older_than=BUCKET_REFRESH_SECONDS)) == 1

    def test_contacts_from_wire_records(self) -> None:
        nodes = (NodeInfo(node_id=os.urandom(20), host="10.0.0.7", port=6881),)
        assert encode_nodes(nodes)  # the record survives the round trip
        contacts = contacts_from(nodes)
        assert contacts[0].address == ("10.0.0.7", 6881)
        assert contacts[0].node_id == nodes[0].node_id

    def test_the_table_reports_what_it_holds(self) -> None:
        table = RoutingTable(OWN)
        for _ in range(5):
            table.add(contact())  # type: ignore[arg-type]
        assert table.size == len(table) == 5
        assert len(table.contacts()) == 5
        assert "5 contacts" in repr(table)

    def test_our_own_id_must_be_the_right_size(self) -> None:
        with pytest.raises(DhtError, match="own node id"):
            RoutingTable(b"short")
