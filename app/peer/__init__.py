"""Peer wire protocol: handshake, message framing, bitfields, session state.

The pieces fit together like this:

```
PeerStream (protocol.py)  →  bytes ⇄ Message
        │
        ▼
PeerSession (state.py)    →  Message ⇄ state transitions + events
        │
        ▼
PeerConnection (M5)       →  owns the stream, the session, and the policy
```

Everything below M5 is independent of sockets and of policies: messages are
immutable values that validate themselves, the stream only frames bytes, and
the session only records what messages mean. That is what makes the protocol
layer testable without a network.

Quick start::

    # M5 wraps that in a connection and a manager:
    manager = PeerManager(SwarmContext.from_torrent(torrent), peer_id=our_peer_id)
    manager.add_peers(outcome.peers)
    await manager.fill()          # connect up to the slot cap
    await manager.broadcast(Have(index=0))

Exports:
    Handshake, outgoing_handshake
    KeepAlive, Choke, Unchoke, Interested, NotInterested, Have, Bitfield,
    Request, Piece, Cancel, Port, Message, encode, decode, message_name
    PeerStream
    PeerConnection, SwarmContext
    PeerSession, ConnectionState
    Bitfield, PieceAvailability
    PeerError and subclasses
"""

from __future__ import annotations

from app.peer.bitfield import Bitfield, PieceAvailability
from app.peer.connection import PeerConnection, SwarmContext
from app.peer.errors import (
    HandshakeError,
    MessageError,
    MetadataError,
    PeerConnectionError,
    PeerDisconnected,
    PeerError,
    PeerTimeoutError,
    ProtocolError,
)
from app.peer.handshake import Handshake, outgoing_handshake
from app.peer.messages import (
    MESSAGE_NAMES,
    Cancel,
    Choke,
    Extended,
    Have,
    Interested,
    KeepAlive,
    Message,
    NotInterested,
    Piece,
    Port,
    Request,
    Unchoke,
    decode,
    encode,
    message_name,
)
from app.peer.messages import (
    Bitfield as BitfieldMessage,
)
from app.peer.metadata_exchange import (
    UT_METADATA,
    ExtensionHandshake,
    MetadataResult,
    MetadataServer,
    fetch_metadata,
    fetch_metadata_from_any,
)
from app.peer.protocol import PeerStream
from app.peer.state import ConnectionState, PeerSession

__all__ = [
    "MESSAGE_NAMES",
    "UT_METADATA",
    "Bitfield",
    "BitfieldMessage",
    "Cancel",
    "Choke",
    "ConnectionState",
    "Extended",
    "ExtensionHandshake",
    "Handshake",
    "HandshakeError",
    "Have",
    "Interested",
    "KeepAlive",
    "Message",
    "MessageError",
    "MetadataError",
    "MetadataResult",
    "MetadataServer",
    "NotInterested",
    "PeerConnection",
    "PeerConnectionError",
    "PeerDisconnected",
    "PeerError",
    "PeerSession",
    "PeerStream",
    "PeerTimeoutError",
    "Piece",
    "PieceAvailability",
    "Port",
    "ProtocolError",
    "Request",
    "SwarmContext",
    "Unchoke",
    "decode",
    "encode",
    "fetch_metadata",
    "fetch_metadata_from_any",
    "message_name",
    "outgoing_handshake",
]
