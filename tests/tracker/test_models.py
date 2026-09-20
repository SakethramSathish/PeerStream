"""Unit tests for tracker protocol models (no network involved)."""

from __future__ import annotations

from urllib.parse import unquote_to_bytes

import pytest
from app.tracker import (
    AnnounceRequest,
    AnnounceResponse,
    PeerAddress,
    TrackerEvent,
    TrackerState,
    TrackerStatus,
    append_query,
    build_query_string,
)
from app.tracker.errors import UnsupportedTrackerError
from app.tracker.http_tracker import HttpTracker

INFO_HASH = bytes(range(20))
PEER_ID = bytes(range(100, 120))


class TestAnnounceRequest:
    def test_builds_a_valid_request(self) -> None:
        request = AnnounceRequest(info_hash=INFO_HASH, peer_id=PEER_ID, port=6881)
        assert request.uploaded == 0
        assert request.downloaded == 0
        assert request.left == 0
        assert request.event is None
        assert request.compact is True

    @pytest.mark.parametrize("length", [0, 19, 21])
    def test_rejects_wrong_info_hash_size(self, length: int) -> None:
        with pytest.raises(ValueError, match="info_hash must be 20 bytes"):
            AnnounceRequest(info_hash=b"x" * length, peer_id=PEER_ID, port=6881)

    @pytest.mark.parametrize("length", [0, 19, 21])
    def test_rejects_wrong_peer_id_size(self, length: int) -> None:
        with pytest.raises(ValueError, match="peer_id must be 20 bytes"):
            AnnounceRequest(info_hash=INFO_HASH, peer_id=b"x" * length, port=6881)

    @pytest.mark.parametrize("port", [0, -1, 65536])
    def test_rejects_invalid_ports(self, port: int) -> None:
        with pytest.raises(ValueError, match="outside 1-65535"):
            AnnounceRequest(info_hash=INFO_HASH, peer_id=PEER_ID, port=port)

    def test_rejects_negative_counters(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            AnnounceRequest(info_hash=INFO_HASH, peer_id=PEER_ID, port=6881, downloaded=-1)

    def test_is_seeding(self) -> None:
        assert AnnounceRequest(INFO_HASH, PEER_ID, 6881, left=0).is_seeding
        assert not AnnounceRequest(INFO_HASH, PEER_ID, 6881, left=10).is_seeding


class TestQueryEncoding:
    def test_byte_fields_survive_encoding(self) -> None:
        """Arbitrary 20-byte values are not text: they must round-trip exactly."""
        request = AnnounceRequest(info_hash=INFO_HASH, peer_id=PEER_ID, port=6881)
        query = request.to_query()

        assert unquote_to_bytes(query["info_hash"]) == INFO_HASH
        assert unquote_to_bytes(query["peer_id"]) == PEER_ID

    def test_values_are_not_double_encoded(self) -> None:
        """An encoded '%' must not become '%25' - that corrupts every announce."""
        request = AnnounceRequest(info_hash=b"\x25\x00" * 10, peer_id=PEER_ID, port=6881)
        assert "%" not in request.to_query()["info_hash"].replace("%25", "").replace("%00", "")

    def test_event_is_omitted_for_periodic_announces(self) -> None:
        assert "event" not in AnnounceRequest(INFO_HASH, PEER_ID, 6881).to_query()
        query = AnnounceRequest(INFO_HASH, PEER_ID, 6881, event=TrackerEvent.STARTED).to_query()
        assert query["event"] == "started"

    def test_compact_flag(self) -> None:
        assert AnnounceRequest(INFO_HASH, PEER_ID, 6881).to_query()["compact"] == "1"
        assert AnnounceRequest(INFO_HASH, PEER_ID, 6881, compact=False).to_query()["compact"] == "0"

    def test_optional_parameters(self) -> None:
        query = AnnounceRequest(
            INFO_HASH, PEER_ID, 6881, key="abc def", tracker_id=b"tid", num_want=7
        ).to_query()
        assert query["key"] == "abc%20def"
        assert query["trackerid"] == "tid"
        assert query["numwant"] == "7"

    def test_counters_are_included(self) -> None:
        query = AnnounceRequest(
            INFO_HASH, PEER_ID, 6881, uploaded=1, downloaded=2, left=3
        ).to_query()
        assert query["uploaded"] == "1"
        assert query["downloaded"] == "2"
        assert query["left"] == "3"
        assert query["port"] == "6881"


class TestQueryHelpers:
    def test_build_query_string_encodes_keys_not_values(self) -> None:
        assert build_query_string({"info_hash": "%FF"}) == "info_hash=%FF"

    def test_append_query_uses_a_question_mark_for_clean_urls(self) -> None:
        assert append_query("http://t/announce", {"a": "1"}) == "http://t/announce?a=1"

    def test_append_query_uses_ampersand_when_a_query_exists(self) -> None:
        """Trackers commonly carry a passkey in the URL query."""
        assert (
            append_query("http://t/announce?passkey=secret", {"a": "1"})
            == "http://t/announce?passkey=secret&a=1"
        )


class TestPeerAddress:
    def test_valid_peer(self) -> None:
        peer = PeerAddress(host="10.0.0.1", port=6881)
        assert peer.address == ("10.0.0.1", 6881)
        assert str(peer) == "10.0.0.1:6881"
        assert peer.source == "tracker"

    def test_is_hashable_and_comparable(self) -> None:
        first = PeerAddress(host="10.0.0.1", port=6881)
        same = PeerAddress(host="10.0.0.1", port=6881)
        other = PeerAddress(host="10.0.0.2", port=6881)

        assert first == same
        assert first != other
        assert len({first, same, other}) == 2

    def test_rejects_empty_host(self) -> None:
        with pytest.raises(ValueError, match="host must not be empty"):
            PeerAddress(host="", port=6881)

    @pytest.mark.parametrize("port", [0, 65536, -1])
    def test_rejects_invalid_ports(self, port: int) -> None:
        with pytest.raises(ValueError, match="outside 1-65535"):
            PeerAddress(host="10.0.0.1", port=port)

    def test_rejects_wrong_peer_id_length(self) -> None:
        with pytest.raises(ValueError, match="peer id must be 20 bytes"):
            PeerAddress(host="10.0.0.1", port=6881, peer_id=b"short")

    def test_ipv6_host_is_supported(self) -> None:
        peer = PeerAddress(host="2001:db8::1", port=6881)
        assert peer.host == "2001:db8::1"


class TestAnnounceResponse:
    def test_defaults(self) -> None:
        response = AnnounceResponse(interval=1800)
        assert response.peers == ()
        assert response.seeders == 0
        assert response.leechers == 0

    def test_counts(self) -> None:
        response = AnnounceResponse(interval=1800, complete=5, incomplete=2)
        assert response.seeders == 5
        assert response.leechers == 2


class TestTrackerStatus:
    def test_success_resets_failures(self) -> None:
        status = TrackerStatus(url="http://t/announce")
        status.record_failure("boom")
        status.record_failure("boom again")
        assert status.consecutive_failures == 2

        status.record_success(
            AnnounceResponse(interval=60, complete=3, incomplete=1),
            latency_ms=12.5,
            next_announce_at=100.0,
        )
        assert status.state is TrackerState.OK
        assert status.consecutive_failures == 0
        assert status.last_error is None
        assert status.seeders == 3
        assert status.leechers == 1
        assert status.latency_ms == 12.5
        assert status.next_announce_at == 100.0

    def test_warning_message_is_surfaced(self) -> None:
        status = TrackerStatus(url="http://t/announce")
        status.record_success(
            AnnounceResponse(interval=60, warning_message="slow down"), latency_ms=1.0
        )
        assert status.state is TrackerState.WARNING

    def test_failure_records_the_reason(self) -> None:
        status = TrackerStatus(url="http://t/announce")
        status.record_failure("connection refused")
        assert status.state is TrackerState.FAILED
        assert status.last_error == "connection refused"
        assert status.last_success_at is None


class TestTrackerInterface:
    def test_rejects_unsupported_scheme(self) -> None:
        with pytest.raises(UnsupportedTrackerError, match="cannot speak udp"):
            HttpTracker("udp://tracker.example:6969/announce")

    def test_rejects_empty_url(self) -> None:
        with pytest.raises(UnsupportedTrackerError):
            HttpTracker("")

    def test_exposes_url_parts(self) -> None:
        tracker = HttpTracker("https://tracker.example:8080/announce")
        assert tracker.scheme == "https"
        assert tracker.host == "tracker.example"
        assert str(tracker) == "https://tracker.example:8080/announce"


def test_rejects_negative_num_want() -> None:
    with pytest.raises(ValueError, match="num_want must not be negative"):
        AnnounceRequest(INFO_HASH, PEER_ID, 6881, num_want=-1)


class TestSwarmReporting:
    def test_unreported_swarm_is_distinguishable_from_zero(self) -> None:
        """Debian's tracker sends peers but no counts: 0 would be a lie."""
        assert AnnounceResponse(interval=1800).swarm_reported is False
        assert AnnounceResponse(interval=1800, complete=0).swarm_reported is True
        assert AnnounceResponse(interval=1800, incomplete=0).swarm_reported is True
