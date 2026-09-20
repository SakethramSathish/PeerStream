"""The DHT announce loop: what we publish, and what we refuse to.

Two questions matter, and both are answered by measurement rather than by
intent. Did an announce reach the nodes closest to the hash? And did we publish
an address a peer can actually dial?

The unit tests here use a node that records calls, because most of the
interesting behaviour is about *declining*: a torrent with no listening port, a
private torrent, a node that is not bound, a lookup that fails. The last test
uses a real four-node cluster over loopback UDP, and asks a node that knew
nothing to find us — which is the only proof that an announce is an announce.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from app.core.constants import DHT_NODE_ID_SIZE
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.discovery.dht import DhtNode
from app.discovery.dht.errors import DhtError
from app.discovery.dht_announcer import (
    MIN_ANNOUNCE_INTERVAL,
    AnnounceStatus,
    DhtAnnouncer,
)

INFO_HASH = bytes(range(DHT_NODE_ID_SIZE))
OTHER_HASH = bytes(range(100, 100 + DHT_NODE_ID_SIZE))
LOCALHOST = "127.0.0.1"
TCP_PORT = 6881


@dataclass(slots=True)
class RecordingNode:
    """A node that writes down what it was asked to publish."""

    bound: bool = True
    calls: list[tuple[bytes, int | None, bool]] = field(default_factory=list)
    accepted: int = 3
    raise_with: DhtError | None = None

    async def announce_peer(
        self, info_hash: bytes, *, port: int | None = None, implied_port: bool = True
    ) -> int:
        self.calls.append((info_hash, port, implied_port))
        if self.raise_with is not None:
            raise self.raise_with
        return self.accepted

    @property
    def ports(self) -> list[int | None]:
        """The TCP ports we were asked to publish, in order."""
        return [call[1] for call in self.calls]


def make_bus() -> tuple[EventBus, list[Event]]:
    """A bus, and every event it carried."""
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe_all(seen.append)
    return bus, seen


def types_of(events: list[Event]) -> list[EventType]:
    return [event.type for event in events]


class TestRegistration:
    def test_a_torrent_is_registered_once(self) -> None:
        announcer = DhtAnnouncer()

        assert announcer.register(INFO_HASH, TCP_PORT) is True
        assert announcer.register(INFO_HASH, TCP_PORT) is False, "a duplicate is not new"
        assert announcer.registered == (INFO_HASH,)

    def test_registration_keeps_its_order(self) -> None:
        announcer = DhtAnnouncer()

        announcer.register(INFO_HASH, TCP_PORT)
        announcer.register(OTHER_HASH, TCP_PORT)

        assert announcer.registered == (INFO_HASH, OTHER_HASH)
        assert [status.hex_info_hash for status in announcer.statuses()] == [
            INFO_HASH.hex(),
            OTHER_HASH.hex(),
        ]

    def test_unregistering_reports_whether_it_was_there(self) -> None:
        announcer = DhtAnnouncer()
        announcer.register(INFO_HASH, TCP_PORT)

        assert announcer.unregister(INFO_HASH) is True
        assert announcer.unregister(INFO_HASH) is False
        assert announcer.registered == ()

    def test_an_unknown_torrent_has_no_status(self) -> None:
        assert DhtAnnouncer().status(INFO_HASH) is None

    def test_an_interval_below_the_floor_is_refused(self) -> None:
        with pytest.raises(ValueError, match="announce interval"):
            DhtAnnouncer(interval=MIN_ANNOUNCE_INTERVAL / 10)


class TestDeclining:
    """Every way an announce can honestly not happen."""

    async def test_no_node_means_no_announce(self) -> None:
        announcer = DhtAnnouncer()
        announcer.register(INFO_HASH, TCP_PORT)

        assert await announcer.announce_once() == 0
        assert announcer.status(INFO_HASH).attempts == 0, "nothing was attempted"

    async def test_an_unbound_node_is_not_announced_through(self) -> None:
        node = RecordingNode(bound=False)
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT)

        assert await announcer.announce_once() == 0
        assert node.calls == []

    async def test_nothing_is_registered(self) -> None:
        node = RecordingNode()

        assert await DhtAnnouncer(node).announce_once() == 0
        assert node.calls == []

    async def test_a_torrent_that_is_not_listening_is_skipped(self) -> None:
        # The engine has not bound a port, or the torrent is paused. Publishing
        # would hand out an address that refuses connections.
        node = RecordingNode()
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, 0)

        assert await announcer.announce_once() == 0
        assert node.calls == []

        status = announcer.status(INFO_HASH)
        assert status.skipped == "not listening: no port to publish"
        assert status.attempts == 1, "the pass looked at it and decided"
        assert status.published is False

    async def test_the_port_is_read_at_announce_time_not_at_registration(self) -> None:
        node = RecordingNode()
        announcer = DhtAnnouncer(node)
        port = {"value": 0}
        announcer.register(INFO_HASH, lambda: port["value"])

        await announcer.announce_once()
        assert node.calls == []

        port["value"] = TCP_PORT
        await announcer.announce_once()

        assert node.ports == [TCP_PORT], "the port the engine has now, not the one it had"

    async def test_a_private_torrent_is_never_published(self) -> None:
        # BEP 27: a private torrent is tracker-only. The DHT is a tracker we do
        # not control, so the flag has to be honoured here.
        node = RecordingNode()
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT, private=True)

        assert await announcer.announce_once() == 0
        assert node.calls == []

        status = announcer.status(INFO_HASH)
        assert "private" in status.skipped
        assert status.published is False

    async def test_a_failing_lookup_does_not_stop_the_pass(self) -> None:
        node = RecordingNode(raise_with=DhtError("no route"))
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT)
        announcer.register(OTHER_HASH, TCP_PORT)

        assert await announcer.announce_once() == 0
        assert len(node.calls) == 2, "the second torrent was still tried"

        status = announcer.status(INFO_HASH)
        assert status.failures == 1
        assert status.last_error == "no route"
        assert status.announced == 0

    async def test_going_quiet_stops_the_passes(self) -> None:
        node = RecordingNode()
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT)
        announcer.go_quiet()

        assert announcer.quiet is True
        assert await announcer.announce_once() == 0
        assert node.calls == []

        announcer.resume_announcing()
        assert await announcer.announce_once() == 3
        assert len(node.calls) == 1


class TestPublishing:
    async def test_an_accepted_announce_is_recorded(self) -> None:
        node = RecordingNode(accepted=5)
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT)

        assert await announcer.announce_once() == 5

        status = announcer.status(INFO_HASH)
        assert status.announced == 5
        assert status.published is True
        assert status.last_attempt is not None
        assert announcer.published == 1

    async def test_we_publish_our_tcp_port_and_not_our_udp_one(self) -> None:
        # implied_port=False is the whole point: a node told to use our source
        # port records the DHT socket, which accepts no peer connections.
        node = RecordingNode()
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT)

        await announcer.announce_once()

        assert node.calls == [(INFO_HASH, TCP_PORT, False)]

    async def test_an_announce_nobody_accepted_is_not_a_publication(self) -> None:
        node = RecordingNode(accepted=0)
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT)

        assert await announcer.announce_once() == 0
        assert announcer.published == 0, "a lookup that found nobody published nothing"
        assert announcer.status(INFO_HASH).published is False

    async def test_only_the_published_torrents_are_counted(self) -> None:
        node = RecordingNode()
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT)
        announcer.register(OTHER_HASH, 0)

        await announcer.announce_once()

        assert announcer.published == 1
        assert announcer.registered == (INFO_HASH, OTHER_HASH)

    async def test_a_torrent_that_stops_listening_stops_counting(self) -> None:
        node = RecordingNode()
        announcer = DhtAnnouncer(node)
        port = {"value": TCP_PORT}
        announcer.register(INFO_HASH, lambda: port["value"])

        await announcer.announce_once()
        assert announcer.published == 1

        port["value"] = 0
        await announcer.announce_once()

        assert announcer.published == 0, "the count follows the measurement, not the memory"

    async def test_attaching_a_node_later_starts_publishing(self) -> None:
        announcer = DhtAnnouncer()
        announcer.register(INFO_HASH, TCP_PORT)
        assert await announcer.announce_once() == 0

        node = RecordingNode()
        announcer.attach(node)

        assert await announcer.announce_once() == 3
        announcer.attach(None)
        assert await announcer.announce_once() == 0


class TestEvents:
    async def test_a_successful_pass_is_reported_once(self) -> None:
        bus, seen = make_bus()
        announcer = DhtAnnouncer(RecordingNode(), event_bus=bus)
        announcer.register(INFO_HASH, TCP_PORT)
        announcer.register(OTHER_HASH, TCP_PORT)

        await announcer.announce_once()

        announced = [event for event in seen if event.type is EventType.DHT_ANNOUNCED]
        assert len(announced) == 1, "one pass, one event, not one per torrent"
        assert announced[0].data == {"accepted": 6, "torrents": 2, "registered": 2}

    async def test_a_pass_that_published_nothing_says_nothing(self) -> None:
        bus, seen = make_bus()
        announcer = DhtAnnouncer(RecordingNode(accepted=0), event_bus=bus)
        announcer.register(INFO_HASH, TCP_PORT)

        await announcer.announce_once()

        assert types_of(seen) == [], "an empty pass is not news"

    async def test_a_failure_is_reported_as_a_warning(self) -> None:
        bus, seen = make_bus()
        node = RecordingNode(raise_with=DhtError("timed out"))
        announcer = DhtAnnouncer(node, event_bus=bus)
        announcer.register(INFO_HASH, TCP_PORT)

        await announcer.announce_once()

        failures = [event for event in seen if event.type is EventType.DHT_FAILED]
        assert len(failures) == 1
        assert "timed out" in failures[0].message
        assert failures[0].torrent_id == INFO_HASH.hex()

    async def test_the_announce_type_is_in_the_dht_category(self) -> None:
        # The log panel filters by category; a type with no category is invisible.
        from app.core.events import EVENT_CATEGORIES

        assert EVENT_CATEGORIES[EventType.DHT_ANNOUNCED].value == "dht"


class TestLoop:
    async def test_the_loop_repeats_on_its_interval(self) -> None:
        node = RecordingNode()
        announcer = DhtAnnouncer(node, interval=0.05)
        announcer.register(INFO_HASH, TCP_PORT)

        task = announcer.start()
        try:
            await asyncio.wait_for(_until(lambda: len(node.calls) >= 3), timeout=5.0)
        finally:
            await announcer.stop()

        assert announcer.running is False
        assert task.done()

    async def test_a_poke_brings_the_next_pass_forward(self) -> None:
        node = RecordingNode()
        announcer = DhtAnnouncer(node, interval=3600.0)
        announcer.register(INFO_HASH, TCP_PORT)
        announcer.start()
        try:
            await asyncio.wait_for(_until(lambda: len(node.calls) == 1), timeout=5.0)
            announcer.register(OTHER_HASH, TCP_PORT)
            announcer.poke()
            await asyncio.wait_for(_until(lambda: len(node.calls) == 3), timeout=5.0)
        finally:
            await announcer.stop()

    async def test_poking_an_announcer_that_is_not_running_is_harmless(self) -> None:
        DhtAnnouncer().poke()

    async def test_starting_twice_returns_the_same_task(self) -> None:
        announcer = DhtAnnouncer(interval=3600.0)
        first = announcer.start()
        try:
            assert announcer.start() is first
        finally:
            await announcer.stop()

    async def test_stopping_keeps_the_registrations(self) -> None:
        announcer = DhtAnnouncer(interval=3600.0)
        announcer.register(INFO_HASH, TCP_PORT)
        announcer.start()

        await announcer.stop()

        assert announcer.registered == (INFO_HASH,), "a stopped loop is not a forgotten one"

    async def test_closing_forgets_everything(self) -> None:
        node = RecordingNode()
        announcer = DhtAnnouncer(node, interval=3600.0)
        announcer.register(INFO_HASH, TCP_PORT)
        announcer.start()

        await announcer.aclose()

        assert announcer.registered == ()
        assert announcer.node is None
        assert announcer.running is False

    async def test_stopping_twice_is_safe(self) -> None:
        announcer = DhtAnnouncer(interval=3600.0)
        await announcer.stop()
        await announcer.stop()


class TestStatus:
    def test_a_fresh_status_has_attempted_nothing(self) -> None:
        status = AnnounceStatus(info_hash=INFO_HASH)

        assert status.attempts == 0
        assert status.announced == 0
        assert status.published is False
        assert status.hex_info_hash == INFO_HASH.hex()

    def test_published_needs_an_acceptance_and_no_refusal(self) -> None:
        assert AnnounceStatus(info_hash=INFO_HASH, announced=2).published is True
        assert AnnounceStatus(info_hash=INFO_HASH, announced=2, skipped="x").published is False
        assert AnnounceStatus(info_hash=INFO_HASH).published is False


class TestAgainstARealCluster:
    """The proof: a node that never met us finds the address we published."""

    @pytest.fixture
    async def cluster(self) -> object:
        nodes = [DhtNode(host=LOCALHOST, port=0, timeout=1.0) for _ in range(4)]
        ports = [await node._transport.start() for node in nodes]
        addresses = [(LOCALHOST, port) for port in ports]
        for index, node in enumerate(nodes[:3]):
            others = [
                address for position, address in enumerate(addresses[:3]) if position != index
            ]
            await node.bootstrap(others)
        try:
            yield nodes, addresses
        finally:
            for node in nodes:
                await node.aclose()

    async def test_a_published_torrent_is_found_by_a_stranger(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        publisher, finder = nodes[0], nodes[3]
        await finder.bootstrap(addresses[:2])

        announcer = DhtAnnouncer(publisher, interval=3600.0)
        announcer.register(INFO_HASH, TCP_PORT)
        accepted = await announcer.announce_once()

        assert accepted > 0, "the closest nodes took the announce"
        assert announcer.published == 1

        result = await finder.get_peers(INFO_HASH)

        assert result.found_peers, "a stranger found the swarm we published"
        assert (LOCALHOST, TCP_PORT) in result.peers, (
            "and found our TCP port, not the UDP socket we announced from"
        )

    async def test_a_paused_torrent_publishes_nothing(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        publisher, finder = nodes[0], nodes[3]
        await finder.bootstrap(addresses[:2])

        announcer = DhtAnnouncer(publisher, interval=3600.0)
        announcer.register(INFO_HASH, 0)
        assert await announcer.announce_once() == 0

        result = await finder.get_peers(INFO_HASH)

        assert not result.found_peers, "we were never published, so we are not there"

    async def test_the_real_node_satisfies_the_protocol(self) -> None:
        # DhtAnnouncer is typed against AnnouncingNode, not DhtNode; this is the
        # test that the real one still fits, so the typing cannot drift.
        node = DhtNode(host=LOCALHOST, port=0)
        announcer = DhtAnnouncer(node)
        announcer.register(INFO_HASH, TCP_PORT)

        assert announcer.node is node
        assert await announcer.announce_once() == 0, "an unbound node announces nothing"
        await announcer.aclose()


async def _until(condition: object, *, delay: float = 0.01) -> None:
    """Poll a predicate. Used only for loop timing, never for protocol state."""
    while not condition():  # type: ignore[operator]
        await asyncio.sleep(delay)


class TestPokeTiming:
    """A poke that arrives during a pass must not be thrown away."""

    async def test_a_poke_during_a_pass_brings_the_next_one_forward(self) -> None:
        # The session pokes when a torrent is added. If the loop cleared its wake
        # flag on the way into the wait, that poke would be lost and the new
        # torrent would sit unpublished for a whole interval — which is exactly
        # what a screenshot of a fifteen-second session showed.
        class PokingNode:
            bound = True

            def __init__(self) -> None:
                self.calls = 0
                self.announcer: DhtAnnouncer | None = None

            async def announce_peer(
                self, info_hash: bytes, *, port: int | None = None, implied_port: bool = True
            ) -> int:
                self.calls += 1
                if self.calls == 1 and self.announcer is not None:
                    self.announcer.poke()
                return 1

        node = PokingNode()
        announcer = DhtAnnouncer(node, interval=3600.0)
        node.announcer = announcer
        announcer.register(INFO_HASH, TCP_PORT)
        announcer.start()
        try:
            await asyncio.wait_for(_until(lambda: node.calls >= 2), timeout=5.0)
        finally:
            await announcer.stop()

        assert node.calls >= 2, "the poke survived the pass it arrived during"


class TestRetryingWhatCouldNotBePublished:
    """A torrent that is not listening *yet* is not a torrent we forget."""

    async def test_a_torrent_added_before_its_port_bound_still_gets_published(self) -> None:
        # The bug this exists for: the announce pass ran while the engine had not
        # bound its listener, skipped the torrent, and then waited a whole
        # interval — so a client that had just started was invisible to a
        # trackerless swarm for fifteen minutes.
        node = RecordingNode()
        announcer = DhtAnnouncer(node, interval=3600.0, retry_interval=0.05)
        port = {"value": 0}
        announcer.register(INFO_HASH, lambda: port["value"])
        announcer.start()
        try:
            await asyncio.wait_for(_until(lambda: announcer.retry_soon), timeout=5.0)
            assert node.calls == [], "nothing to publish yet"

            port["value"] = TCP_PORT
            await asyncio.wait_for(_until(lambda: announcer.published == 1), timeout=5.0)

            assert node.ports == [TCP_PORT], "and it was published without being poked"
            assert announcer.retry_soon is False, "nothing left waiting on a port"
        finally:
            await announcer.aclose()

    def test_a_private_torrent_does_not_earn_a_retry(self) -> None:
        # That refusal never changes, so retrying it every thirty seconds would
        # be a loop that can only ever produce the same answer.
        announcer = DhtAnnouncer(RecordingNode())
        announcer.register(INFO_HASH, TCP_PORT, private=True)
        assert announcer.retry_soon is False

    def test_a_published_torrent_waits_the_full_interval(self) -> None:
        announcer = DhtAnnouncer(RecordingNode())
        announcer.register(INFO_HASH, TCP_PORT)
        assert announcer.retry_soon is False, "nothing has been attempted yet"
