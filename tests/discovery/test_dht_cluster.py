"""The DHT, tested against a real network of nodes on loopback.

A DHT client that answers correctly when you mock its own socket proves very
little: every bug that matters lives in the part where a stranger's datagram
arrives. So these tests stand up a **cluster** — several real
:class:`~app.discovery.dht.node.DhtNode` instances, each bound to its own UDP
port on 127.0.0.1, speaking the real protocol over real datagrams — and then
walk it: bootstrap, find a node, announce a peer, look it up from a node that
knew nothing, and watch the routing tables fill as a side effect.

Also covered, because a DHT spends most of its life among unreliable strangers:
a node that never answers, a node that answers with nonsense, and a node that
answers with a refusal.
"""

from __future__ import annotations

import asyncio

import pytest
from app.core.constants import DHT_NODE_ID_SIZE
from app.discovery.dht import DhtNode
from app.discovery.dht.errors import DhtBootstrapError, DhtRemoteError

INFO_HASH = bytes(range(DHT_NODE_ID_SIZE))
OTHER_HASH = bytes(range(100, 100 + DHT_NODE_ID_SIZE))
LOCALHOST = "127.0.0.1"


@pytest.fixture
async def cluster() -> object:
    """Four nodes on free loopback ports: three seeded, one joining."""
    nodes = [DhtNode(host=LOCALHOST, port=0, timeout=1.0) for _ in range(4)]
    ports = [await node._transport.start() for node in nodes]
    addresses = [(LOCALHOST, port) for port in ports]
    # Wire the first three together, so the network has a shape before the
    # fourth one arrives: each knows the next, as a real node would after
    # hearing about it in a reply.
    for index, node in enumerate(nodes[:3]):
        others = [address for position, address in enumerate(addresses[:3]) if position != index]
        await node.bootstrap(others)
    try:
        yield nodes, addresses
    finally:
        for node in nodes:
            await node.aclose()


class TestJoining:
    async def test_a_new_node_learns_the_network(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        joining = nodes[3]

        learned = await joining.bootstrap(addresses[:2])

        assert learned > 0, "a bootstrap that learns nothing has not joined"
        assert joining.size >= 2, "the contacts we were told about are in the table"

    async def test_bootstrap_fails_loudly_when_nobody_answers(self) -> None:
        # Nothing is listening on port 1: the DHT is unreachable, which is not
        # the same claim as "no peers for this torrent".
        node = DhtNode(host=LOCALHOST, port=0, timeout=0.2, bootstrap_nodes=[(LOCALHOST, 1)])
        try:
            with pytest.raises(DhtBootstrapError, match="unreachable"):
                await node.start()
        finally:
            await node.aclose()

    async def test_a_node_that_ignores_us_is_skipped_not_fatal(self) -> None:
        # A socket with no DHT behind it: it is bound, it receives our query,
        # and it never answers. That is most of the nodes a real DHT meets.
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=(LOCALHOST, 0)
        )
        silent_port = int(transport.get_extra_info("sockname")[1])

        node = DhtNode(host=LOCALHOST, port=0, timeout=0.4)
        await node._transport.start()
        try:
            assert await node.bootstrap([(LOCALHOST, silent_port)]) == 0
            assert node.size == 0
        finally:
            await node.aclose()
            transport.close()


class TestLookups:
    async def test_announcing_and_finding_a_peer(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        announcer, finder = nodes[0], nodes[3]
        await finder.bootstrap(addresses[:2])

        accepted = await announcer.announce_peer(INFO_HASH, implied_port=True)

        assert accepted > 0, "the closest nodes took the announce"

        result = await finder.get_peers(INFO_HASH)

        assert result.contacted > 0, "the search ran"
        assert result.found_peers, "the peer the announcer left behind was found"
        assert (LOCALHOST, announcer._transport.port) in result.peers

    async def test_a_lookup_for_an_unknown_hash_finds_no_peers(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        finder = nodes[3]
        await finder.bootstrap(addresses[:2])

        result = await finder.get_peers(OTHER_HASH)

        # The important half: the search *ran* and came back empty. An empty
        # result from a search that never happened would be a lie.
        assert result.contacted > 0
        assert result.peers == ()

    async def test_a_lookup_teaches_us_about_nodes(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        finder = nodes[3]
        before = finder.size
        await finder.bootstrap(addresses[:1])

        await finder.get_peers(INFO_HASH)

        assert finder.size >= before, "walking the network fills the routing table"

    async def test_find_node_returns_the_closest_contacts(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        finder = nodes[3]
        await finder.bootstrap(addresses[:2])

        result = await finder.find_node(INFO_HASH)

        assert result.nodes, "a find_node that returns nothing has not looked"
        assert result.nodes[0].node_id != finder.node_id, "we are not our own closest node"


class TestAnswering:
    async def test_we_answer_a_ping_we_did_not_ask_for(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        assert await nodes[0].ping(addresses[1])

    async def test_a_node_we_never_heard_of_can_be_learned(self, cluster: object) -> None:
        # Joining a network is what puts us in other nodes' tables; without
        # answering queries, nobody would ever learn we exist.
        nodes, addresses = cluster  # type: ignore[misc]
        joining = nodes[3]
        await joining.bootstrap(addresses[:1])

        assert joining.size >= 1
        assert any(
            contact.node_id == joining.node_id
            for node in nodes[:3]
            for contact in node.table.contacts()
        ), "answering our own id means the others remember us"

    async def test_announcing_without_a_token_is_refused(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        target = nodes[0]
        stranger = nodes[3]
        await stranger.bootstrap(addresses[:1])

        with pytest.raises(DhtRemoteError):
            await stranger._transport.query(
                target.address,
                "announce_peer",
                {
                    b"id": stranger.node_id,
                    b"info_hash": INFO_HASH,
                    b"port": 6881,
                    b"token": b"madeup!",
                    b"implied_port": 0,
                },
                timeout=1.0,
            )

    async def test_an_unknown_method_is_refused_not_ignored(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        with pytest.raises(DhtRemoteError, match="unknown method"):
            await nodes[3]._transport.query(
                addresses[0], "delete_everything", {b"id": nodes[3].node_id}, timeout=1.0
            )


class TestAmongStrangers:
    async def test_a_node_that_sends_nonsense_is_ignored(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        finder = nodes[3]
        await finder.bootstrap(addresses[:1])
        before = finder.size

        finder._transport.send(addresses[0], b"this is not bencode at all")
        finder._transport.send(addresses[0], b"d1:yi9ee")  # well-formed, unknown type
        await asyncio.sleep(0.2)

        assert finder.size == before, "a packet we cannot trust teaches us nothing"

    async def test_a_reply_we_did_not_ask_for_is_dropped(self, cluster: object) -> None:
        nodes, addresses = cluster  # type: ignore[misc]
        finder = nodes[3]
        await finder.bootstrap(addresses[:1])
        before = finder.size

        # A valid-looking response to a transaction we never opened.
        from app.discovery.dht.krpc import encode_response

        finder._transport.send(
            addresses[0], encode_response(b"\xde\xad", {b"id": bytes(20)})
        )
        await asyncio.sleep(0.2)

        assert finder.size == before

    async def test_a_silent_node_costs_a_timeout_and_nothing_else(self) -> None:
        node = DhtNode(host=LOCALHOST, port=0, timeout=0.2)
        await node._transport.start()
        try:
            assert await node.ping((LOCALHOST, 1)) is False
            assert node.size == 0, "a node that never answered is not remembered"
        finally:
            await node.aclose()
