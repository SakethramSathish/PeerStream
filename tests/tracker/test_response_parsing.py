"""Unit tests for tracker response parsing.

These are pure functions over recorded byte strings: no sockets, no server.
They are also where tracker-supplied data gets validated, so every malformed
shape has a case here.
"""

from __future__ import annotations

import pytest
from app.bencode import encode
from app.tracker import (
    parse_announce_response,
    parse_compact_peers_ipv4,
    parse_compact_peers_ipv6,
    parse_dictionary_peers,
    parse_peers,
    parse_scrape_response,
)
from app.tracker.errors import TrackerProtocolError

INFO_HASH = bytes(range(20))


def announce_document(**fields: object) -> bytes:
    """Encode an announce response document."""
    document = {
        key.encode() if isinstance(key, str) else key: value for key, value in fields.items()
    }
    return encode(document)


class TestCompactIPv4:
    def test_parses_ipv4_records(self) -> None:
        blob = bytes([10, 0, 0, 1, 0x1A, 0xE1])  # 10.0.0.1:6881
        peers = parse_compact_peers_ipv4(blob)
        assert [peer.address for peer in peers] == [("10.0.0.1", 6881)]

    def test_parses_multiple_records(self) -> None:
        blob = (
            bytes([10, 0, 0, 1])
            + (6881).to_bytes(2, "big")
            + bytes([192, 168, 1, 7])
            + (51413).to_bytes(2, "big")
        )
        peers = parse_compact_peers_ipv4(blob)
        assert [peer.address for peer in peers] == [("10.0.0.1", 6881), ("192.168.1.7", 51413)]

    def test_empty_list(self) -> None:
        assert parse_compact_peers_ipv4(b"") == []

    def test_skips_peers_advertising_port_zero(self) -> None:
        """A peer on port 0 is not listening; announcing to it wastes a slot."""
        blob = bytes([10, 0, 0, 1]) + (0).to_bytes(2, "big")
        assert parse_compact_peers_ipv4(blob) == []

    def test_rejects_malformed_length(self) -> None:
        with pytest.raises(TrackerProtocolError, match="not a multiple of 6"):
            parse_compact_peers_ipv4(b"\x01\x02\x03\x04\x05")

    def test_caps_the_peer_count(self) -> None:
        blob = (bytes([10, 0, 0, 1]) + (6881).to_bytes(2, "big")) * 50
        assert len(parse_compact_peers_ipv4(blob, max_peers=7)) == 7


class TestCompactIPv6:
    def test_parses_ipv6_records(self) -> None:
        address = bytes.fromhex("20010db8000000000000000000000001")
        blob = address + (6881).to_bytes(2, "big")
        peers = parse_compact_peers_ipv6(blob)
        assert [peer.address for peer in peers] == [("2001:db8::1", 6881)]

    def test_rejects_malformed_length(self) -> None:
        with pytest.raises(TrackerProtocolError, match="not a multiple of 18"):
            parse_compact_peers_ipv6(b"\x00" * 10)


class TestDictionaryPeers:
    def test_parses_legacy_entries(self) -> None:
        entries = [
            {b"ip": b"10.0.0.1", b"port": 6881, b"peer id": b"p" * 20},
            {b"ip": b"10.0.0.2", b"port": 6882},
        ]
        peers = parse_dictionary_peers(entries)
        assert [peer.address for peer in peers] == [("10.0.0.1", 6881), ("10.0.0.2", 6882)]
        assert peers[0].peer_id == b"p" * 20
        assert peers[1].peer_id is None

    def test_accepts_string_hosts(self) -> None:
        peers = parse_dictionary_peers([{b"ip": "example.org", b"port": 80}])
        assert peers[0].host == "example.org"

    def test_skips_malformed_entries(self) -> None:
        entries = [
            b"not a dict",
            {b"ip": b"10.0.0.1"},  # no port
            {b"port": 6881},  # no ip
            {b"ip": b"10.0.0.2", b"port": 0},  # invalid port
            {b"ip": b"10.0.0.3", b"port": 6883},
        ]
        peers = parse_dictionary_peers(entries)
        assert [peer.address for peer in peers] == [("10.0.0.3", 6883)]

    def test_ignores_wrong_length_peer_ids(self) -> None:
        peers = parse_dictionary_peers([{b"ip": b"10.0.0.1", b"port": 1, b"peer id": b"short"}])
        assert peers[0].peer_id is None

    def test_rejects_a_non_list(self) -> None:
        with pytest.raises(TrackerProtocolError, match="not a list"):
            parse_dictionary_peers(b"nope")


class TestParsePeers:
    def test_accepts_compact_and_ipv6_together(self) -> None:
        document = {
            b"peers": bytes([10, 0, 0, 1]) + (6881).to_bytes(2, "big"),
            b"peers6": bytes.fromhex("20010db8000000000000000000000001")
            + (6882).to_bytes(2, "big"),
        }
        peers = parse_peers(document)
        assert [peer.address for peer in peers] == [("10.0.0.1", 6881), ("2001:db8::1", 6882)]

    def test_rejects_unsupported_peers_type(self) -> None:
        with pytest.raises(TrackerProtocolError, match="unsupported type"):
            parse_peers({b"peers": 42})

    def test_rejects_unsupported_peers6_type(self) -> None:
        with pytest.raises(TrackerProtocolError, match="unsupported type"):
            parse_peers({b"peers6": [1, 2]})

    def test_missing_peer_fields_are_fine(self) -> None:
        assert parse_peers({}) == ()


class TestAnnounceResponseParsing:
    def test_parses_a_complete_response(self) -> None:
        raw = announce_document(
            interval=1800,
            **{"min interval": 900},
            complete=42,
            incomplete=7,
            peers=bytes([10, 0, 0, 1]) + (6881).to_bytes(2, "big"),
            **{"tracker id": b"abc"},
        )
        response = parse_announce_response(raw)

        assert response.interval == 1800
        assert response.min_interval == 900
        assert response.seeders == 42
        assert response.leechers == 7
        assert response.tracker_id == b"abc"
        assert [peer.address for peer in response.peers] == [("10.0.0.1", 6881)]

    def test_failure_reason_is_an_error(self) -> None:
        raw = announce_document(**{"failure reason": b"torrent not registered"})
        with pytest.raises(TrackerProtocolError, match="torrent not registered"):
            parse_announce_response(raw)

    def test_warning_message_is_captured(self) -> None:
        raw = announce_document(interval=60, **{"warning message": b"please slow down"})
        response = parse_announce_response(raw)
        assert response.warning_message == "please slow down"

    def test_missing_interval_falls_back_to_default(self) -> None:
        response = parse_announce_response(encode({b"peers": b""}), default_interval=1234)
        assert response.interval == 1234

    @pytest.mark.parametrize("interval", [0, -5])
    def test_invalid_interval_falls_back(self, interval: int) -> None:
        response = parse_announce_response(encode({b"interval": interval}), default_interval=900)
        assert response.interval == 900

    def test_non_bencode_response_is_an_error(self) -> None:
        with pytest.raises(TrackerProtocolError, match="not valid bencode"):
            parse_announce_response(b"<html>no</html>")

    def test_non_dictionary_response_is_an_error(self) -> None:
        with pytest.raises(TrackerProtocolError, match="must be a dictionary"):
            parse_announce_response(encode([1, 2, 3]))

    def test_non_integer_interval_is_an_error(self) -> None:
        with pytest.raises(TrackerProtocolError, match="not an integer"):
            parse_announce_response(encode({b"interval": b"soon"}))

    def test_bad_optional_counters_are_ignored(self) -> None:
        response = parse_announce_response(encode({b"interval": 60, b"complete": b"many"}))
        assert response.complete is None
        assert response.seeders == 0

    def test_invalid_min_interval_is_dropped(self) -> None:
        response = parse_announce_response(encode({b"interval": 60, b"min interval": 0}))
        assert response.min_interval is None


class TestScrapeParsing:
    def test_parses_scrape_results(self) -> None:
        raw = encode(
            {
                b"files": {
                    INFO_HASH: {b"complete": 10, b"incomplete": 3, b"downloaded": 99},
                }
            }
        )
        results = parse_scrape_response(raw)
        assert results[INFO_HASH].complete == 10
        assert results[INFO_HASH].incomplete == 3
        assert results[INFO_HASH].downloaded == 99

    def test_parses_torrent_name(self) -> None:
        raw = encode({b"files": {INFO_HASH: {b"complete": 1, b"name": b"ubuntu.iso"}}})
        assert parse_scrape_response(raw)[INFO_HASH].name == "ubuntu.iso"

    def test_skips_malformed_entries(self) -> None:
        raw = encode({b"files": {b"short": {b"complete": 1}, INFO_HASH: b"nope"}})
        assert parse_scrape_response(raw) == {}

    def test_failure_reason_is_an_error(self) -> None:
        with pytest.raises(TrackerProtocolError, match="refused the scrape"):
            parse_scrape_response(encode({b"failure reason": b"no"}))

    def test_missing_files_is_an_error(self) -> None:
        with pytest.raises(TrackerProtocolError, match="no 'files' dictionary"):
            parse_scrape_response(encode({b"interval": 60}))

    def test_non_dictionary_files_is_an_error(self) -> None:
        with pytest.raises(TrackerProtocolError, match="'files' is not a dictionary"):
            parse_scrape_response(encode({b"files": [1, 2]}))

    def test_non_bencode_is_an_error(self) -> None:
        with pytest.raises(TrackerProtocolError, match="not valid bencode"):
            parse_scrape_response(b"nope")


class TestMorePeerParsing:
    def test_ipv6_skips_port_zero(self) -> None:
        blob = bytes.fromhex("20010db8000000000000000000000001") + (0).to_bytes(2, "big")
        assert parse_compact_peers_ipv6(blob) == []

    def test_ipv6_respects_the_peer_cap(self) -> None:
        record = bytes.fromhex("20010db8000000000000000000000001") + (6881).to_bytes(2, "big")
        assert len(parse_compact_peers_ipv6(record * 5, max_peers=2)) == 2

    def test_dictionary_peers_skips_empty_hosts(self) -> None:
        entries = [{b"ip": b"   ", b"port": 6881}, {b"ip": b"10.0.0.1", b"port": 6881}]
        assert [peer.host for peer in parse_dictionary_peers(entries)] == ["10.0.0.1"]

    def test_dictionary_peers_respects_the_peer_cap(self) -> None:
        entries = [{b"ip": b"10.0.0.1", b"port": 6881}, {b"ip": b"10.0.0.2", b"port": 6882}]
        assert len(parse_dictionary_peers(entries, max_peers=1)) == 1


class TestMoreScrapeParsing:
    def test_non_dictionary_response_is_an_error(self) -> None:
        with pytest.raises(TrackerProtocolError, match="must be a dictionary"):
            parse_scrape_response(encode([1, 2, 3]))
