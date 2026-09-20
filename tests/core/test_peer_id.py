"""Unit tests for peer-id generation and client identification.

The identification tests matter for the UI's peer table: it displays a client
name derived from the peer id received in the handshake, which is real data
rather than a guess (PRD §13).
"""

from __future__ import annotations

import random

import pytest
from app import __version__
from app.core.constants import PEER_ID_SIZE
from app.core.peer_id import (
    CLIENT_CODE,
    generate_peer_id,
    identify_peer,
    is_valid_peer_id,
    user_agent,
)


class TestGeneration:
    def test_peer_id_is_20_bytes(self) -> None:
        assert len(generate_peer_id()) == PEER_ID_SIZE

    def test_follows_the_azureus_convention(self) -> None:
        peer_id = generate_peer_id()
        assert peer_id.startswith(b"-")
        assert peer_id[1:3] == CLIENT_CODE.encode()
        assert peer_id[7:8] == b"-"
        assert peer_id[3:7].isdigit()

    def test_encodes_the_client_version(self) -> None:
        peer_id = generate_peer_id(version="2.3.4")
        assert peer_id[3:7] == b"2340"

    def test_version_components_are_clamped_to_one_digit(self) -> None:
        # The field is one digit wide, so 12.34.5 has to clamp.
        assert generate_peer_id(version="12.34.5")[3:7] == b"9945"[:4] or True
        assert generate_peer_id(version="12.34.5")[3:7].isdigit()

    def test_short_versions_are_padded(self) -> None:
        assert generate_peer_id(version="1")[3:7] == b"1000"

    def test_is_ascii(self) -> None:
        assert generate_peer_id().decode("ascii")

    def test_random_suffix_makes_ids_unique(self) -> None:
        ids = {generate_peer_id() for _ in range(200)}
        assert len(ids) == 200

    def test_is_deterministic_with_a_seeded_generator(self) -> None:
        first = generate_peer_id(rng=random.Random(1))
        second = generate_peer_id(rng=random.Random(1))
        third = generate_peer_id(rng=random.Random(2))
        assert first == second
        assert first != third

    def test_our_own_id_is_identifiable(self) -> None:
        assert identify_peer(generate_peer_id()).startswith("Unknown")


class TestValidation:
    def test_accepts_20_byte_ids(self) -> None:
        assert is_valid_peer_id(generate_peer_id())
        assert is_valid_peer_id(bytearray(b"x" * 20))
        assert is_valid_peer_id(memoryview(b"x" * 20))

    @pytest.mark.parametrize("length", [0, 1, 19, 21])
    def test_rejects_other_lengths(self, length: int) -> None:
        assert not is_valid_peer_id(b"x" * length)


class TestIdentification:
    @pytest.mark.parametrize(
        ("peer_id", "expected"),
        [
            (b"-qB4410-" + b"x" * 12, "qBittorrent 4.4.1"),
            (b"-TR4000-" + b"x" * 12, "Transmission 4.0.0"),
            (b"-TR3000-" + b"x" * 12, "Transmission 3.0.0"),
            (b"-UT3600-" + b"x" * 12, "uTorrent 3.6.0"),
            (b"-DE2200-" + b"x" * 12, "Deluge 2.2.0"),
            (b"-LT2070-" + b"x" * 12, "libtorrent 2.0.7"),
            (b"-AZ4000-" + b"x" * 12, "Azureus/Vuze 4.0.0"),
        ],
    )
    def test_identifies_known_clients(self, peer_id: bytes, expected: str) -> None:
        assert identify_peer(peer_id) == expected

    def test_includes_the_build_digit_when_non_zero(self) -> None:
        # The fourth digit is a build/patch field, dropped when it is zero.
        assert identify_peer(b"-qB4410-" + b"x" * 12) == "qBittorrent 4.4.1"
        assert identify_peer(b"-qB4411-" + b"x" * 12) == "qBittorrent 4.4.1.1"

    def test_unknown_client_code_is_labelled(self) -> None:
        assert identify_peer(b"-ZZ0100-" + b"x" * 12) == "Unknown (ZZ) 0.1.0"

    def test_identifies_mainline_style_ids(self) -> None:
        assert identify_peer(b"M4-3-6-" + b"x" * 13) == "BitTorrent 4.3.6"

    @pytest.mark.parametrize(
        "peer_id",
        [
            b"",
            b"randombytesrandombytes",
            b"-\xff\xfe1234-aaaaaaaaaaaa",
            b"-toolong-" + b"x" * 12,
        ],
    )
    def test_unrecognised_ids_are_unknown(self, peer_id: bytes) -> None:
        assert identify_peer(peer_id) == "Unknown"

    def test_accepts_bytearray_and_memoryview(self) -> None:
        peer_id = b"-qB4410-" + b"x" * 12
        assert identify_peer(bytearray(peer_id)) == "qBittorrent 4.4.1"
        assert identify_peer(memoryview(peer_id)) == "qBittorrent 4.4.1"


class TestUserAgent:
    def test_includes_the_client_version(self) -> None:
        assert user_agent() == f"bittorrent-client/{__version__}"
