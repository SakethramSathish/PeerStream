"""The KRPC codec, on recorded bytes.

Everything here is a pure function of bytes, so none of it needs a socket: the
tests build packets by hand (sometimes wrongly on purpose) and check what the
codec makes of them. The cases that matter most are the malformed ones, because
every packet a DHT receives comes from a stranger.
"""

from __future__ import annotations

import os
import socket
import struct

import pytest
from app.bencode import encode as bencode
from app.discovery.dht.errors import KrpcError
from app.discovery.dht.krpc import (
    ANNOUNCE_PEER,
    ERROR_METHOD_UNKNOWN,
    ERROR_PROTOCOL,
    FIND_NODE,
    GET_PEERS,
    NODE_INFO_SIZE,
    PING,
    KrpcFailure,
    KrpcQuery,
    KrpcResponse,
    NodeInfo,
    decode,
    decode_nodes,
    encode_announce_peer,
    encode_error,
    encode_find_node,
    encode_get_peers,
    encode_nodes,
    encode_peers,
    encode_ping,
    encode_response,
    new_transaction_id,
)

NODE_A = bytes(range(20))
NODE_B = bytes(range(100, 120))
TARGET = bytes(range(200, 220))
INFO_HASH = bytes(range(40, 60))


def node_info(node_id: bytes, host: str = "10.0.0.1", port: int = 6881) -> NodeInfo:
    return NodeInfo(node_id=node_id, host=host, port=port)


class TestQueries:
    def test_a_ping_carries_our_id(self) -> None:
        payload = encode_ping(b"aa", NODE_A)
        message = decode(payload)
        assert isinstance(message, KrpcQuery)
        assert message.transaction_id == b"aa"
        assert message.method == PING
        assert message.node_id == NODE_A

    def test_find_node_names_its_target(self) -> None:
        message = decode(encode_find_node(b"aa", NODE_A, TARGET))
        assert isinstance(message, KrpcQuery)
        assert message.method == FIND_NODE
        assert message.target == TARGET

    def test_get_peers_names_its_torrent(self) -> None:
        message = decode(encode_get_peers(b"aa", NODE_A, INFO_HASH))
        assert isinstance(message, KrpcQuery)
        assert message.method == GET_PEERS
        assert message.info_hash == INFO_HASH

    def test_announce_peer_carries_a_port_and_a_token(self) -> None:
        message = decode(
            encode_announce_peer(b"aa", NODE_A, INFO_HASH, port=6881, token=b"tok")
        )
        assert isinstance(message, KrpcQuery)
        assert message.method == ANNOUNCE_PEER
        assert message.port == 6881
        assert message.token == b"tok"
        assert message.implied_port is False

    def test_implied_port_is_advertised_when_asked(self) -> None:
        message = decode(
            encode_announce_peer(
                b"aa", NODE_A, INFO_HASH, port=6881, token=b"tok", implied_port=True
            )
        )
        assert isinstance(message, KrpcQuery)
        assert message.implied_port is True

    def test_a_wrong_sized_node_id_never_reaches_the_wire(self) -> None:
        with pytest.raises(KrpcError, match="node id must be 20 bytes"):
            encode_ping(b"aa", b"short")


class TestResponses:
    def test_a_response_carries_the_responder(self) -> None:
        message = decode(encode_response(b"ab", {b"id": NODE_B}))
        assert isinstance(message, KrpcResponse)
        assert message.transaction_id == b"ab"
        assert message.node_id == NODE_B

    def test_nodes_are_a_compact_string(self) -> None:
        nodes = encode_nodes([node_info(NODE_A, "10.0.0.1", 6881), node_info(NODE_B, "10.0.0.2", 6882)])
        assert len(nodes) == 2 * NODE_INFO_SIZE, "it is a string, not a list"
        message = decode(encode_response(b"ab", {b"id": NODE_B, b"nodes": nodes}))
        assert isinstance(message, KrpcResponse)
        assert [node.address for node in message.nodes] == [
            ("10.0.0.1", 6881),
            ("10.0.0.2", 6882),
        ]
        assert message.nodes[0].node_id == NODE_A

    def test_peers_come_as_a_list_of_compact_addresses(self) -> None:
        body = {
            b"id": NODE_B,
            b"token": b"tok",
            b"values": encode_peers([("203.0.113.5", 51413)]),
        }
        message = decode(encode_response(b"ab", body))
        assert isinstance(message, KrpcResponse)
        assert message.peers == (("203.0.113.5", 51413),)
        assert message.token == b"tok"

    def test_a_compact_peer_record_is_six_bytes(self) -> None:
        (record,) = encode_peers([("203.0.113.5", 51413)])
        assert record == socket.inet_aton("203.0.113.5") + struct.pack(">H", 51413)

    def test_a_peer_that_is_not_compact_is_ignored(self) -> None:
        # Some clients send dictionaries. Guessing an interpretation would be
        # inventing peers, so they are dropped.
        body = {b"id": NODE_B, b"values": [{b"ip": b"203.0.113.5", b"port": 51413}, b"short"]}
        assert decode(encode_response(b"ab", body)).peers == ()

    def test_a_peer_on_port_zero_is_not_a_peer(self) -> None:
        body = {b"id": NODE_B, b"values": encode_peers([("203.0.113.5", 0)])}
        assert decode(encode_response(b"ab", body)).peers == ()


class TestFailures:
    def test_an_error_is_decoded_with_its_code(self) -> None:
        message = decode(encode_error(b"cd", ERROR_PROTOCOL, "malformed packet"))
        assert isinstance(message, KrpcFailure)
        assert message.code == ERROR_PROTOCOL
        assert message.message == "malformed packet"
        assert message.transaction_id == b"cd"

    def test_an_unknown_method_error_has_its_own_code(self) -> None:
        message = decode(encode_error(b"cd", ERROR_METHOD_UNKNOWN, "nope"))
        assert isinstance(message, KrpcFailure)
        assert message.code == ERROR_METHOD_UNKNOWN


class TestFromStrangers:
    @pytest.mark.parametrize(
        ("packet", "reason"),
        [
            (b"", "not valid bencode"),
            (b"gibberish", "not valid bencode"),
            (bencode([1, 2, 3]), "must be a dictionary"),
            (bencode({b"y": b"q"}), "transaction id"),
            (bencode({b"t": b"", b"y": b"q"}), "transaction id"),
            (bencode({b"t": b"aa", b"y": b"z"}), "unknown message type"),
            (bencode({b"t": b"aa", b"y": b"q"}), "no method name"),
            (bencode({b"t": b"aa", b"y": b"q", b"q": b"ping"}), "no argument dictionary"),
            (bencode({b"t": b"aa", b"y": b"r"}), "no response dictionary"),
            (bencode({b"t": b"aa", b"y": b"r", b"r": {}}), "no node id"),
            (bencode({b"t": b"aa", b"y": b"e", b"e": [203]}), "code, message"),
            (bencode({b"t": b"aa", b"y": b"e", b"e": [b"nope", b"x"]}), "not an integer"),
        ],
    )
    def test_a_packet_we_cannot_trust_is_refused(self, packet: bytes, reason: str) -> None:
        with pytest.raises(KrpcError, match=reason):
            decode(packet)

    def test_an_oversized_packet_is_refused(self) -> None:
        with pytest.raises(KrpcError, match="exceeds"):
            decode(b"x" * 4096)

    def test_a_node_list_that_is_not_whole_records_is_refused(self) -> None:
        with pytest.raises(KrpcError, match="not a multiple of 26"):
            decode_nodes(b"\x00" * 27)

    def test_a_node_that_does_not_listen_is_not_a_node(self) -> None:
        blob = NODE_A + socket.inet_aton("10.0.0.1") + struct.pack(">H", 0)
        assert decode_nodes(blob) == ()

    def test_a_node_id_of_the_wrong_length_is_refused(self) -> None:
        with pytest.raises(KrpcError, match="node id must be 20 bytes"):
            node_info(b"short")

    def test_a_transaction_id_is_random(self) -> None:
        # Predictable ids let anyone forge a reply to a question we asked; the
        # id is the only thing tying the two together.
        assert len({new_transaction_id() for _ in range(20)}) == 20

    def test_a_reply_to_somebody_else_is_recognised_as_one(self) -> None:
        # The codec reports the id; the transport does the matching.
        message = decode(encode_response(b"\x01\x02", {b"id": NODE_B}))
        assert message.transaction_id == b"\x01\x02"

    def test_a_long_transaction_id_is_accepted(self) -> None:
        # We send two bytes; other clients are allowed to send more.
        message = decode(encode_response(os.urandom(8), {b"id": NODE_B}))
        assert isinstance(message, KrpcResponse)
