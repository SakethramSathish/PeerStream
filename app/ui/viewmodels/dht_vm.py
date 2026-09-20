"""The DHT view model: our node, and the neighbours it knows about.

Everything here is read straight off the routing table, which means everything
here is true: contacts are nodes that answered a question, "seen" is the last
time one did, and failures are counted rather than smoothed. A DHT panel that
invented numbers would be worse than an empty one, because the whole point of
looking at it is to know whether the network is actually there.

Two states are distinct and must not be blurred:

* **disabled** — the configuration says no. Nothing is listening, and the table
  is empty by choice.
* **running, but alone** — the socket is bound but bootstrap reached nobody.
  That is a network fact (UDP blocked, or three routers all down), and the
  panel says so instead of showing zeroes as if they were measurements.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

from PySide6.QtCore import QObject, Signal

from app.discovery.dht.node import DhtNode
from app.discovery.dht.routing import DhtContact

MAX_CONTACTS_SHOWN: Final[int] = 200
"""How many contacts the panel lists.

The table can hold thousands; nobody can read thousands. The routing table is
ordered by XOR distance, so this shows the neighbourhood we would actually
walk — the closest nodes first.
"""


@dataclass(frozen=True, slots=True)
class DhtContactView:
    """One node in the routing table, as the panel shows it.

    Attributes:
        hex_id: The node id in hex, which is the only name a DHT node has.
        address: ``host:port`` as we would dial it.
        seen_seconds: How long since it last answered, or ``None`` if it
            never has — a contact we were *told* about but have not met.
        failures: Consecutive questions it failed to answer.
        questionable: Whether it has failed often enough to be replaced.
    """

    hex_id: str
    address: str
    seen_seconds: float | None
    failures: int
    questionable: bool


@dataclass(frozen=True, slots=True)
class DhtSummary:
    """The state of the DHT, in one line each.

    Attributes:
        enabled: Whether the configuration asks for a DHT at all.
        running: Whether our socket is bound and answering.
        node_id: Our node id in hex, or ``""`` when we have none.
        port: The UDP port we are listening on.
        contacts: How many nodes we know.
        buckets: How many k-buckets the table is split into.
        peers_known: How many peer announcements we are holding, summed over
            every torrent — the DHT's other job.
        published: How many of *our* torrents a remote node has accepted an
            announce for. The other direction: without it we can find a
            trackerless swarm but nobody can find us.
        note: One honest sentence about the current state.
    """

    enabled: bool = False
    running: bool = False
    node_id: str = ""
    port: int = 0
    contacts: int = 0
    buckets: int = 0
    peers_known: int = 0
    published: int = 0
    note: str = "DHT is disabled. Turn it on in Settings to resolve magnet links."


class DhtViewModel(QObject):
    """The DHT behind the client, read off the node itself.

    Holds no network state of its own: it is handed a
    :class:`~app.discovery.dht.node.DhtNode` and reads it, so the panel can
    never show a number the routing table does not have.
    """

    changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._summary = DhtSummary()
        self._contacts: tuple[DhtContactView, ...] = ()

    # ----------------------------------------------------------------- reading

    @property
    def summary(self) -> DhtSummary:
        """The one-line state of the DHT."""
        return self._summary

    @property
    def contacts(self) -> tuple[DhtContactView, ...]:
        """Known nodes, closest first."""
        return self._contacts

    # ----------------------------------------------------------------- updates

    def update(
        self,
        node: DhtNode | None,
        *,
        enabled: bool,
        published: int = 0,
        now: float | None = None,
    ) -> bool:
        """Re-read the node. Returns whether anything the panel draws changed.

        Args:
            node: The running DHT node, or None when there is not one.
            enabled: Whether DHT is enabled in the configuration — the
                difference between "off" and "on but alone".
            published: How many torrents the announce loop has published, read
                off :class:`~app.discovery.dht_announcer.DhtAnnouncer`. Passed
                in rather than reached for, so this view model still owns no
                network state of its own.
            now: Clock reading for age calculations; ``time.monotonic`` by
                default, injected in tests.

        Returns:
            True when the summary or the contacts changed since the last read,
            so the window can skip repainting a panel that did not move.
        """
        stamp = time.monotonic() if now is None else now
        previous = (self._summary, self._contacts)

        if node is None or not node.bound:
            self._summary = DhtSummary(
                enabled=enabled,
                note=(
                    "DHT is disabled. Turn it on in Settings to resolve magnet links."
                    if not enabled
                    else "DHT is enabled but not running. It starts with the client."
                ),
            )
            self._contacts = ()
        else:
            self._contacts = tuple(
                DhtContactView(
                    hex_id=contact.hex_id,
                    address=f"{contact.host}:{contact.port}",
                    seen_seconds=(
                        max(stamp - contact.last_seen, 0.0) if contact.last_seen else None
                    ),
                    failures=contact.failures,
                    questionable=bool(contact.questionable),
                )
                for contact in _closest_contacts(node)
            )
            self._summary = DhtSummary(
                enabled=enabled,
                running=True,
                node_id=node.hex_id,
                port=node.address[1],
                contacts=node.size,
                buckets=node.buckets,
                peers_known=node.peers_known,
                published=published,
                note=(
                    f"Listening on UDP {node.address[1]}; "
                    f"{node.size} nodes in {node.buckets} buckets."
                    if node.size
                    else "Listening, but bootstrap reached nobody: no node has answered yet."
                ),
            )

        changed = (self._summary, self._contacts) != previous
        if changed:
            self.changed.emit(self)
        return changed


def _closest_contacts(node: DhtNode) -> list[DhtContact]:
    """The contacts the table shows: closest to us first.

    A DHT routing table is not a list, so there is no natural order for a
    table to inherit; distance from our own id is the useful one, because
    those are the nodes every lookup starts from.
    """
    contacts = sorted(
        node.table.contacts(), key=lambda contact: _xor_distance(contact.node_id, node.node_id)
    )
    return contacts[:MAX_CONTACTS_SHOWN]


def _xor_distance(left: bytes, right: bytes) -> int:
    """Kademlia distance: the two ids XORed and read as a number."""
    return int.from_bytes(left, "big") ^ int.from_bytes(right, "big")
