"""Resolving a magnet end to end, with the sources faked but the logic real.

The resolver's job is orchestration: ask three sources for peers, deduplicate
what comes back, hand the addresses to the metadata fetcher, and turn the
verified bytes into a torrent. The network is injected here, so these tests are
about the decisions — which peers win, what happens when a source is silent, and
what a torrent that turns out to be private costs us.

The metadata fetch is faked with a function that behaves like the real one:
verified bytes, or a :class:`MetadataError`. The verification itself is
exercised for real in ``tests/peer/test_metadata_exchange.py``; here the
resolver's own second check is what keeps a fake from being believed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from hashlib import sha1
from typing import Final

import pytest
from app.bencode import decode, encode
from app.core.constants import DEFAULT_PIECE_LENGTH
from app.discovery.dht.node import DhtNode
from app.discovery.magnet_resolver import MagnetResolution, MagnetResolver
from app.peer.errors import MetadataError
from app.peer.metadata_exchange import MetadataResult
from app.torrent.magnet import MagnetUri, magnet_for, parse_magnet
from app.tracker.base import PeerAddress

OUR_PEER_ID: Final[bytes] = b"-TS0100-" + b"1" * 12


def info_bytes(*, private: bool = False) -> bytes:
    """An info dictionary, optionally flagged private (BEP 27)."""
    info: dict[bytes, object] = {
        b"name": b"resolved-by-magnet",
        b"piece length": DEFAULT_PIECE_LENGTH,
        b"pieces": b"\x01" * 20,
        b"length": DEFAULT_PIECE_LENGTH,
    }
    if private:
        info[b"private"] = 1
    return encode(info)


def link(raw: bytes, *, extra: str = "", digest: str = "") -> MagnetUri:
    """A magnet whose info hash really is the hash of ``raw``.

    So the resolver's own verification passes, as it would over a real socket.
    ``digest`` overrides the hash, for the tests that want a mismatch.
    """
    return parse_magnet(f"magnet:?xt=urn:btih:{digest or sha1(raw).hexdigest()}{extra}")


def result_for(raw: bytes, *, peer: str = "203.0.113.5:6881") -> MetadataResult:
    info = decode(raw)
    assert isinstance(info, dict)
    return MetadataResult(info=info, raw=raw, address=peer, pieces=1, elapsed=0.1)


@dataclass(slots=True)
class FakeFetch:
    """Stands in for the metadata fetch: records the peers it was offered."""

    result: MetadataResult | None = None
    error: Exception | None = None
    asked: list[tuple[str, int]] = field(default_factory=list)
    dht_port: int | None = None

    async def __call__(self, addresses, info_hash, **kwargs):  # type: ignore[no-untyped-def]
        self.asked = list(addresses)
        self.dht_port = kwargs.get("dht_port")
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class FakeDht:
    """A DHT node that has already been walked: bound, with peers to hand out."""

    def __init__(self, peers: tuple[tuple[str, int], ...] = (), *, port: int = 6881) -> None:
        self.peers = peers
        self.port = port
        self.looked_up: list[bytes] = []

    @property
    def bound(self) -> bool:
        return True

    @property
    def address(self) -> tuple[str, int]:
        return ("0.0.0.0", self.port)

    async def lookup_peers(self, info_hash: bytes) -> tuple[tuple[str, int], ...]:
        self.looked_up.append(info_hash)
        return self.peers


async def tracker_peers(url: str, info_hash: bytes) -> tuple[PeerAddress, ...]:
    """A tracker that answers with one peer, so resolution has somewhere to go."""
    assert url and len(info_hash) == 20
    return (PeerAddress("203.0.113.9", 6881),)


class TestPeers:
    async def test_peers_named_in_the_link_are_tried_first(self) -> None:
        raw = info_bytes()
        magnet = link(raw, extra="&x.pe=198.51.100.7:51413")
        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch)
        resolution = await resolver.resolve(magnet)

        assert fetch.asked[0] == ("198.51.100.7", 51413)
        assert isinstance(resolution, MagnetResolution)
        assert resolution.torrent.name == "resolved-by-magnet"
        assert "x.pe" in resolution.sources

    async def test_tracker_peers_are_added_and_deduplicated(self) -> None:
        raw = info_bytes()
        magnet = link(raw, extra="&x.pe=198.51.100.7:51413&tr=http://t.example/announce")

        async def announce(url: str, info_hash: bytes) -> tuple[PeerAddress, ...]:
            return (
                PeerAddress("198.51.100.7", 51413),  # already in the link
                PeerAddress("203.0.113.9", 6881),
            )

        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch, announce=announce)
        resolution = await resolver.resolve(magnet)

        assert fetch.asked == [("198.51.100.7", 51413), ("203.0.113.9", 6881)]
        assert "tracker" in resolution.sources

    async def test_dht_peers_arrive_when_a_node_is_available(self) -> None:
        raw = info_bytes()
        magnet = link(raw)
        dht = FakeDht(peers=(("192.0.2.44", 6881),))
        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch, dht=dht)  # type: ignore[arg-type]
        resolution = await resolver.resolve(magnet)

        assert dht.looked_up == [magnet.info_hash]
        assert fetch.asked == [("192.0.2.44", 6881)]
        assert fetch.dht_port == 6881, "we tell peers where our DHT listens"
        assert resolution.sources == ("dht",)

    async def test_extra_peers_from_the_caller_are_used(self) -> None:
        raw = info_bytes()
        magnet = link(raw)
        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch)
        await resolver.resolve(magnet, peers=[PeerAddress("192.0.2.1", 7000, source="manual")])

        assert fetch.asked == [("192.0.2.1", 7000)]

    async def test_a_source_that_fails_is_reported_not_raised(self) -> None:
        raw = info_bytes()
        magnet = link(raw, extra="&tr=http://dead.example/announce")

        async def announce(url: str, info_hash: bytes) -> tuple[PeerAddress, ...]:
            raise OSError("no route to host")

        dht = FakeDht(peers=(("192.0.2.44", 6881),))
        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch, announce=announce, dht=dht)  # type: ignore[arg-type]
        resolution = await resolver.resolve(magnet)

        assert resolution.sources == ("dht",), "the dead tracker contributed nothing and cost nothing"

    async def test_finding_no_peers_at_all_is_an_error_a_user_can_read(self) -> None:
        magnet = link(info_bytes())
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=FakeFetch(result=None))
        with pytest.raises(MetadataError, match="no peers found"):
            await resolver.resolve(magnet)

    async def test_a_slow_source_costs_only_its_deadline(self) -> None:
        raw = info_bytes()
        magnet = link(raw, extra="&tr=http://slow.example/announce")

        async def announce(url: str, info_hash: bytes) -> tuple[PeerAddress, ...]:
            await asyncio.sleep(5)
            return (PeerAddress("203.0.113.9", 6881),)

        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch, announce=announce, timeout=0.2)
        with pytest.raises(MetadataError, match="no peers found"):
            await resolver.resolve(magnet)


class TestMetadata:
    async def test_the_torrent_carries_the_magnets_trackers(self) -> None:
        raw = info_bytes()
        magnet = magnet_for(
            sha1(raw).digest(),
            display_name="Example",
            trackers=("http://a.example/announce", "udp://b.example:6969/announce"),
        )
        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch, announce=tracker_peers)
        resolution = await resolver.resolve(magnet)

        urls = [tier[0] for tier in resolution.torrent.announce_list]
        assert "http://a.example/announce" in urls
        assert "udp://b.example:6969/announce" in urls
        assert resolution.torrent.info_hash == magnet.info_hash
        assert resolution.hex_info_hash == magnet.hex_info_hash
        assert resolution.sized

    async def test_metadata_that_is_not_for_this_magnet_is_refused(self) -> None:
        # The fetcher checks the hash; the resolver checks it again, because
        # its promise is that a resolution belongs to the magnet it came from.
        raw = info_bytes()
        magnet = link(raw, digest="9" * 40, extra="&x.pe=198.51.100.7:51413")
        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch)
        with pytest.raises(MetadataError, match="did not match the info hash"):
            await resolver.resolve(magnet)

    async def test_a_private_torrent_loses_its_dht_peers(self) -> None:
        # BEP 27's flag is inside the metadata, so this is the first moment we
        # can know. We cannot un-ask the DHT, but we can stop here.
        raw = info_bytes(private=True)
        magnet = link(raw)
        dht = FakeDht(peers=(("192.0.2.44", 6881),))
        fetch = FakeFetch(result=result_for(raw))
        resolver = MagnetResolver(
            peer_id=OUR_PEER_ID,
            fetch=fetch,
            dht=dht,  # type: ignore[arg-type]
            announce=tracker_peers,
        )
        resolution = await resolver.resolve(
            magnet, peers=[PeerAddress("198.51.100.7", 51413, source="manual")]
        )

        assert resolution.torrent.private is True
        assert resolution.peers == (PeerAddress("198.51.100.7", 51413, source="manual"),)

    async def test_a_peer_that_cannot_serve_metadata_fails_the_resolution(self) -> None:
        raw = info_bytes()
        magnet = link(raw, extra="&x.pe=198.51.100.7:51413")
        fetch = FakeFetch(error=MetadataError("no peer supplied metadata"))
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, fetch=fetch)
        with pytest.raises(MetadataError, match="no peer supplied metadata"):
            await resolver.resolve(magnet)


class TestWiring:
    def test_a_peer_id_is_generated_when_none_is_given(self) -> None:
        resolver = MagnetResolver()
        assert len(resolver.peer_id) == len(OUR_PEER_ID)

    def test_a_wrong_sized_peer_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="peer_id must be 20 bytes"):
            MagnetResolver(peer_id=b"short")

    def test_a_real_dht_node_is_accepted(self) -> None:
        node = DhtNode(host="127.0.0.1", port=0)
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, dht=node)
        assert resolver.dht is node

    def test_an_unbound_dht_node_is_not_consulted(self) -> None:
        node = DhtNode(host="127.0.0.1", port=0)
        resolver = MagnetResolver(peer_id=OUR_PEER_ID, dht=node)
        assert resolver.dht is not None and not resolver.dht.bound
