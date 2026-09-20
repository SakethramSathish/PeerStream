"""Magnet links, parsed the way they arrive in the wild.

Magnet links come from browsers, from search pages, and from other people's
clipboards, which means they arrive URL-encoded, sometimes base32, sometimes
with trackers that no longer exist, and occasionally for a torrent version we
cannot download. The tests below are mostly those cases.
"""

from __future__ import annotations

import base64

import pytest
from app.torrent import MagnetUri, is_magnet, magnet_for, parse_magnet
from app.torrent.errors import (
    MagnetError,
    TorrentError,
    UnsupportedMagnetError,
)

HEX = "481b6e3617be4c88f96cb25e47c9d8272130071e"
BASE32 = base64.b32encode(bytes.fromhex(HEX)).decode().rstrip("=")
DEBIAN = (
    "magnet:?xt=urn:btih:481b6e3617be4c88f96cb25e47c9d8272130071e"
    "&dn=debian-13.6.0-amd64-netinst.iso"
    "&tr=http%3A%2F%2Fbttracker.debian.org%3A6969%2Fannounce"
    "&tr=udp%3A%2F%2Ftracker.example%3A6969"
)


class TestReading:
    def test_the_info_hash_is_the_identity(self) -> None:
        assert parse_magnet(DEBIAN).info_hash == bytes.fromhex(HEX)
        assert parse_magnet(DEBIAN).hex_info_hash == HEX

    def test_a_base32_hash_is_the_same_torrent(self) -> None:
        # Both encodings appear in the wild; they are the same 20 bytes.
        assert parse_magnet(f"magnet:?xt=urn:btih:{BASE32}").info_hash == bytes.fromhex(HEX)

    def test_the_display_name_is_decoded(self) -> None:
        assert parse_magnet(DEBIAN).display_name == "debian-13.6.0-amd64-netinst.iso"

    def test_every_tracker_is_kept_in_order(self) -> None:
        assert parse_magnet(DEBIAN).trackers == (
            "http://bttracker.debian.org:6969/announce",
            "udp://tracker.example:6969",
        )

    def test_trackers_become_tiers_of_one(self) -> None:
        # A magnet does not describe tiers, so each URL stands alone.
        assert parse_magnet(DEBIAN).tiers == (
            ("http://bttracker.debian.org:6969/announce",),
            ("udp://tracker.example:6969",),
        )

    def test_a_peer_address_is_read(self) -> None:
        link = f"magnet:?xt=urn:btih:{HEX}&x.pe=203.0.113.4%3A6881&x.pe=198.51.100.9%3A51413"
        assert parse_magnet(link).peers == (("203.0.113.4", 6881), ("198.51.100.9", 51413))

    def test_an_ipv6_peer_address_is_read(self) -> None:
        link = f"magnet:?xt=urn:btih:{HEX}&x.pe=%5B2001%3Adb8%3A%3A1%5D%3A6881"
        assert parse_magnet(link).peers == (("2001:db8::1", 6881),)

    def test_web_seeds_and_sources(self) -> None:
        link = (
            f"magnet:?xt=urn:btih:{HEX}"
            "&ws=https%3A%2F%2Fexample.org%2Fdebian.iso"
            "&xs=https%3A%2F%2Fexample.org%2Fdebian.torrent"
        )
        magnet = parse_magnet(link)
        assert magnet.web_seeds == ("https://example.org/debian.iso",)
        assert magnet.sources == ("https://example.org/debian.torrent",)

    def test_select_only_file_indices(self) -> None:
        assert parse_magnet(f"magnet:?xt=urn:btih:{HEX}&so=0,3,7").select_only == (0, 3, 7)

    def test_whitespace_around_the_link_is_ignored(self) -> None:
        assert parse_magnet(f"  {DEBIAN}\n").hex_info_hash == HEX

    def test_the_scheme_is_case_insensitive(self) -> None:
        assert parse_magnet(f"MAGNET:?xt=urn:btih:{HEX}").hex_info_hash == HEX

    def test_the_original_text_is_kept(self) -> None:
        assert parse_magnet(DEBIAN).raw == DEBIAN


class TestWhatWeRefuseToInvent:
    def test_a_link_without_a_topic_names_no_torrent(self) -> None:
        with pytest.raises(MagnetError, match="no 'xt'"):
            parse_magnet("magnet:?dn=just-a-name")

    def test_a_hash_of_the_wrong_length_is_refused(self) -> None:
        with pytest.raises(MagnetError, match="40 hex or 32 base32"):
            parse_magnet("magnet:?xt=urn:btih:deadbeef")

    def test_a_v2_only_magnet_says_so(self) -> None:
        # BEP 52 identifies a v2 torrent by a multihash. Saying "we cannot do
        # v2" here saves a DHT search that was never going to work.
        with pytest.raises(UnsupportedMagnetError, match="v2"):
            parse_magnet("magnet:?xt=urn:btmh:1220" + "ab" * 32)

    def test_a_hybrid_magnet_uses_its_v1_hash(self) -> None:
        # A hybrid torrent has both hashes: we can download it, so we do.
        link = f"magnet:?xt=urn:btmh:1220{'ab' * 32}&xt=urn:btih:{HEX}"
        assert parse_magnet(link).hex_info_hash == HEX

    def test_a_topic_that_is_not_a_bittorrent_hash(self) -> None:
        with pytest.raises(MagnetError, match="not a BitTorrent info-hash"):
            parse_magnet("magnet:?xt=urn:sha1:deadbeef")

    def test_not_a_magnet_at_all(self) -> None:
        with pytest.raises(MagnetError, match="not a magnet link"):
            parse_magnet("https://example.org/debian.torrent")

    def test_an_empty_link(self) -> None:
        with pytest.raises(MagnetError, match="empty"):
            parse_magnet("   ")

    def test_a_nameless_magnet_shows_its_hash_not_a_guess(self) -> None:
        magnet = parse_magnet(f"magnet:?xt=urn:btih:{HEX}")
        assert magnet.display_name is None
        assert magnet.name == HEX, "the info-hash is at least true"


class TestBeingForgiving:
    def test_a_malformed_tracker_is_dropped_not_fatal(self) -> None:
        link = f"magnet:?xt=urn:btih:{HEX}&tr=not-a-url&tr=http%3A%2F%2Fok.example%2Fannounce"
        assert parse_magnet(link).trackers == ("http://ok.example/announce",)

    def test_a_malformed_peer_is_dropped(self) -> None:
        link = f"magnet:?xt=urn:btih:{HEX}&x.pe=nonsense&x.pe=10.0.0.9%3A6881"
        assert parse_magnet(link).peers == (("10.0.0.9", 6881),)

    def test_a_peer_with_an_impossible_port_is_dropped(self) -> None:
        link = f"magnet:?xt=urn:btih:{HEX}&x.pe=10.0.0.9%3A0"
        assert parse_magnet(link).peers == ()

    def test_duplicate_trackers_are_kept_once(self) -> None:
        link = f"magnet:?xt=urn:btih:{HEX}&tr=http%3A%2F%2Fa.example%2Fannounce&tr=http%3A%2F%2Fa.example%2Fannounce"
        assert len(parse_magnet(link).trackers) == 1

    def test_unknown_parameters_are_not_an_error(self) -> None:
        link = f"magnet:?xt=urn:btih:{HEX}&as=https%3A%2F%2Fexample.org%2Fx&x.foo=bar"
        assert parse_magnet(link).hex_info_hash == HEX

    def test_is_magnet_is_cheap_and_never_raises(self) -> None:
        assert is_magnet("  magnet:?xt=urn:btih:anything")
        assert not is_magnet("/home/user/debian.torrent")
        assert not is_magnet("")


class TestWriting:
    def test_a_link_round_trips(self) -> None:
        magnet = parse_magnet(DEBIAN)
        again = parse_magnet(magnet.to_uri())
        assert again.info_hash == magnet.info_hash
        assert again.display_name == magnet.display_name
        assert again.trackers == magnet.trackers
        assert again.peers == magnet.peers

    def test_the_hash_is_written_as_hex(self) -> None:
        # Both encodings are legal; hex is what a person pastes back into a
        # search box expecting to find the same torrent.
        assert parse_magnet(f"magnet:?xt=urn:btih:{BASE32}").to_uri().startswith(
            f"magnet:?xt=urn:btih:{HEX}"
        )

    def test_a_built_link_is_parseable(self) -> None:
        magnet = magnet_for(bytes.fromhex(HEX), display_name="debian", trackers=("udp://t:6969",))
        assert parse_magnet(magnet.to_uri()).display_name == "debian"

    def test_trackers_can_be_added_without_losing_the_originals(self) -> None:
        magnet = parse_magnet(DEBIAN).with_trackers(["http://new.example/announce"])
        assert magnet.trackers[-1] == "http://new.example/announce"
        assert len(magnet.trackers) == 3

    def test_adding_a_tracker_we_already_have_changes_nothing(self) -> None:
        before = parse_magnet(DEBIAN).trackers
        again = parse_magnet(DEBIAN).with_trackers(list(before))
        assert again.trackers == before

    def test_the_string_form_is_the_link(self) -> None:
        magnet: MagnetUri = magnet_for(bytes.fromhex(HEX))
        assert str(magnet) == magnet.to_uri()


class TestValidation:
    def test_a_wrong_sized_hash_cannot_be_built(self) -> None:
        with pytest.raises(TorrentError, match="20 bytes"):
            magnet_for(b"too short")

    def test_the_urn_is_what_a_link_carries(self) -> None:
        assert magnet_for(bytes.fromhex(HEX)).urn == f"urn:btih:{HEX}"
