"""The extension protocol (BEP 10): agreeing which id means what, per connection.

The base wire protocol has fourteen message ids and no room left, so every
extension since 2008 arrives the same way: bit 43 of the reserved bytes says
"we can talk about extensions at all", and the first message after the
handshake is an ``Extended`` with id 0 whose body names the extensions each
side implements and the id *it* will number them with.

That last part is the one that trips implementations up, and it is why this
module exists rather than a constant per extension. The ids are chosen by the
sender, so a message arriving with id 3 means whatever *that peer* said 3
means, and a message we send must use the id the peer gave it. Nothing can be
hard-coded, and a connection that guesses is a connection that talks past its
peer.

Two rules this module enforces, both from BEP 10:

* An id is 1-255. Zero is the handshake itself, so a peer that numbers an
  extension 0 is not offering it.
* We advertise only what we answer. Naming an extension we cannot serve is a
  promise, and the peer will send messages we drop on the floor.

The handshake also carries optional fields — ``v`` (client version), ``p``
(a port), ``metadata_size``, ``reqq`` — which belong to whatever extension
uses them. They are parsed here because they are part of the same body, and
read by their owners (:mod:`app.peer.metadata_exchange` for the last two).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from app.bencode import BencodeValue, decode, encode
from app.bencode.errors import BencodeError
from app.peer.errors import PeerError

logger = logging.getLogger(__name__)

HANDSHAKE_ID: Final[int] = 0
"""The ``Extended`` message id that carries the handshake itself."""

UT_METADATA: Final[str] = "ut_metadata"
"""BEP 9: fetching an info dictionary from a peer."""

UT_PEX: Final[str] = "ut_pex"
"""BEP 11: peer exchange."""

MAX_EXTENSION_ID: Final[int] = 255
"""Ids are one byte, and 0 is taken by the handshake."""

MAX_HANDSHAKE_LENGTH: Final[int] = 64 * 1024
"""Largest extension handshake we will parse.

Real ones are a few hundred bytes even for clients that advertise twenty
extensions. A peer sending more is broken or hostile, and the stream layer has
already refused anything past its own message cap; this is the tighter,
extension-specific bound.
"""


class ExtensionError(PeerError):
    """An extension handshake we could not read or believe."""


@dataclass(frozen=True, slots=True)
class DecodedHandshake:
    """One parsed BEP 10 handshake body.

    Attributes:
        extensions: The peer's ``m`` map: extension name to the id *it* uses.
        version: The ``v`` string, usually the client name. Empty when absent.
        port: The ``p`` field, which a peer uses to say "my DHT port is here"
            or "reach me here instead". None when absent.
        metadata_size: BEP 9's ``metadata_size``. None when absent or zero,
            because a peer holding no metadata bytes has nothing to serve.
        reqq: The peer's stated request-queue limit. None when absent.
        raw: The decoded body, for a field this client does not know about yet.
    """

    extensions: Mapping[str, int] = field(default_factory=dict)
    version: str = ""
    port: int | None = None
    metadata_size: int | None = None
    reqq: int | None = None
    raw: Mapping[bytes, BencodeValue] = field(default_factory=dict)

    def id_for(self, name: str) -> int | None:
        """The id this peer numbers ``name`` with, or None if it does not."""
        return self.extensions.get(name)

    def supports(self, name: str) -> bool:
        """Whether this peer advertised ``name``."""
        return name in self.extensions


@dataclass(slots=True)
class ExtensionState:
    """What one connection can do, in both directions.

    A connection owns one of these. ``ours`` is fixed when we build the
    handshake; ``theirs`` arrives in the peer's reply and stays empty until it
    does, which is why :attr:`negotiated` exists: sending to a peer before its
    handshake has arrived means guessing its ids.

    Attributes:
        ours: The extensions we answer, and the ids we number them with.
        theirs: The peer's, once its handshake has arrived.
        version: The peer's ``v`` string, when it sent one.
        port: The peer's ``p`` field, when it sent one.
    """

    ours: Mapping[str, int] = field(default_factory=dict)
    theirs: Mapping[str, int] = field(default_factory=dict)
    version: str = ""
    port: int | None = None
    _negotiated: bool = False

    @property
    def negotiated(self) -> bool:
        """Whether the peer's handshake has arrived."""
        return self._negotiated

    @property
    def we_offer(self) -> tuple[str, ...]:
        """The extensions we advertised, in the order we advertised them."""
        return tuple(self.ours)

    def our_id(self, name: str) -> int | None:
        """The id we number ``name`` with, or None if we do not offer it."""
        return self.ours.get(name)

    def their_id(self, name: str) -> int | None:
        """The id the peer numbers ``name`` with, or None if it does not."""
        return self.theirs.get(name)

    def shared(self) -> tuple[str, ...]:
        """Extensions both sides advertised — the only ones worth using."""
        return tuple(name for name in self.ours if name in self.theirs)

    def can_send(self, name: str) -> bool:
        """Whether a message for ``name`` can be sent on this connection now."""
        return self._negotiated and name in self.theirs

    def note_theirs(self, handshake: DecodedHandshake) -> None:
        """Record the peer's handshake. A second one overwrites the first.

        BEP 10 does not forbid a peer re-sending its handshake, and clients do
        it when they change state, so the newest answer wins rather than the
        first one being frozen in.
        """
        self.theirs = dict(handshake.extensions)
        self.version = handshake.version
        self.port = handshake.port
        self._negotiated = True

    def encode_handshake(
        self, *, version: str = "", port: int | None = None, metadata_size: int | None = None
    ) -> bytes:
        """The body of the id-0 message we send."""
        return encode_handshake(self.ours, version=version, port=port, metadata_size=metadata_size)


def encode_handshake(
    extensions: Mapping[str, int],
    *,
    version: str = "",
    port: int | None = None,
    metadata_size: int | None = None,
) -> bytes:
    """Bencode an extension handshake body advertising ``extensions``.

    Args:
        extensions: Name to the id we will number it with.
        version: Our ``v`` string. Sent when given, because a peer that asks
            what we are deserves an answer, and ours is the truth.
        port: Our ``p`` field.
        metadata_size: BEP 9's field, only meaningful when serving metadata.

    Raises:
        ExtensionError: If an id is outside 1-255, or a name is empty.

    An empty ``extensions`` map is legal and means "we speak BEP 10 and nothing
    else"; it is what a client sends when it has no extensions to offer.
    """
    names: dict[bytes, BencodeValue] = {}
    for name, identifier in extensions.items():
        if not name:
            raise ExtensionError("an extension cannot have an empty name")
        if not 1 <= identifier <= MAX_EXTENSION_ID:
            raise ExtensionError(
                f"extension id {identifier} for {name!r} is outside 1-{MAX_EXTENSION_ID}"
            )
        names[name.encode()] = identifier

    body: dict[bytes, BencodeValue] = {b"m": names}
    if version:
        body[b"v"] = version.encode()
    if port is not None:
        body[b"p"] = port
    if metadata_size is not None:
        body[b"metadata_size"] = metadata_size
    return encode(body)


def decode_handshake(payload: bytes) -> DecodedHandshake:
    """Parse the body of an ``Extended`` message with id 0.

    Args:
        payload: The bytes after the extension id.

    Raises:
        ExtensionError: If the body is not bencoded, is not a dictionary, or is
            absurdly long. Unknown fields are kept, not rejected: a peer that
            advertises something we have never heard of is normal.
    """
    if len(payload) > MAX_HANDSHAKE_LENGTH:
        raise ExtensionError(
            f"extension handshake is {len(payload)} bytes, over {MAX_HANDSHAKE_LENGTH}"
        )
    try:
        value = decode(payload)
    except BencodeError as exc:
        raise ExtensionError(f"peer sent an unreadable extension handshake: {exc}") from exc
    if not isinstance(value, dict):
        raise ExtensionError("peer's extension handshake was not a dictionary")

    extensions: dict[str, int] = {}
    raw_names = value.get(b"m")
    if isinstance(raw_names, dict):
        for raw_name, raw_id in raw_names.items():
            # An id of 0 means "not offered", and anything past one byte cannot
            # be used on the wire, so both are dropped rather than believed.
            if isinstance(raw_id, int) and 1 <= raw_id <= MAX_EXTENSION_ID:
                name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, bytes) else ""
                if name:
                    extensions[name] = raw_id

    raw_version = value.get(b"v")
    raw_port = value.get(b"p")
    raw_size = value.get(b"metadata_size")
    raw_reqq = value.get(b"reqq")

    return DecodedHandshake(
        extensions=extensions,
        version=(raw_version.decode("utf-8", "replace") if isinstance(raw_version, bytes) else ""),
        port=raw_port if isinstance(raw_port, int) and 0 < raw_port <= 65535 else None,
        metadata_size=raw_size if isinstance(raw_size, int) and raw_size > 0 else None,
        reqq=raw_reqq if isinstance(raw_reqq, int) and raw_reqq > 0 else None,
        raw=value,
    )


__all__ = [
    "HANDSHAKE_ID",
    "MAX_EXTENSION_ID",
    "MAX_HANDSHAKE_LENGTH",
    "UT_METADATA",
    "UT_PEX",
    "DecodedHandshake",
    "ExtensionError",
    "ExtensionState",
    "decode_handshake",
    "encode_handshake",
]
