"""The Kademlia routing table: which nodes we know, and how far away they are.

Kademlia's whole trick is that *every* node can answer "who is close to this
id?", and can answer it well enough to route a query in ``log2(n)`` hops. That
answer comes from this table, so its shape is the protocol:

**Distance is XOR.** Two ids are "close" when their bitwise XOR is small. It is
not a metaphor: it is the metric every routing decision uses, and it is
symmetric, which is what lets a node learn about us from the queries we send it.

**Buckets are ranges of the id space, not counters.** Each bucket covers the
nodes whose shared-prefix-with-us length is a particular value — bucket 0 holds
the closest nodes, the last bucket holds half the internet. A table that buckets
by *count* instead of by range would answer "closest to X" wrongly, and every
lookup would still work, just slowly and badly.

**A full bucket is not a full network.** When a bucket is at capacity (k=8) and
a new node arrives, the choice is between a node we have never spoken to and one
that has been answering for an hour. Kademlia prefers the known one, so the new
candidate is dropped — unless one of the incumbents has failed, in which case it
is replaced. Only the bucket covering *our own* id is ever split, because
splitting a distant bucket learns nothing: the nodes we need to find first are
the near ones.

The table is pure: no sockets, no clocks passed in from outside (``time`` is
read through a callable so tests can age the network without sleeping).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.core.constants import DHT_K, DHT_NODE_ID_SIZE, MAX_PORT, MIN_PORT
from app.discovery.dht.errors import DhtError
from app.discovery.dht.krpc import NodeInfo

ID_BITS: Final[int] = DHT_NODE_ID_SIZE * 8  # 160
#: A bucket that has not been touched in this long is worth a ``find_node`` on
#: our own id, which is the standard way to keep the near buckets populated.
BUCKET_REFRESH_SECONDS: Final[float] = 900.0  # 15 minutes
#: How many failed queries before a contact is considered questionable — i.e.
#: eligible to be replaced by a newcomer.
FAILURES_BEFORE_QUESTIONABLE: Final[int] = 2


def distance(left: bytes, right: bytes) -> int:
    """The Kademlia distance between two node ids: their XOR, as an integer."""
    if len(left) != DHT_NODE_ID_SIZE or len(right) != DHT_NODE_ID_SIZE:
        raise DhtError(f"node ids must be {DHT_NODE_ID_SIZE} bytes for a distance to mean anything")
    return int.from_bytes(bytes(a ^ b for a, b in zip(left, right, strict=True)), "big")


def bucket_index(own_id: bytes, node_id: bytes) -> int:
    """Which bucket a node belongs to: how many bits of distance it has.

    Bucket 0 holds the nodes that share 159 prefix bits with us — the closest
    possible; the highest bucket holds those differing in the very first bit.
    """
    gap = distance(own_id, node_id)
    return max(0, gap.bit_length() - 1) if gap else 0


@dataclass(slots=True)
class DhtContact:
    """One node we know about, and what we know about its reliability.

    Attributes:
        node_id: The node's 20-byte id.
        host: IPv4 address.
        port: UDP port.
        last_seen: Monotonic time of the last successful exchange.
        failures: Consecutive failed queries. Reset by any success.
        questionable: Set when a query failed and the node has not answered
            since; a questionable contact may be replaced by a newcomer.
    """

    node_id: bytes
    host: str
    port: int
    last_seen: float = field(default_factory=time.monotonic)
    failures: int = 0
    questionable: bool = False

    def __post_init__(self) -> None:
        if len(self.node_id) != DHT_NODE_ID_SIZE:
            raise DhtError(f"node id must be {DHT_NODE_ID_SIZE} bytes, got {len(self.node_id)}")
        if not MIN_PORT <= self.port <= MAX_PORT:
            raise DhtError(f"node port {self.port} is outside {MIN_PORT}-{MAX_PORT}")

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def hex_id(self) -> str:
        return self.node_id.hex()

    def note_success(self, *, now: float | None = None) -> None:
        """It answered: it is alive, and it is not questionable any more."""
        self.last_seen = time.monotonic() if now is None else now
        self.failures = 0
        self.questionable = False

    def note_failure(self) -> None:
        """It did not answer. Enough of these and a newcomer may take its place."""
        self.failures += 1
        if self.failures >= FAILURES_BEFORE_QUESTIONABLE:
            self.questionable = True

    def __str__(self) -> str:
        return f"{self.hex_id[:12]}@{self.host}:{self.port}"


class KBucket:
    """One bucket: at most ``k`` contacts, covering a range of the id space.

    Args:
        low: Lowest id in this bucket's range, as an integer.
        high: Highest id in this bucket's range, as an integer.
        capacity: Maximum contacts (``k``).
        now: Clock, so tests can age the bucket without sleeping.
    """

    def __init__(
        self,
        low: int = 0,
        high: int = (1 << ID_BITS) - 1,
        *,
        capacity: int = DHT_K,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.low = low
        self.high = high
        self.capacity = capacity
        self._now = now
        self._contacts: dict[bytes, DhtContact] = {}
        self.last_changed = now()

    # ------------------------------------------------------------------ reading

    @property
    def contacts(self) -> tuple[DhtContact, ...]:
        """The contacts in this bucket, most recently seen first."""
        return tuple(
            sorted(self._contacts.values(), key=lambda contact: contact.last_seen, reverse=True)
        )

    @property
    def size(self) -> int:
        return len(self._contacts)

    @property
    def is_full(self) -> bool:
        return len(self._contacts) >= self.capacity

    def contains(self, node_id: bytes) -> bool:
        """Whether this id falls inside the bucket's range."""
        return self.low <= int.from_bytes(node_id, "big") <= self.high

    def get(self, node_id: bytes) -> DhtContact | None:
        return self._contacts.get(node_id)

    def questionable(self) -> DhtContact | None:
        """The least reliable contact, if the bucket has one.

        Preferring the *oldest* failure over the newest is deliberate: it is the
        contact we have the least reason to keep.
        """
        candidates = [contact for contact in self._contacts.values() if contact.questionable]
        if not candidates:
            return None
        return max(candidates, key=lambda contact: (contact.failures, -contact.last_seen))

    def split(self) -> tuple[KBucket, KBucket]:
        """Halve the bucket's range, returning the two halves.

        The contacts are redistributed by the same rule they were filed under,
        so a split never loses a node.
        """
        middle = (self.low + self.high) // 2
        left = KBucket(self.low, middle, capacity=self.capacity, now=self._now)
        right = KBucket(middle + 1, self.high, capacity=self.capacity, now=self._now)
        for contact in self._contacts.values():
            (left if left.contains(contact.node_id) else right)._contacts[contact.node_id] = contact
        return left, right

    # ------------------------------------------------------------------ writing

    def add(self, contact: DhtContact) -> bool:
        """Record a contact, returning whether it now lives here.

        A contact already in the bucket is refreshed (moved to the front and
        marked successful) rather than duplicated.
        """
        existing = self._contacts.get(contact.node_id)
        if existing is not None:
            existing.last_seen = contact.last_seen
            existing.failures = 0
            existing.questionable = False
            self.last_changed = self._now()
            return True
        self._contacts[contact.node_id] = contact
        self.last_changed = self._now()
        return True

    def remove(self, node_id: bytes) -> bool:
        return self._contacts.pop(node_id, None) is not None

    def __len__(self) -> int:
        return len(self._contacts)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"KBucket({self.size}/{self.capacity}, range={self.low:#x}..{self.high:#x})"


class RoutingTable:
    """The table itself: buckets, and the questions we ask of them.

    Args:
        own_id: Our 20-byte node id, which the buckets are arranged around.
        capacity: ``k`` — how many contacts a bucket holds.
        now: Clock, for tests that need to age the table.
    """

    def __init__(
        self,
        own_id: bytes,
        *,
        capacity: int = DHT_K,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(own_id) != DHT_NODE_ID_SIZE:
            raise DhtError(f"our own node id must be {DHT_NODE_ID_SIZE} bytes, got {len(own_id)}")
        self.own_id = own_id
        self.capacity = capacity
        self._now = now
        self._buckets: list[KBucket] = [KBucket(capacity=capacity, now=now)]

    # ------------------------------------------------------------------ reading

    @property
    def size(self) -> int:
        """How many contacts the table holds in total."""
        return sum(len(bucket) for bucket in self._buckets)

    @property
    def buckets(self) -> int:
        """How many buckets exist. One at first, more as the near ones fill."""
        return len(self._buckets)

    def __len__(self) -> int:
        return self.size

    def contacts(self) -> tuple[DhtContact, ...]:
        """Every contact, across every bucket."""
        return tuple(contact for bucket in self._buckets for contact in bucket.contacts)

    def get(self, node_id: bytes) -> DhtContact | None:
        for bucket in self._buckets:
            if (contact := bucket.get(node_id)) is not None:
                return contact
        return None

    def closest(self, target: bytes, count: int = DHT_K) -> tuple[DhtContact, ...]:
        """The ``count`` known contacts closest to ``target``.

        This is the answer to "who should I ask next?", which is the only
        question an iterative lookup asks. Ordering by XOR distance to the
        *target* — not to us — is what makes the lookup converge.
        """
        known = [contact for contact in self.contacts() if contact.node_id != target]
        healthy = [contact for contact in known if not contact.questionable]
        # A contact that has gone quiet is worse than no answer at all — but
        # only while there are enough healthy ones to fill the request.
        pool = healthy if len(healthy) >= count else known
        pool.sort(key=lambda contact: distance(contact.node_id, target))
        return tuple(pool[:count])

    def stale_buckets(self, *, older_than: float = BUCKET_REFRESH_SECONDS) -> tuple[KBucket, ...]:
        """Buckets that have not changed in a while and are worth refreshing."""
        cutoff = self._now() - older_than
        return tuple(bucket for bucket in self._buckets if bucket.last_changed < cutoff)

    # ------------------------------------------------------------------ writing

    def add(self, contact: DhtContact) -> bool:
        """Record a contact, if we have room or a reason to make room.

        Returns:
            Whether the contact is now in the table. A full bucket of healthy,
            long-lived nodes is *not* an error: the newcomer is simply not the
            most useful node to remember, and dropping it is Kademlia's
            preference for stability, not a bug.
        """
        if contact.node_id == self.own_id:
            return False  # we do not route to ourselves
        bucket = self._bucket_for(contact.node_id)
        if not bucket.is_full or bucket.get(contact.node_id) is not None:
            return bucket.add(contact)

        stale = bucket.questionable()
        if stale is not None:
            bucket.remove(stale.node_id)
            return bucket.add(contact)

        # Full of nodes that answer. Split only the bucket that covers our own
        # id: that is where resolution is worth having. Everywhere else, the
        # newcomer loses to a node we have already spoken to.
        if bucket.contains(self.own_id):
            self._split(bucket)
            return self.add(contact)
        return False

    def remove(self, node_id: bytes) -> bool:
        return any(bucket.remove(node_id) for bucket in self._buckets)

    def note_success(
        self, node_id: bytes, *, host: str | None = None, port: int | None = None
    ) -> None:
        """A node answered: refresh it, or learn it for the first time."""
        contact = self.get(node_id)
        if contact is None:
            if host is None or port is None:
                return  # we know nothing about it and were told nothing new
            contact = DhtContact(node_id=node_id, host=host, port=port, last_seen=self._now())
            self.add(contact)
            return
        contact.note_success(now=self._now())

    def note_failure(self, node_id: bytes) -> None:
        """A node did not answer. Three strikes and a newcomer may replace it."""
        contact = self.get(node_id)
        if contact is not None:
            contact.note_failure()

    # ------------------------------------------------------------------ internals

    def _bucket_for(self, node_id: bytes) -> KBucket:
        for bucket in self._buckets:
            if bucket.contains(node_id):
                return bucket
        return self._buckets[-1]

    def _split(self, bucket: KBucket) -> None:
        index = self._buckets.index(bucket)
        left, right = bucket.split()
        self._buckets[index : index + 1] = [left, right]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RoutingTable({self.size} contacts in {len(self._buckets)} buckets)"


def contacts_from(nodes: Sequence[NodeInfo]) -> tuple[DhtContact, ...]:
    """Build routing contacts from the ``NodeInfo`` records a reply carried.

    The routing table does not import the codec; the node layer uses this to
    turn what the wire said into something the table can remember.
    """
    return tuple(DhtContact(node_id=node.node_id, host=node.host, port=node.port) for node in nodes)
