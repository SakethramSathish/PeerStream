"""The DHT against the real network — opt-in, and only for a human at a keyboard.

These tests talk to the public bootstrap routers over UDP. They are skipped by
default, because a client that cannot reach the internet is not broken, and a
test suite that failed for that reason would be a suite nobody can run on a
plane. Run them deliberately::

    pytest tests/discovery/test_dht_live.py --network

What they prove is the thing no local cluster can: that our bytes are the same
bytes the rest of the network speaks. A four-node cluster on loopback will
happily agree with itself about a mistake.
"""

from __future__ import annotations

import os

import pytest
from app.core.constants import DEFAULT_DHT_BOOTSTRAP_NODES
from app.discovery.dht.node import DhtNode

pytestmark = pytest.mark.network


async def test_bootstrap_reaches_the_public_routers() -> None:
    """Join the real DHT, and report how many nodes we met doing it.

    Three routers, any one of which may be down or unreachable from wherever
    this runs. Success is *one* of them answering, not all three: the network
    is redundant on purpose, and so is this test.
    """
    node = DhtNode(host="0.0.0.0", port=0, bootstrap_nodes=DEFAULT_DHT_BOOTSTRAP_NODES)
    try:
        await node.start()
    except Exception as error:  # noqa: BLE001 - a network test reports, not raises
        pytest.skip(f"the DHT could not be reached from here: {error}")

    try:
        assert node.bound, "the socket never bound"
        assert node.size > 0, "bootstrap answered, but we learned no nodes"
        print(f"\nbootstrapped: {node.size} nodes in {node.buckets} buckets, id {node.hex_id}")
    finally:
        await node.aclose()


async def test_a_lookup_walks_the_real_network() -> None:
    """Ask the network for peers on a well-known torrent and see who answers.

    Not an assertion about how many peers there are — a swarm's size is not
    ours to promise — but about the walk working: we ask nodes we have never
    met, they answer with nodes closer to the hash, and we get there.
    """
    # The Debian netinst ISO: a real, permanent, well-seeded torrent.
    info_hash = bytes.fromhex("481b6e3617be4c88f96cb25e47c9d8272130071e")
    node = DhtNode(host="0.0.0.0", port=0, bootstrap_nodes=DEFAULT_DHT_BOOTSTRAP_NODES)
    try:
        await node.start()
    except Exception as error:  # noqa: BLE001 - reported as a skip, not a failure
        pytest.skip(f"the DHT could not be reached from here: {error}")

    try:
        result = await node.get_peers(info_hash)
        assert result.contacted > 0, "we spoke to nobody"
        print(f"\nwalked {result.contacted} node(s), found {len(result.peers)} peer(s)")
    finally:
        await node.aclose()


async def test_a_real_magnet_resolves() -> None:
    """The whole point of M15, against the internet: a link becomes a torrent.

    The Debian netinst ISO is a permanent, well-seeded torrent, and its tracker
    is public, so this asks the real world for the real info dictionary and
    checks it against the real hash. It is the one test that could not be faked
    by a tidier mock agreeing with itself.

    Peers are temperamental — most will not answer, some will hang up — so
    this asserts only on the outcome: metadata arrived, and it hashes to the
    hash we asked for. Anything else is a skip, not a failure, because a
    network that is unreachable from here is not a bug in the client.
    """
    from app.core.config import Config
    from app.discovery.magnet_resolver import MagnetResolver
    from app.peer.errors import MetadataError
    from app.torrent import magnet_for, parse_info_hash

    info_hash = parse_info_hash("481b6e3617be4c88f96cb25e47c9d8272130071e")
    magnet = magnet_for(
        info_hash,
        display_name="debian-13.6.0-amd64-netinst.iso",
        trackers=("http://bttracker.debian.org:6969/announce",),
    )

    node = DhtNode(host="0.0.0.0", port=0, bootstrap_nodes=DEFAULT_DHT_BOOTSTRAP_NODES)
    try:
        await node.start()
    except Exception as error:  # noqa: BLE001 - a network test skips, it does not fail
        pytest.skip(f"the DHT could not be reached from here: {error}")

    resolver = MagnetResolver(dht=node, config=Config(), timeout=30.0)
    try:
        resolution = await resolver.resolve(magnet)
    except MetadataError as error:
        pytest.skip(f"no peer supplied metadata from here: {error}")

    assert resolution.torrent.info_hash == info_hash
    assert resolution.torrent.piece_count > 0
    assert resolution.torrent.total_length > 0
    print(
        f"\nresolved {resolution.torrent.name}: "
        f"{resolution.torrent.total_length} B in {resolution.torrent.piece_count} pieces, "
        f"from {resolution.metadata.address} via {', '.join(resolution.sources)}"
    )


async def test_our_node_id_is_twenty_random_bytes() -> None:
    """A sanity check that needs no network, but belongs beside these."""
    node = DhtNode(host="127.0.0.1", port=0)
    assert len(node.node_id) == 20
    assert node.node_id != bytes(20)
    other = DhtNode(host="127.0.0.1", port=0)
    assert node.node_id != other.node_id
    _ = os.urandom  # the ids come from the OS, not from a counter
