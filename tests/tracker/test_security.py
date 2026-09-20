"""Security tests: tracker responses are untrusted input.

Everything here is data a remote server could send to make us misbehave —
excessive peer counts, oversized bodies, malformed addresses, invalid ports.
The client must degrade safely rather than allocate unbounded memory or hand
garbage to the peer manager.
"""

from __future__ import annotations

import pytest
from app.core.constants import MAX_PORT
from app.tracker import AnnounceRequest, HttpTracker, PeerAddress
from app.tracker.errors import TrackerProtocolError
from app.tracker.http_tracker import (
    parse_announce_response,
    parse_compact_peers_ipv4,
    parse_peers,
)
from tools.mock_tracker import MockTracker

INFO_HASH = bytes(range(20))
PEER_ID = bytes(range(100, 120))


def request(**overrides: object) -> AnnounceRequest:
    fields = {"info_hash": INFO_HASH, "peer_id": PEER_ID, "port": 6881, "left": 1000}
    fields.update(overrides)  # type: ignore[arg-type]
    return AnnounceRequest(**fields)  # type: ignore[arg-type]


class TestRequestValidation:
    def test_peer_address_rejects_zero_port(self) -> None:
        with pytest.raises(ValueError, match="outside 1-65535"):
            PeerAddress(host="10.0.0.1", port=0)

    def test_peer_address_rejects_port_above_max(self) -> None:
        with pytest.raises(ValueError, match="outside 1-65535"):
            PeerAddress(host="10.0.0.1", port=MAX_PORT + 1)

    def test_peer_address_accepts_the_maximum_port(self) -> None:
        assert PeerAddress(host="10.0.0.1", port=MAX_PORT).port == MAX_PORT


class TestPeerLimits:
    async def test_peer_count_is_capped(self, mock_tracker: MockTracker) -> None:
        for index in range(50):
            mock_tracker.add_peer(INFO_HASH, f"10.0.2.{index}", 7000 + index)

        async with HttpTracker(mock_tracker.announce_url, max_peers=5) as tracker:
            response = await tracker.announce(request(num_want=50))

        assert len(response.peers) <= 5

    def test_parse_caps_a_huge_compact_list(self) -> None:
        """A 60 000-peer response must not become 60 000 PeerAddress objects."""
        blob = (bytes([10, 0, 0, 1]) + (6881).to_bytes(2, "big")) * 60_000
        parsed = parse_announce_response(
            b"d8:intervali1800e5:peers" + str(len(blob)).encode() + b":" + blob + b"e",
            max_peers=10,
        )
        assert len(parsed.peers) == 10

    def test_parse_rejects_a_truncated_compact_list(self) -> None:
        with pytest.raises(TrackerProtocolError):
            parse_compact_peers_ipv4(b"\x0a\x00\x00\x01\x1a\xe1\x0a\x00")


class TestResponseLimits:
    async def test_oversized_body_is_rejected(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(f"{mock_tracker.url}/big", max_response_bytes=4096) as tracker:
            with pytest.raises(TrackerProtocolError, match="exceeds"):
                await tracker.announce(request())

    async def test_html_body_is_rejected(self, mock_tracker: MockTracker) -> None:
        """A captive portal or error page must not be treated as a tracker reply."""
        async with HttpTracker(f"{mock_tracker.url}/garbage") as tracker:
            with pytest.raises(TrackerProtocolError, match="not valid bencode"):
                await tracker.announce(request())

    async def test_malformed_peers_are_rejected(self, mock_tracker: MockTracker) -> None:
        async with HttpTracker(f"{mock_tracker.url}/malformed-peers") as tracker:
            with pytest.raises(TrackerProtocolError, match="multiple of 6"):
                await tracker.announce(request())


class TestUntrustedPeerValues:
    def test_invalid_ports_are_dropped(self) -> None:
        from app.tracker.http_tracker import parse_dictionary_peers

        entries = [
            {b"ip": b"10.0.0.1", b"port": 0},
            {b"ip": b"10.0.0.2", b"port": 70000},
            {b"ip": b"10.0.0.3", b"port": 6881},
        ]
        assert [peer.host for peer in parse_dictionary_peers(entries)] == ["10.0.0.3"]

    def test_non_string_ip_is_coerced_not_crashed(self) -> None:
        from app.tracker.http_tracker import parse_dictionary_peers

        entries = [{b"ip": 42, b"port": 6881}]
        assert [peer.host for peer in parse_dictionary_peers(entries)] == ["42"]

    def test_unexpected_peers_type_is_rejected(self) -> None:
        with pytest.raises(TrackerProtocolError, match="unsupported type"):
            parse_peers({b"peers": {"nested": "dict"}})

    def test_peer_id_of_wrong_length_is_dropped(self) -> None:
        from app.tracker.http_tracker import parse_dictionary_peers

        entries = [{b"ip": b"10.0.0.1", b"port": 6881, b"peer id": b"toolong" * 10}]
        assert parse_dictionary_peers(entries)[0].peer_id is None


class TestUrlHandling:
    async def test_urls_with_an_existing_query_are_preserved(
        self, mock_tracker: MockTracker
    ) -> None:
        """Passkey trackers put a secret in the query; it must survive."""
        url = f"{mock_tracker.announce_url}?passkey=supersecret"

        async with HttpTracker(url) as tracker:
            await tracker.announce(request())

        sent = mock_tracker.last_request or {}
        assert sent.get("passkey") == "supersecret"
        assert sent.get("info_hash") is not None

    async def test_scheme_is_preserved(self, mock_tracker: MockTracker) -> None:
        tracker = HttpTracker(mock_tracker.announce_url)
        assert tracker.scheme == "http"
        await tracker.aclose()
