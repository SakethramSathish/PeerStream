"""The 68-byte peer handshake (BEP 3, TRD §25).

Both ends send this immediately after the TCP connection opens, before any
other bytes:

```
+--------+--------------------------------+
| 1 byte | pstrlen = 19                   |
| 19     | "BitTorrent protocol"          |
| 8      | reserved / extension flags     |
| 20     | info_hash                      |
| 20     | peer_id                        |
+--------+--------------------------------+
```

Three checks have to happen before we say a word back (TRD §25): ``pstrlen``
is 19, ``pstr`` is exactly ``BitTorrent protocol``, and ``info_hash`` matches
ours. The last one is what keeps a peer from a different swarm — or a port
scanner — attached to our piece state.

The reserved bytes are the extension handshake. We advertise nothing today, but
we must read them: bit 63 (last byte, low bit) means the peer speaks DHT
(BEP 5), bit 43 means the extension protocol (BEP 10). Reporting them honestly
is what lets the UI show "DHT: not enabled" rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from hmac import compare_digest
from typing import Final

from app.core.constants import (
    HANDSHAKE_LENGTH,
    INFO_HASH_SIZE,
    PEER_ID_SIZE,
    PROTOCOL_STRING,
    PROTOCOL_STRING_LENGTH,
    RESERVED_BYTES_LENGTH,
)
from app.core.peer_id import identify_peer
from app.peer.errors import HandshakeError

# Reserved-byte flags, counted from the most significant bit of byte 0.
DHT_FLAG_BYTE: Final[int] = 7  # bit 63 → BEP 5 DHT
DHT_FLAG_MASK: Final[int] = 0x01
EXTENSION_FLAG_BYTE: Final[int] = 5  # bit 43 → BEP 10 extension protocol
EXTENSION_FLAG_MASK: Final[int] = 0x10
FAST_FLAG_BYTE: Final[int] = 7  # bit 61 → BEP 6 fast extension
FAST_FLAG_MASK: Final[int] = 0x04

DEFAULT_RESERVED: Final[bytes] = bytes(RESERVED_BYTES_LENGTH)


@dataclass(frozen=True, slots=True)
class Handshake:
    """A parsed, validated handshake.

    Args:
        info_hash: The 20-byte info hash of the torrent being exchanged.
        peer_id: The 20-byte id of the sending peer.
        reserved: The 8 reserved/extension bytes.
    """

    info_hash: bytes
    peer_id: bytes
    reserved: bytes = DEFAULT_RESERVED

    def __post_init__(self) -> None:
        if len(self.info_hash) != INFO_HASH_SIZE:
            raise HandshakeError(
                f"info_hash must be {INFO_HASH_SIZE} bytes, got {len(self.info_hash)}"
            )
        if len(self.peer_id) != PEER_ID_SIZE:
            raise HandshakeError(f"peer_id must be {PEER_ID_SIZE} bytes, got {len(self.peer_id)}")
        if len(self.reserved) != RESERVED_BYTES_LENGTH:
            raise HandshakeError(
                f"reserved must be {RESERVED_BYTES_LENGTH} bytes, got {len(self.reserved)}"
            )

    # ---------------------------------------------------------------- coding

    def encode(self) -> bytes:
        """Serialise to the 68-byte wire form."""
        return (
            bytes((PROTOCOL_STRING_LENGTH,))
            + PROTOCOL_STRING
            + self.reserved
            + self.info_hash
            + self.peer_id
        )

    @classmethod
    def decode(cls, data: bytes, *, expected_info_hash: bytes | None = None) -> Handshake:
        """Parse a handshake received from a peer.

        Args:
            data: Exactly 68 bytes, as read from the socket.
            expected_info_hash: Our torrent's info hash. When given, a
                mismatch is fatal — the peer is on another swarm.

        Raises:
            HandshakeError: If the length, protocol string or info hash is
                wrong.
        """
        if len(data) != HANDSHAKE_LENGTH:
            raise HandshakeError(f"handshake must be {HANDSHAKE_LENGTH} bytes, got {len(data)}")

        protocol_length = data[0]
        if protocol_length != PROTOCOL_STRING_LENGTH:
            raise HandshakeError(f"unsupported protocol string length {protocol_length}")

        protocol = data[1 : 1 + PROTOCOL_STRING_LENGTH]
        if protocol != PROTOCOL_STRING:
            raise HandshakeError(f"unsupported protocol {protocol!r}")

        reserved = data[20:28]
        info_hash = data[28:48]
        peer_id = data[48:68]

        if expected_info_hash is not None and not compare_digest(info_hash, expected_info_hash):
            raise HandshakeError(
                f"info_hash mismatch: peer wants {info_hash.hex()[:16]}..., "
                f"we are serving {expected_info_hash.hex()[:16]}..."
            )

        return cls(info_hash=info_hash, peer_id=peer_id, reserved=reserved)

    # ------------------------------------------------------------ extensions

    @property
    def supports_dht(self) -> bool:
        """Whether the peer advertises DHT support (BEP 5)."""
        return bool(self.reserved[DHT_FLAG_BYTE] & DHT_FLAG_MASK)

    @property
    def supports_extensions(self) -> bool:
        """Whether the peer advertises the extension protocol (BEP 10)."""
        return bool(self.reserved[EXTENSION_FLAG_BYTE] & EXTENSION_FLAG_MASK)

    @property
    def supports_fast(self) -> bool:
        """Whether the peer advertises the fast extension (BEP 6)."""
        return bool(self.reserved[FAST_FLAG_BYTE] & FAST_FLAG_MASK)

    # ----------------------------------------------------------- description

    @property
    def client(self) -> str:
        """The peer's client software, inferred from its peer id."""
        return identify_peer(self.peer_id)

    def __str__(self) -> str:
        flags = ",".join(
            name
            for name, enabled in (
                ("dht", self.supports_dht),
                ("ext", self.supports_extensions),
                ("fast", self.supports_fast),
            )
            if enabled
        )
        return f"Handshake(client={self.client}, info_hash={self.info_hash.hex()[:12]}, flags={flags or 'none'})"


def outgoing_handshake(
    info_hash: bytes,
    peer_id: bytes,
    *,
    dht: bool = False,
    extensions: bool = False,
) -> Handshake:
    """Build the handshake we send, advertising what we actually implement.

    The reserved bytes are a set of promises. We set a flag only for the
    extensions this client can answer: DHT (BEP 5), once it is running, and the
    extension protocol (BEP 10), which carries metadata exchange. The fast
    extension (BEP 6) is deliberately not advertised.

    Args:
        info_hash: The torrent's info hash.
        peer_id: Our peer id.
        dht: Advertise DHT support. Only true when our DHT node is listening.
        extensions: Advertise the extension protocol. Only true when we can
            answer an extension handshake, which is what the metadata fetch
            depends on.
    """
    reserved = bytearray(DEFAULT_RESERVED)
    if dht:
        reserved[DHT_FLAG_BYTE] |= DHT_FLAG_MASK
    if extensions:
        reserved[EXTENSION_FLAG_BYTE] |= EXTENSION_FLAG_MASK
    return Handshake(info_hash=info_hash, peer_id=peer_id, reserved=bytes(reserved))
