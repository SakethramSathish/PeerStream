"""Tests for the UDP tracker client (BEP 15).

Two kinds of test, deliberately separated:

*The wire tests* build and read packets as bytes. They need no socket and no
event loop, because the protocol is fixed-width big-endian and everything
interesting about it — where each field sits, what a stale transaction id looks
like, what an error packet says — can be asserted on byte strings.

*The conversation tests* run the real client against :mod:`tools.mock_udp_tracker`,
a local UDP tracker that speaks the real protocol on a loopback port. Those
cover what only a conversation can: the connect-then-announce handshake,
connection-id reuse and expiry, transaction-id matching, retries, and what the
client does when the tracker answers badly or not at all.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct

import pytest
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.core.peer_id import generate_peer_id
from app.tracker import AnnounceRequest, TrackerEvent
from app.tracker.errors import (
    TrackerProtocolError,
    TrackerTimeoutError,
    UnsupportedTrackerError,
)
from app.tracker.udp_tracker import (
    ACTION_ANNOUNCE,
    ACTION_CONNECT,
    ACTION_ERROR,
    ACTION_SCRAPE,
    ANNOUNCE_REQUEST_SIZE,
    DEFAULT_MAX_RETRIES,
    MAX_SCRAPE_HASHES,
    PROTOCOL_ID,
    UdpTracker,
    _UdpEndpoint,
    build_announce_request,
    build_connect_request,
    build_scrape_request,
    endpoint_of,
    error_message,
    parse_announce_response,
    parse_connect_response,
    parse_scrape_response,
)
from tools.mock_udp_tracker import MockUdpTracker, UdpTrackerMode

INFO_HASH = bytes(range(20))
OTHER_HASH = bytes(range(100, 120))
PEER_ID = generate_peer_id(rng=random.Random(7))


def request(**overrides: object) -> AnnounceRequest:
    """A default announce request with overrides applied."""
    fields = {
        "info_hash": INFO_HASH,
        "peer_id": PEER_ID,
        "port": 6881,
        "uploaded": 0,
        "downloaded": 0,
        "left": 1000,
        "num_want": 50,
    }
    fields.update(overrides)  # type: ignore[arg-type]
    return AnnounceRequest(**fields)  # type: ignore[arg-type]


def raw_request(**overrides: object) -> AnnounceRequest:
    """A request built without the dataclass's own validation.

    The client packs the wire itself, so it has to defend the wire even against
    a request its own model would have refused — the field checks at the edge
    are the last thing between a nonsense value and a packet a tracker ignores.
    """
    fields = {
        "info_hash": INFO_HASH,
        "peer_id": PEER_ID,
        "port": 6881,
        "uploaded": 0,
        "downloaded": 0,
        "left": 1000,
        "event": None,
        "compact": True,
        "num_want": 50,
        "key": None,
        "tracker_id": None,
    }
    fields.update(overrides)  # type: ignore[arg-type]
    blank = object.__new__(AnnounceRequest)
    for name, value in fields.items():
        object.__setattr__(blank, name, value)  # the dataclass is frozen
    return blank


def compact(*peers: tuple[str, int]) -> bytes:
    """Build a compact IPv4 peer list the way a tracker would."""
    import socket

    return b"".join(socket.inet_aton(host) + struct.pack(">H", port) for host, port in peers)


@pytest.fixture
async def udp_tracker() -> MockUdpTracker:
    """A local UDP tracker on a free loopback port."""
    tracker = MockUdpTracker(port=0)
    await tracker.start()
    try:
        yield tracker
    finally:
        await tracker.stop()


# ------------------------------------------------------------------ the wire


class TestConnectPackets:
    def test_the_connect_request_is_the_magic_followed_by_action_and_id(self) -> None:
        raw = build_connect_request(0xDEADBEEF)
        assert len(raw) == 16
        protocol_id, action, transaction_id = struct.unpack(">QII", raw)
        assert protocol_id == PROTOCOL_ID == 0x41727101980
        assert action == ACTION_CONNECT == 0
        assert transaction_id == 0xDEADBEEF

    def test_the_connect_reply_carries_a_connection_id(self) -> None:
        raw = struct.pack(">IIQ", ACTION_CONNECT, 42, 0x0102_0304_0506_0708)
        assert parse_connect_response(raw, 42) == 0x0102_0304_0506_0708

    def test_a_reply_to_somebody_else_is_not_ours(self) -> None:
        raw = struct.pack(">IIQ", ACTION_CONNECT, 43, 1)
        with pytest.raises(TrackerProtocolError, match="transaction id 43, expected 42"):
            parse_connect_response(raw, 42)

    def test_a_short_reply_is_rejected(self) -> None:
        with pytest.raises(TrackerProtocolError, match="too short"):
            parse_connect_response(struct.pack(">II", ACTION_CONNECT, 42), 42)

    def test_an_error_reply_is_a_refusal_not_a_reply(self) -> None:
        raw = struct.pack(">II", ACTION_ERROR, 42) + b"Connection ID missmatch."
        with pytest.raises(TrackerProtocolError, match="Connection ID missmatch"):
            parse_connect_response(raw, 42)
        assert error_message(raw) == "Connection ID missmatch."

    def test_an_error_reply_with_no_text_still_says_something(self) -> None:
        assert error_message(struct.pack(">II", ACTION_ERROR, 1)) == "unknown error"


class TestAnnouncePackets:
    def test_the_request_is_98_bytes_in_the_order_bep_15_specifies(self) -> None:
        raw = build_announce_request(0x1122_3344_5566_7788, 7, request(), key=0xABCD)
        assert len(raw) == ANNOUNCE_REQUEST_SIZE == 98

        connection_id, action, transaction_id = struct.unpack_from(">QII", raw, 0)
        assert connection_id == 0x1122_3344_5566_7788
        assert action == ACTION_ANNOUNCE
        assert transaction_id == 7
        assert raw[16:36] == INFO_HASH
        assert raw[36:56] == PEER_ID
        downloaded, left, uploaded = struct.unpack_from(">QQQ", raw, 56)
        assert (downloaded, left, uploaded) == (0, 1000, 0)
        event, ip, key, num_want, port = struct.unpack_from(">IIIiH", raw, 80)
        assert (event, ip, key, num_want, port) == (0, 0, 0xABCD, 50, 6881)

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (None, 0),
            (TrackerEvent.COMPLETED, 1),
            (TrackerEvent.STARTED, 2),
            (TrackerEvent.STOPPED, 3),
        ],
    )
    def test_the_event_field_is_numbered_the_udp_way(
        self, event: TrackerEvent | None, expected: int
    ) -> None:
        # Not the HTTP tracker's ordering: completed is 1 here, started is 2.
        raw = build_announce_request(1, 1, request(event=event))
        (code, _ip, _key, _num_want, _port) = struct.unpack_from(">IIIiH", raw, 80)
        assert code == expected

    def test_progress_is_reported_as_bytes_not_as_a_guess(self) -> None:
        raw = build_announce_request(1, 1, request(downloaded=12_345, uploaded=678, left=9))
        downloaded, left, uploaded = struct.unpack_from(">QQQ", raw, 56)
        assert (downloaded, left, uploaded) == (12_345, 9, 678)

    def test_num_want_below_minus_one_is_clamped(self) -> None:
        # -1 is legal ("send what you like"); anything below it is nonsense, and
        # sending it would be asking a tracker to ignore the announce.
        raw = build_announce_request(1, 1, raw_request(num_want=-5))
        (_event, _ip, _key, num_want, _port) = struct.unpack_from(">IIIiH", raw, 80)
        assert num_want == -1

    def test_a_wrong_sized_info_hash_never_reaches_the_wire(self) -> None:
        with pytest.raises(TrackerProtocolError, match="info_hash must be 20 bytes"):
            build_announce_request(1, 1, raw_request(info_hash=b"too short"))

    def test_the_reply_carries_interval_leechers_seeders_then_peers(self) -> None:
        raw = struct.pack(">IIIII", ACTION_ANNOUNCE, 9, 1800, 7, 3) + compact(
            ("10.0.0.5", 6882), ("10.0.0.6", 6883)
        )
        response = parse_announce_response(raw, 9)
        assert response.interval == 1800
        assert response.leechers == 7
        assert response.seeders == 3
        assert [peer.address for peer in response.peers] == [
            ("10.0.0.5", 6882),
            ("10.0.0.6", 6883),
        ]

    def test_a_udp_reply_always_reports_the_swarm(self) -> None:
        # Unlike an HTTP tracker, which may omit the counters entirely.
        raw = struct.pack(">IIIII", ACTION_ANNOUNCE, 9, 1800, 0, 0)
        assert parse_announce_response(raw, 9).swarm_reported

    def test_an_invalid_interval_falls_back_to_the_default(self) -> None:
        raw = struct.pack(">IIIII", ACTION_ANNOUNCE, 9, 0, 1, 1)
        assert parse_announce_response(raw, 9).interval == 1800

    def test_a_truncated_reply_is_rejected(self) -> None:
        raw = struct.pack(">II", ACTION_ANNOUNCE, 9)
        with pytest.raises(TrackerProtocolError, match="too short for action 1"):
            parse_announce_response(raw, 9)

    def test_the_peer_list_is_capped(self) -> None:
        many = compact(*[(f"10.0.{index}.1", 6881) for index in range(20)])
        raw = struct.pack(">IIIII", ACTION_ANNOUNCE, 9, 1800, 0, 0) + many
        assert len(parse_announce_response(raw, 9, max_peers=5).peers) == 5

    def test_a_peer_announcing_port_zero_is_not_a_peer(self) -> None:
        raw = struct.pack(">IIIII", ACTION_ANNOUNCE, 9, 1800, 0, 0) + compact(
            ("10.0.0.5", 0), ("10.0.0.6", 6883)
        )
        assert [peer.port for peer in parse_announce_response(raw, 9).peers] == [6883]


class TestScrapePackets:
    def test_the_request_is_a_header_plus_one_hash_per_torrent(self) -> None:
        raw = build_scrape_request(0x55, 3, [INFO_HASH, OTHER_HASH])
        assert len(raw) == 16 + 40
        connection_id, action, transaction_id = struct.unpack_from(">QII", raw, 0)
        assert (connection_id, action, transaction_id) == (0x55, ACTION_SCRAPE, 3)
        assert raw[16:36] == INFO_HASH
        assert raw[36:56] == OTHER_HASH

    def test_a_scrape_needs_at_least_one_hash(self) -> None:
        with pytest.raises(TrackerProtocolError, match="at least one info hash"):
            build_scrape_request(1, 1, [])

    def test_a_scrape_fits_in_one_packet(self) -> None:
        with pytest.raises(TrackerProtocolError, match="at most 74"):
            build_scrape_request(1, 1, [INFO_HASH] * (MAX_SCRAPE_HASHES + 1))

    def test_entries_come_back_in_the_order_they_were_asked_for(self) -> None:
        body = b"".join(
            struct.pack(">III", seeders, completed, leechers)
            for seeders, completed, leechers in ((5, 300, 2), (0, 0, 0))
        )
        raw = struct.pack(">II", ACTION_SCRAPE, 4) + body
        results = parse_scrape_response(raw, 4, [INFO_HASH, OTHER_HASH])
        assert results[INFO_HASH].complete == 5
        assert results[INFO_HASH].downloaded == 300
        assert results[INFO_HASH].incomplete == 2
        assert results[OTHER_HASH].complete == 0

    def test_a_body_that_is_not_whole_entries_is_rejected(self) -> None:
        raw = struct.pack(">II", ACTION_SCRAPE, 4) + b"\x00" * 13
        with pytest.raises(TrackerProtocolError, match="not a multiple of 12"):
            parse_scrape_response(raw, 4, [INFO_HASH])

    def test_more_entries_than_were_asked_for_is_rejected(self) -> None:
        body = struct.pack(">III", 1, 2, 3) * 3
        raw = struct.pack(">II", ACTION_SCRAPE, 4) + body
        with pytest.raises(TrackerProtocolError, match="3 scrape entries for 2"):
            parse_scrape_response(raw, 4, [INFO_HASH, OTHER_HASH])


class TestUrlParsing:
    def test_a_udp_url_names_a_host_and_a_port(self) -> None:
        assert endpoint_of("udp://tracker.example:6969/announce") == ("tracker.example", 6969)

    def test_a_portless_url_falls_back_to_6969(self) -> None:
        # The port is mandatory in practice, and its absence is not a reason to
        # refuse to open the torrent.
        assert endpoint_of("udp://tracker.example/announce") == ("tracker.example", 6969)

    def test_a_url_with_no_host_is_refused(self) -> None:
        with pytest.raises(UnsupportedTrackerError, match="names no host"):
            endpoint_of("udp:///announce")

    def test_a_non_udp_url_is_not_this_clients_job(self) -> None:
        with pytest.raises(UnsupportedTrackerError):
            UdpTracker("http://tracker.example/announce")


# ---------------------------------------------------------- the conversation


class TestTheHandshake:
    async def test_an_announce_connects_first_and_then_announces(
        self, udp_tracker: MockUdpTracker
    ) -> None:
        udp_tracker.add_peer(INFO_HASH, "10.0.0.5", 6882, left=0)
        udp_tracker.add_peer(INFO_HASH, "10.0.0.6", 6883, left=500)

        async with UdpTracker(udp_tracker.url) as tracker:
            response = await tracker.announce(request())

        assert udp_tracker.connect_count == 1
        assert udp_tracker.announce_count == 1
        assert [peer.address for peer in response.peers] == [
            ("10.0.0.5", 6882),
            ("10.0.0.6", 6883),
        ]
        assert (response.seeders, response.leechers) == (1, 2), (
            "the announcing peer counts itself: it still has 1000 bytes left"
        )
        assert response.interval == udp_tracker.interval

    async def test_the_connection_id_is_reused_while_it_is_fresh(
        self, udp_tracker: MockUdpTracker
    ) -> None:
        async with UdpTracker(udp_tracker.url) as tracker:
            await tracker.announce(request())
            await tracker.announce(request())

        assert udp_tracker.connect_count == 1, "one handshake per client, not per announce"
        assert udp_tracker.announce_count == 2

    async def test_an_expired_connection_id_is_renegotiated(
        self, udp_tracker: MockUdpTracker
    ) -> None:
        async with UdpTracker(udp_tracker.url, connection_lifetime=0.0) as tracker:
            await tracker.announce(request())
            await tracker.announce(request())

        assert udp_tracker.connect_count == 2
        assert udp_tracker.issued_connection_ids[0] != udp_tracker.issued_connection_ids[1]

    async def test_what_the_tracker_receives_is_what_we_meant_to_send(
        self, udp_tracker: MockUdpTracker
    ) -> None:
        async with UdpTracker(udp_tracker.url) as tracker:
            await tracker.announce(
                request(
                    downloaded=4096,
                    uploaded=2048,
                    left=1024,
                    event=TrackerEvent.STARTED,
                    num_want=17,
                )
            )

        seen = udp_tracker.last_announce
        assert seen["info_hash"] == INFO_HASH
        assert seen["peer_id"] == PEER_ID
        assert seen["downloaded"] == 4096
        assert seen["uploaded"] == 2048
        assert seen["left"] == 1024
        assert seen["event"] == 2, "started is 2 on the UDP wire"
        assert seen["num_want"] == 17
        assert seen["port"] == 6881

    async def test_the_tracker_does_not_hand_us_back_ourselves(
        self, udp_tracker: MockUdpTracker
    ) -> None:
        async with UdpTracker(udp_tracker.url) as tracker:
            await tracker.announce(request())
            response = await tracker.announce(request())

        assert ("127.0.0.1", 6881) not in [peer.address for peer in response.peers]


class TestScraping:
    async def test_scrape_reports_counters_per_torrent(self, udp_tracker: MockUdpTracker) -> None:
        udp_tracker.add_peer(INFO_HASH, "10.0.0.5", 6882, left=0)
        udp_tracker.add_peer(OTHER_HASH, "10.0.0.6", 6883, left=10)

        async with UdpTracker(udp_tracker.url) as tracker:
            results = await tracker.scrape([INFO_HASH, OTHER_HASH])

        assert results[INFO_HASH].complete == 1
        assert results[OTHER_HASH].incomplete == 1

    async def test_a_large_scrape_is_split_into_packets(self, udp_tracker: MockUdpTracker) -> None:
        hashes = [bytes([index]) * 20 for index in range(MAX_SCRAPE_HASHES + 6)]
        async with UdpTracker(udp_tracker.url) as tracker:
            results = await tracker.scrape(hashes)

        assert udp_tracker.scrape_count == 2, "74 hashes per packet, then the rest"
        assert len(results) == len(hashes)

    async def test_scraping_nothing_asks_nothing(self, udp_tracker: MockUdpTracker) -> None:
        async with UdpTracker(udp_tracker.url) as tracker:
            assert await tracker.scrape([]) == {}
        assert udp_tracker.scrape_count == 0

    async def test_a_hash_of_the_wrong_length_is_refused(self, udp_tracker: MockUdpTracker) -> None:
        async with UdpTracker(udp_tracker.url) as tracker:
            with pytest.raises(TrackerProtocolError, match="info_hash must be 20 bytes"):
                await tracker.scrape([b"short"])


class TestWhenTheTrackerMisbehaves:
    async def test_a_silent_tracker_is_a_timed_out_tracker(self) -> None:
        silent = MockUdpTracker(port=0, mode=UdpTrackerMode.SILENT)
        await silent.start()
        try:
            async with UdpTracker(silent.url, timeout=0.05) as tracker:
                with pytest.raises(TrackerTimeoutError, match="did not answer"):
                    await tracker.announce(request())
        finally:
            await silent.stop()
        assert silent.packets_received == DEFAULT_MAX_RETRIES + 1, "every retry goes out"
        assert silent.connect_count == 0, "it never answered anything"

    async def test_a_slow_tracker_is_timed_out_and_retried(self) -> None:
        slow = MockUdpTracker(port=0, delay=0.4)
        await slow.start()
        try:
            async with UdpTracker(slow.url, timeout=0.05, max_retries=1) as tracker:
                with pytest.raises(TrackerTimeoutError):
                    await tracker.announce(request())
        finally:
            await slow.stop()

    async def test_a_tracker_that_answers_nonsense_is_not_trusted(self) -> None:
        # Garbage still has to be recognised as *not an answer*: the client must
        # wait out its timeout rather than parse the bytes it was sent.
        noisy = MockUdpTracker(port=0, mode=UdpTrackerMode.GARBAGE)
        await noisy.start()
        try:
            async with UdpTracker(noisy.url, timeout=0.05, max_retries=0) as tracker:
                with pytest.raises(TrackerTimeoutError):
                    await tracker.announce(request())
        finally:
            await noisy.stop()

    async def test_a_truncated_announce_reply_is_a_protocol_error(self) -> None:
        broken = MockUdpTracker(port=0, mode=UdpTrackerMode.TRUNCATED)
        await broken.start()
        try:
            async with UdpTracker(broken.url, timeout=1.0) as tracker:
                with pytest.raises(TrackerProtocolError, match="too short for action 1"):
                    await tracker.announce(request())
        finally:
            await broken.stop()

    async def test_a_stale_connection_id_costs_one_reconnect(
        self, udp_tracker: MockUdpTracker
    ) -> None:
        async with UdpTracker(udp_tracker.url, timeout=1.0) as tracker:
            await tracker.announce(request())
            # Forget every id we ever issued, as a tracker that has restarted
            # would: the next announce must be refused, retried with a fresh id,
            # and then succeed rather than failing the torrent.
            udp_tracker.issued_connection_ids.clear()
            response = await tracker.announce(request())

        assert response.interval == udp_tracker.interval
        assert udp_tracker.connect_count == 2
        assert udp_tracker.announce_count >= 3

    async def test_a_tracker_that_refuses_a_fresh_connection_is_not_retried(self) -> None:
        refusing = MockUdpTracker(port=0, mode=UdpTrackerMode.STALE)
        await refusing.start()
        try:
            async with UdpTracker(refusing.url, timeout=1.0) as tracker:
                with pytest.raises(TrackerProtocolError, match="Connection ID missmatch"):
                    await tracker.announce(request())
        finally:
            await refusing.stop()
        assert refusing.announce_count == 1, "a fresh id was refused; retrying is pointless"

    async def test_an_unreachable_tracker_is_a_connection_error(self) -> None:
        # Port 1 is privileged: nothing is listening there.
        async with UdpTracker("udp://127.0.0.1:1", timeout=0.2, max_retries=0) as tracker:
            with pytest.raises(TrackerTimeoutError):
                await tracker.announce(request())


class TestTheSocket:
    async def test_closing_twice_is_allowed(self, udp_tracker: MockUdpTracker) -> None:
        tracker = UdpTracker(udp_tracker.url)
        await tracker.announce(request())
        await tracker.aclose()
        await tracker.aclose()
        assert tracker.connection_id is None

    async def test_a_closed_tracker_opens_a_new_socket_when_needed(
        self, udp_tracker: MockUdpTracker
    ) -> None:
        tracker = UdpTracker(udp_tracker.url)
        await tracker.announce(request())
        await tracker.aclose()
        response = await tracker.announce(request())
        assert response.interval == udp_tracker.interval
        await tracker.aclose()


class TestTransactionIds:
    async def test_a_reply_to_a_different_request_is_ignored(self) -> None:
        # The filter lives at the socket edge: a packet that is not the answer
        # to the question we just asked must never reach a parser.
        endpoint = _UdpEndpoint()
        future = endpoint.wait_for(ACTION_ANNOUNCE, 11)

        endpoint.datagram_received(struct.pack(">II", ACTION_ANNOUNCE, 12), ("127.0.0.1", 1))
        endpoint.datagram_received(b"\x00" * 4, ("127.0.0.1", 1))  # too short to judge
        assert not future.done(), "neither was the reply we were waiting for"

        endpoint.datagram_received(
            struct.pack(">IIIII", ACTION_ANNOUNCE, 11, 1800, 0, 0), ("127.0.0.1", 1)
        )
        await asyncio.wait_for(future, timeout=0.1)
        assert future.done()

    async def test_an_error_for_our_transaction_is_still_delivered(self) -> None:
        endpoint = _UdpEndpoint()
        future = endpoint.wait_for(ACTION_ANNOUNCE, 11)
        endpoint.datagram_received(struct.pack(">II", ACTION_ERROR, 11) + b"nope", ("127.0.0.1", 1))
        raw = await asyncio.wait_for(future, timeout=0.1)
        assert error_message(raw) == "nope"


class TestEvents:
    async def test_an_announce_is_published(self, udp_tracker: MockUdpTracker) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe_all(lambda event: seen.append(event))

        async with UdpTracker(udp_tracker.url, event_bus=bus) as tracker:
            await tracker.announce(request(event=TrackerEvent.STARTED))
            await bus.drain()

        types = [event.type for event in seen]
        assert EventType.TRACKER_REQUEST in types
        response_event = next(event for event in seen if event.type is EventType.TRACKER_RESPONSE)
        assert response_event.data["transport"] == "udp"
        assert "latency_ms" in response_event.data

    async def test_a_failure_is_published(self) -> None:
        silent = MockUdpTracker(port=0, mode=UdpTrackerMode.SILENT)
        await silent.start()
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe_all(lambda event: seen.append(event))
        try:
            async with UdpTracker(
                silent.url, timeout=0.05, max_retries=0, event_bus=bus
            ) as tracker:
                with pytest.raises(TrackerTimeoutError):
                    await tracker.announce(request())
                await bus.drain()
        finally:
            await silent.stop()

        failures = [event for event in seen if event.type is EventType.TRACKER_FAILED]
        assert failures
        assert failures[0].level == logging.WARNING
        assert "did not answer" in str(failures[0].message)
