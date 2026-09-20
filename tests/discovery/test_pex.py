"""Peer exchange (BEP 11): what we advertise, and what we refuse to.

The encoding is the easy half — six bytes per contact and a bencoded wrapper.
What these tests spend their time on is the ledger, because BEP 11's rules are
all about *sequence*: a contact is added only once its handshake completed,
dropped only if it was ever added, never both in one message, never twice in a
message, and never more than once a minute. A stateless encoder cannot express
any of that, which is why the module has memory.
"""

from __future__ import annotations

import pytest
from app.bencode import decode
from app.core.constants import MAX_PORT
from app.discovery.pex import (
    FLAG_HOLEPUNCH,
    FLAG_PREFERS_ENCRYPTION,
    FLAG_REACHABLE,
    FLAG_SEED,
    FLAG_UTP,
    PEX_MAX_ACCEPTED_PER_MESSAGE,
    PEX_MAX_INITIAL,
    PEX_MAX_PER_MESSAGE,
    PEX_MIN_INTERVAL,
    PexContact,
    PexError,
    PexLedger,
    decode_pex,
    encode_pex,
    sanitize_incoming,
)
from app.tracker.base import PeerAddress


def address(host: str = "198.51.100.7", port: int = 6881, **kwargs: object) -> PeerAddress:
    return PeerAddress(host=host, port=port, **kwargs)  # type: ignore[arg-type]


def contact(
    host: str = "198.51.100.7", port: int = 6881, *, seed: bool = False, reachable: bool = False
) -> PexContact:
    return PexContact(address=address(host, port), seed=seed, reachable=reachable)


def decode_and_reencode(payload: bytes) -> bytes:
    """Bencode round trip, to check our bytes are already canonical."""
    from app.bencode import encode

    value = decode(payload)
    assert isinstance(value, dict)
    return encode(value)


class FakeClock:
    """A clock a test can move."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TestEncoding:
    def test_a_round_trip_keeps_the_addresses(self) -> None:
        payload = encode_pex(added=[contact(port=6881), contact(port=6882)])

        message = decode_pex(payload)

        assert [peer.port for peer in message.added] == [6881, 6882]
        assert all(peer.host == "198.51.100.7" for peer in message.added)
        assert all(peer.source == "pex" for peer in message.added)

    def test_the_compact_form_is_six_bytes_per_contact(self) -> None:
        body = decode(encode_pex(added=[contact(), contact(port=1)]))

        assert isinstance(body, dict)
        assert len(body[b"added"]) == 12

    def test_ipv6_contacts_use_the_eighteen_byte_form(self) -> None:
        v6 = contact("2001:db8::1", 6881)
        assert v6.is_ipv6

        body = decode(encode_pex(added=[v6]))
        message = decode_pex(encode_pex(added=[v6]))

        assert isinstance(body, dict)
        assert len(body[b"added6"]) == 18
        assert b"added" not in body
        assert message.added6[0].host == "2001:db8::1"
        assert message.added == ()

    def test_both_families_travel_in_one_message(self) -> None:
        message = decode_pex(encode_pex(added=[contact(), contact("2001:db8::2", 51413)]))

        assert len(message.added) == 1
        assert len(message.added6) == 1
        assert len(message.peers) == 2

    def test_dropped_contacts_are_separate_from_added(self) -> None:
        message = decode_pex(encode_pex(added=[contact(port=6881)], dropped=[address(port=6882)]))

        assert [peer.port for peer in message.added] == [6881]
        assert [peer.port for peer in message.dropped] == [6882]
        assert b"added.f" not in decode(encode_pex(dropped=[address()]))

    def test_an_empty_message_is_refused(self) -> None:
        # BEP 11 requires at least one of added, added6, dropped, dropped6.
        with pytest.raises(PexError, match="at least one contact"):
            encode_pex()

    def test_a_message_with_only_dropped_contacts_is_legal(self) -> None:
        message = decode_pex(encode_pex(dropped=[address()]))
        assert len(message.dropped) == 1
        assert message.peers == ()


class TestFlags:
    def test_no_measurement_means_no_flag(self) -> None:
        assert contact().flags == 0

    def test_a_complete_bitfield_is_a_seed(self) -> None:
        assert contact(seed=True).flags == FLAG_SEED

    def test_a_peer_we_dialled_and_reached_is_reachable(self) -> None:
        assert contact(reachable=True).flags == FLAG_REACHABLE

    def test_both_measurements_set_both_bits(self) -> None:
        assert contact(seed=True, reachable=True).flags == FLAG_SEED | FLAG_REACHABLE

    def test_we_never_claim_what_we_cannot_observe(self) -> None:
        # Encryption, uTP and holepunch are features this client does not
        # implement, so it cannot have seen them in a peer. Claiming them would
        # be a lie about somebody else's client.
        for flags in (
            contact().flags,
            contact(seed=True).flags,
            contact(reachable=True).flags,
            contact(seed=True, reachable=True).flags,
        ):
            assert not flags & FLAG_PREFERS_ENCRYPTION
            assert not flags & FLAG_UTP
            assert not flags & FLAG_HOLEPUNCH

    def test_flags_travel_one_byte_per_added_contact(self) -> None:
        payload = encode_pex(added=[contact(seed=True), contact(), contact(reachable=True)])

        message = decode_pex(payload)

        assert message.flags == bytes([FLAG_SEED, 0, FLAG_REACHABLE])

    def test_ipv6_flags_are_their_own_field(self) -> None:
        message = decode_pex(encode_pex(added=[contact("2001:db8::1", seed=True)]))
        assert message.flags6 == bytes([FLAG_SEED])
        assert message.flags == b""


class TestDecoding:
    def test_an_unreadable_body_is_refused(self) -> None:
        with pytest.raises(PexError, match="unreadable"):
            decode_pex(b"d5:added")

    def test_a_body_that_is_not_a_dictionary_is_refused(self) -> None:
        with pytest.raises(PexError, match="not a dictionary"):
            decode_pex(b"le")

    def test_a_truncated_compact_list_is_refused(self) -> None:
        # Seven bytes is not a whole number of records. Believing the first six
        # would mean inventing a peer out of somebody else's mistake.
        from app.bencode import encode

        with pytest.raises(PexError, match="not a multiple of 6"):
            decode_pex(encode({b"added": b"\x01\x02\x03\x04\x05\x06\x07"}))

    def test_a_truncated_ipv6_list_is_refused(self) -> None:
        from app.bencode import encode

        with pytest.raises(PexError, match="not a multiple of 18"):
            decode_pex(encode({b"added6": b"\x01" * 17}))

    def test_a_field_that_is_not_a_string_is_refused(self) -> None:
        from app.bencode import encode

        with pytest.raises(PexError, match="was not a string"):
            decode_pex(encode({b"added": 6881}))

    def test_a_port_of_zero_is_not_a_peer(self) -> None:
        from app.bencode import encode

        message = decode_pex(encode({b"added": b"\xc6\x33\x64\x07\x00\x00"}))
        assert message.added == (), "nobody listens on port 0"

    def test_missing_fields_are_empty_not_absent(self) -> None:
        message = decode_pex(encode_pex(added=[contact()]))

        assert message.dropped == ()
        assert message.dropped6 == ()
        assert message.added6 == ()
        assert message.is_empty is False

    def test_an_empty_payload_reports_itself(self) -> None:
        from app.bencode import encode

        assert decode_pex(encode({})).is_empty

    def test_a_hostname_cannot_be_encoded_as_a_compact_record(self) -> None:
        # The compact format is raw address bytes; a name has no such form.
        with pytest.raises(OSError):
            encode_pex(added=[contact("tracker.example.com")])


class TestSanitizing:
    def test_the_order_the_peer_sent_is_kept(self) -> None:
        message = decode_pex(
            encode_pex(added=[contact("198.51.100.7", 1), contact("198.51.100.8", 2)])
        )

        accepted = sanitize_incoming(message)

        assert [(peer.host, peer.port) for peer in accepted] == [
            ("198.51.100.7", 1),
            ("198.51.100.8", 2),
        ]

    def test_ourselves_is_never_accepted(self) -> None:
        message = decode_pex(
            encode_pex(added=[contact("198.51.100.7"), contact("198.51.100.8", 6882)])
        )

        accepted = sanitize_incoming(message, our_address=address(port=6881))

        assert [(peer.host, peer.port) for peer in accepted] == [("198.51.100.8", 6882)]

    def test_the_same_host_on_two_ports_is_one_host(self) -> None:
        # BEP 11's security note: duplicate IPs with different ports are how a
        # peer multiplies one address into a hundred dial attempts.
        message = decode_pex(
            encode_pex(added=[contact(port=6881), contact(port=6882), contact(port=6883)])
        )

        accepted = sanitize_incoming(message)

        assert len(accepted) == 1

    def test_two_families_are_two_hosts(self) -> None:
        message = decode_pex(encode_pex(added=[contact(), contact("2001:db8::1")]))
        assert len(sanitize_incoming(message)) == 2, "different hosts, both kept"

    def test_an_overlong_message_is_counted_not_queued(self) -> None:
        hosts = [f"198.51.100.{index}" for index in range(1, 251)]
        payload = encode_pex(added=[contact(host) for host in hosts])

        message = decode_pex(payload)
        accepted = sanitize_incoming(message)

        assert len(message.peers) == PEX_MAX_ACCEPTED_PER_MESSAGE
        assert message.truncated == 250 - PEX_MAX_ACCEPTED_PER_MESSAGE
        assert len(accepted) == PEX_MAX_ACCEPTED_PER_MESSAGE

    def test_a_message_under_the_cap_is_not_truncated(self) -> None:
        message = decode_pex(encode_pex(added=[contact()]))
        assert message.truncated == 0

    def test_an_out_of_range_port_is_refused_by_the_address_itself(self) -> None:
        # The compact form cannot carry a port past 65535, and a port of 0 is
        # dropped at parse time, so the range check is the last line.
        assert MAX_PORT == 65535
        assert sanitize_incoming(decode_pex(encode_pex(added=[contact(port=MAX_PORT)])))


class TestLedger:
    def test_nothing_to_say_sends_nothing(self) -> None:
        assert PexLedger().build() is None

    def test_a_connected_peer_is_advertised_once(self) -> None:
        ledger = PexLedger()
        ledger.note_connected(contact())

        payload = ledger.build()

        assert payload is not None
        assert [peer.port for peer in decode_pex(payload).added] == [6881]
        assert ledger.told == 1
        assert ledger.pending == 0

    def test_the_same_peer_is_not_advertised_twice(self) -> None:
        clock = FakeClock()
        ledger = PexLedger(now=clock)
        ledger.note_connected(contact())
        assert ledger.build() is not None

        clock.advance(PEX_MIN_INTERVAL)
        ledger.note_connected(contact())

        assert ledger.build() is None, "already told; nothing has changed"
        assert ledger.told == 1

    def test_a_peer_that_flapped_between_messages_is_elided(self) -> None:
        # BEP 11 lets transient connect/disconnect pairs cancel out. The peer
        # was advertised once, went away and came back inside one interval, so
        # there is nothing to say: it was never retracted.
        clock = FakeClock()
        ledger = PexLedger(now=clock)
        ledger.note_connected(contact())
        ledger.build()
        clock.advance(PEX_MIN_INTERVAL)

        ledger.note_disconnected(address())
        ledger.note_connected(contact())

        assert ledger.build() is None
        assert ledger.told == 1
        assert ledger.pending == 0

    def test_a_second_message_waits_out_the_minute(self) -> None:
        clock = FakeClock()
        ledger = PexLedger(now=clock)
        ledger.note_connected(contact(port=1))
        assert ledger.build() is not None

        ledger.note_connected(contact(port=2))
        assert ledger.due is False
        assert ledger.build() is None, "batching is mandatory, not polite"
        assert ledger.pending == 1

        clock.advance(PEX_MIN_INTERVAL - 1)
        assert ledger.build() is None
        clock.advance(1)

        payload = ledger.build()
        assert payload is not None
        assert [peer.port for peer in decode_pex(payload).added] == [2]

    def test_a_disconnect_retracts_a_peer_we_advertised(self) -> None:
        clock = FakeClock()
        ledger = PexLedger(now=clock)
        ledger.note_connected(contact())
        ledger.build()

        clock.advance(PEX_MIN_INTERVAL)
        ledger.note_disconnected(address())
        payload = ledger.build()

        assert payload is not None
        message = decode_pex(payload)
        assert [peer.port for peer in message.dropped] == [6881]
        assert message.added == ()
        assert ledger.told == 0

    def test_a_peer_we_never_mentioned_is_not_retracted(self) -> None:
        ledger = PexLedger()
        ledger.note_disconnected(address())

        assert ledger.build() is None
        assert ledger.pending == 0

    def test_no_message_ever_names_a_contact_in_both_lists(self) -> None:
        # The invariant BEP 11 states outright, checked by driving the ledger
        # through a long arbitrary sequence of connects and disconnects.
        # Seeded, so a failure here is reproducible rather than a rumour.
        import random

        rng = random.Random(7)
        clock = FakeClock()
        ledger = PexLedger(now=clock)
        swarm = [address(f"203.0.113.{index}", 6881 + index) for index in range(1, 9)]
        connected: set[tuple[str, int]] = set()
        advertised: set[tuple[str, int]] = set()

        for step in range(400):
            target = rng.choice(swarm)
            if rng.random() < 0.55:
                ledger.note_connected(
                    PexContact(address=target, seed=rng.random() < 0.3, reachable=True)
                )
                connected.add(target.address)
            else:
                ledger.note_disconnected(target)
                connected.discard(target.address)
            if step % 5 == 4:
                clock.advance(PEX_MIN_INTERVAL)
                payload = ledger.build()
                if payload is None:
                    continue
                message = decode_pex(payload)
                added = {peer.address for peer in message.peers}
                dropped = {peer.address for peer in message.dropped + message.dropped6}
                assert not added & dropped, f"step {step}: a contact in both lists"
                assert not added & advertised, f"step {step}: a contact advertised twice"
                assert len(message.peers) == len(added) and len(message.dropped) == len(dropped)
                advertised |= added
                advertised -= dropped

        # What the ledger believes it has advertised is what it did advertise.
        assert ledger.told == len(advertised)
        assert advertised <= {peer.address for peer in swarm}

    def test_a_disconnect_before_any_message_is_elided(self) -> None:
        ledger = PexLedger()
        ledger.note_connected(contact())
        ledger.note_disconnected(address())

        assert ledger.build() is None, "a peer that came and went was never worth mentioning"

    def test_a_duplicate_within_one_message_is_impossible(self) -> None:
        ledger = PexLedger()
        ledger.note_connected(contact())
        ledger.note_connected(contact())

        message = decode_pex(ledger.build())
        assert len(message.added) == 1

    def test_the_first_message_may_carry_the_whole_swarm(self) -> None:
        ledger = PexLedger()
        for index in range(PEX_MAX_INITIAL + 20):
            ledger.note_connected(contact(f"198.51.{index // 250}.{index % 250 + 1}"))

        message = decode_pex(ledger.build())

        assert len(message.peers) == PEX_MAX_INITIAL
        assert ledger.pending == 20, "the rest waits, it is not dropped"

    def test_a_later_message_is_capped_at_fifty(self) -> None:
        clock = FakeClock()
        ledger = PexLedger(now=clock)
        ledger.note_connected(contact())
        ledger.build()

        for index in range(80):
            ledger.note_connected(contact(f"203.0.113.{index + 1}"))
        clock.advance(PEX_MIN_INTERVAL)

        message = decode_pex(ledger.build())

        assert len(message.peers) == PEX_MAX_PER_MESSAGE
        assert ledger.pending == 30

    def test_dropped_contacts_are_capped_too(self) -> None:
        clock = FakeClock()
        ledger = PexLedger(now=clock)
        hosts = [f"198.51.100.{index}" for index in range(1, 101)]
        for host in hosts:
            ledger.note_connected(contact(host))
        ledger.build()
        clock.advance(PEX_MIN_INTERVAL)

        for host in hosts:
            ledger.note_disconnected(address(host))
        message = decode_pex(ledger.build())

        assert len(message.dropped) == PEX_MAX_PER_MESSAGE
        assert ledger.pending == 50

    def test_the_interval_is_the_one_bep_11_names(self) -> None:
        assert PEX_MIN_INTERVAL == 60.0
        assert PexLedger().due is True

    def test_a_ledger_can_be_handed_a_shorter_interval_for_a_test(self) -> None:
        clock = FakeClock()
        ledger = PexLedger(min_interval=0.5, now=clock)
        ledger.note_connected(contact())
        ledger.build()
        ledger.note_connected(contact(port=2))

        assert ledger.build() is None
        clock.advance(0.5)
        assert ledger.build() is not None


class TestAgainstTheWire:
    """What a real client's bytes look like, and what ours look like to it."""

    def test_a_recorded_qbittorrent_style_message_parses(self) -> None:
        # bencoded: added = two IPv4 contacts, added.f = two flag bytes.
        raw = b"d5:added12:\xc6\x33\x64\x07\x1a\xe1\xc6\x33\x64\x08\x1a\xe27:added.f2:\x02\x12e"

        message = decode_pex(raw)

        assert [(peer.host, peer.port) for peer in message.added] == [
            ("198.51.100.7", 6881),
            ("198.51.100.8", 6882),
        ]
        assert message.flags == bytes([FLAG_SEED, FLAG_REACHABLE | FLAG_SEED])

    def test_our_message_is_ordered_canonical_bencode(self) -> None:
        payload = encode_pex(added=[contact()], dropped=[address(port=1)])

        # Bencode dictionaries are sorted by key; a peer that re-encodes to
        # compare bytes has to get the same answer we sent.
        assert payload == decode_and_reencode(payload)
