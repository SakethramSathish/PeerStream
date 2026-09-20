"""Peer exchange (BEP 11): the peers we are talking to, told to the peers we are talking to.

A tracker answers once every thirty minutes and a DHT walk costs a lookup. PEX
costs nothing: every peer we are already connected to knows a handful of others,
and swaps them with us about once a minute. In a swarm with no tracker at all
it is usually the only source still producing addresses, which is why it lands
with the DHT rather than after it.

The message is small — compact address records in a bencoded dictionary — but
BEP 11's rules are where the correctness lives, and each one is implemented
here rather than left to the caller:

**Only peers we are actually connected to.** A contact goes in ``added`` once
its handshake has completed, and in ``dropped`` once it is gone. Advertising
candidates we have not dialled would turn every client into a relay for
addresses nobody has verified, which is how a swarm gets used to aim a denial
of service at a third party. A peer we have said ``added`` for must eventually
be said ``dropped`` for; :class:`PexLedger` is the memory of who has been told
what, and it is the reason this module is a class and not two functions.

**One message per minute, per peer.** Batching is mandatory, not polite. The
ledger refuses to build a message sooner than that, so a caller can ask as
often as it likes.

**Caps.** Fifty added and fifty dropped per message after the first, which is
allowed to be bigger because it is the whole swarm at once. An added contact is
never also a dropped contact in the same message, and neither list contains
duplicates.

**Flags only for what we measured.** BEP 11 defines five bits. We set two:
``0x02`` when the peer's own bitfield says it holds every piece, and ``0x10``
when we dialled it and it answered, which is the definition of reachable. The
other three — prefers encryption, supports uTP, holepunch — describe features
this client does not implement, so it cannot observe them in anyone else and
does not claim them.

Incoming messages are untrusted, and treated that way: the compact lists are
length-checked, addresses are capped per message, and
:func:`sanitize_incoming` drops the ones that would send us dialling in circles
(ourselves, port 0, an address family we cannot parse).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.bencode import BencodeValue, decode, encode
from app.bencode.errors import BencodeError
from app.core.constants import MAX_PORT, MIN_PORT
from app.peer.errors import PeerError
from app.peer.extension import UT_PEX
from app.tracker.base import PeerAddress

logger = logging.getLogger(__name__)

PEX_MIN_INTERVAL: Final[float] = 60.0
"""Seconds between messages to one peer. BEP 11 requires batching to one a minute."""

PEX_MAX_PER_MESSAGE: Final[int] = 50
"""Added (and dropped) contacts per message, after the first one."""

PEX_MAX_INITIAL: Final[int] = 200
"""Contacts allowed in the first message to a peer.

The whole swarm, once. BEP 11 exempts the initial message from the fifty-entry
cap because a peer that joins late would otherwise learn the swarm one contact
per minute; 200 is a ceiling of our own, so a 5 000-peer swarm does not become
a 30 KB message.
"""

PEX_MAX_ACCEPTED_PER_MESSAGE: Final[int] = 200
"""How many addresses we will take from one incoming message.

A peer may legitimately know a large swarm, but a single message is not the
only message it will send, and BEP 11's security note is explicit that a client
should not take all its candidates from one source. Anything past the cap is
dropped and counted, not queued.
"""

COMPACT_IPV4_SIZE: Final[int] = 6
COMPACT_IPV6_SIZE: Final[int] = 18

FLAG_PREFERS_ENCRYPTION: Final[int] = 0x01
FLAG_SEED: Final[int] = 0x02
FLAG_UTP: Final[int] = 0x04
FLAG_HOLEPUNCH: Final[int] = 0x08
FLAG_REACHABLE: Final[int] = 0x10

ADDED: Final[bytes] = b"added"
ADDED_FLAGS: Final[bytes] = b"added.f"
ADDED6: Final[bytes] = b"added6"
ADDED6_FLAGS: Final[bytes] = b"added6.f"
DROPPED: Final[bytes] = b"dropped"
DROPPED6: Final[bytes] = b"dropped6"


class PexError(PeerError):
    """A peer-exchange message we could not read."""


@dataclass(frozen=True, slots=True)
class PexContact:
    """One peer we can describe to somebody else.

    Attributes:
        address: Where it is.
        seed: Whether its bitfield says it holds every piece. The only flag we
            can measure about a peer from its own messages.
        reachable: Whether we dialled it and it completed a handshake. A peer
            that connected to *us* is not evidence it accepts connections, so
            this stays False for incoming ones.
    """

    address: PeerAddress
    seed: bool = False
    reachable: bool = False

    @property
    def flags(self) -> int:
        """The BEP 11 flag byte for this contact, built only from measurements."""
        value = 0
        if self.seed:
            value |= FLAG_SEED
        if self.reachable:
            value |= FLAG_REACHABLE
        return value

    @property
    def is_ipv6(self) -> bool:
        """Whether the address needs the 18-byte compact form."""
        return _family(self.address.host) == socket.AF_INET6


@dataclass(frozen=True, slots=True)
class PexMessage:
    """One parsed ``ut_pex`` payload.

    Attributes:
        added: Contacts the sender has newly connected to, IPv4.
        added6: The same, IPv6.
        dropped: Contacts the sender has disconnected from, IPv4.
        dropped6: The same, IPv6.
        flags: Flag byte per added contact, when the sender sent ``added.f``.
            Indexed against ``added`` then ``added6`` concatenated, as BEP 11
            specifies per family.
        flags6: Flag byte per added IPv6 contact.
        truncated: How many contacts were past our acceptance cap. Reported
            rather than silently ignored, because "a peer told us 900 peers" is
            worth a log line.
    """

    added: tuple[PeerAddress, ...] = ()
    added6: tuple[PeerAddress, ...] = ()
    dropped: tuple[PeerAddress, ...] = ()
    dropped6: tuple[PeerAddress, ...] = ()
    flags: bytes = b""
    flags6: bytes = b""
    truncated: int = 0

    @property
    def peers(self) -> tuple[PeerAddress, ...]:
        """Every address the message offered, IPv4 first."""
        return self.added + self.added6

    @property
    def is_empty(self) -> bool:
        """Whether the message carried no contacts at all."""
        return not (self.added or self.added6 or self.dropped or self.dropped6)


def encode_pex(
    *,
    added: Sequence[PexContact] = (),
    dropped: Sequence[PeerAddress] = (),
) -> bytes:
    """Bencode a ``ut_pex`` body.

    Args:
        added: Contacts to advertise, with the flags each has earned.
        dropped: Contacts to withdraw.

    Raises:
        PexError: If both lists are empty. BEP 11 requires at least one of
            ``added``, ``added6``, ``dropped`` or ``dropped6``, and an empty
            message is a protocol violation a peer is entitled to drop us for.
    """
    if not added and not dropped:
        raise PexError("a pex message must carry at least one contact")

    body: dict[bytes, BencodeValue] = {}
    added_v4 = [contact for contact in added if not contact.is_ipv6]
    added_v6 = [contact for contact in added if contact.is_ipv6]
    dropped_v4 = [address for address in dropped if _family(address.host) != socket.AF_INET6]
    dropped_v6 = [address for address in dropped if _family(address.host) == socket.AF_INET6]

    if added_v4:
        body[ADDED] = _compact_v4(contact.address for contact in added_v4)
        body[ADDED_FLAGS] = bytes(contact.flags for contact in added_v4)
    if added_v6:
        body[ADDED6] = _compact_v6(contact.address for contact in added_v6)
        body[ADDED6_FLAGS] = bytes(contact.flags for contact in added_v6)
    if dropped_v4:
        body[DROPPED] = _compact_v4(dropped_v4)
    if dropped_v6:
        body[DROPPED6] = _compact_v6(dropped_v6)
    return encode(body)


def decode_pex(payload: bytes) -> PexMessage:
    """Parse a ``ut_pex`` body.

    Args:
        payload: The bytes of an ``Extended`` message whose id is the peer's
            ``ut_pex`` id.

    Raises:
        PexError: If the body is not bencoded, is not a dictionary, or carries a
            compact list whose length is not a whole number of records. A
            truncated list is not a message we can partly believe.
    """
    try:
        value = decode(payload)
    except BencodeError as exc:
        raise PexError(f"peer sent an unreadable pex message: {exc}") from exc
    if not isinstance(value, dict):
        raise PexError("peer's pex message was not a dictionary")

    added = _read_records(value, ADDED, socket.AF_INET)
    added6 = _read_records(value, ADDED6, socket.AF_INET6)
    dropped = _read_records(value, DROPPED, socket.AF_INET)
    dropped6 = _read_records(value, DROPPED6, socket.AF_INET6)

    truncated = 0
    total = len(added) + len(added6)
    if total > PEX_MAX_ACCEPTED_PER_MESSAGE:
        keep = PEX_MAX_ACCEPTED_PER_MESSAGE
        truncated = total - keep
        if keep <= len(added):
            added, added6 = tuple(added[:keep]), ()
        else:
            added6 = tuple(added6[: keep - len(added)])

    return PexMessage(
        added=added,
        added6=added6,
        dropped=dropped,
        dropped6=dropped6,
        flags=_read_bytes(value, ADDED_FLAGS),
        flags6=_read_bytes(value, ADDED6_FLAGS),
        truncated=truncated,
    )


def sanitize_incoming(
    message: PexMessage, *, our_address: PeerAddress | None = None
) -> tuple[PeerAddress, ...]:
    """The addresses from a peer's message that we will actually consider.

    BEP 11 says PEX data is untrusted and potentially malicious, and names the
    abuse: a client induced to dial a victim's address range. The defences here
    are the cheap ones — drop ourselves, drop anything unparseable or out of
    range, drop duplicate IPs (the same host on two ports is one host), and keep
    the order the peer sent so a test can see what arrived.

    Args:
        message: The parsed message.
        our_address: Our own listening address, so we do not dial ourselves.

    Returns:
        Addresses worth handing to the peer manager, which applies its own
        backoff, slot caps and deduplication on top.
    """
    accepted: list[PeerAddress] = []
    seen_hosts: set[str] = set()
    if our_address is not None:
        seen_hosts.add(our_address.host)

    for candidate in message.peers:
        if candidate.host in seen_hosts:
            continue
        if not MIN_PORT <= candidate.port <= MAX_PORT:
            continue
        if _family(candidate.host) is None:
            continue
        seen_hosts.add(candidate.host)
        accepted.append(candidate)
    return tuple(accepted)


@dataclass(slots=True)
class PexLedger:
    """Who we have told one peer about, and what has changed since.

    One ledger per connection, owned by the connection. The rules it exists to
    enforce are the ones a stateless encoder cannot: no contact is both added
    and dropped in the same message, no duplicates within a message, a peer we
    advertised as added is eventually advertised as dropped, and no message
    sooner than a minute after the last one.

    Attributes:
        min_interval: Seconds between messages. Injectable for tests.
        now: Clock, injectable for the same reason.
    """

    min_interval: float = PEX_MIN_INTERVAL
    now: Callable[[], float] = time.monotonic
    _told_added: set[tuple[str, int]] = field(default_factory=set, init=False)
    _pending_added: dict[tuple[str, int], PexContact] = field(default_factory=dict, init=False)
    _pending_dropped: dict[tuple[str, int], PeerAddress] = field(default_factory=dict, init=False)
    _last_sent: float | None = field(default=None, init=False)
    _initial_sent: bool = field(default=False, init=False)

    @property
    def told(self) -> int:
        """How many contacts this peer has been told we are connected to."""
        return len(self._told_added)

    @property
    def pending(self) -> int:
        """How many changes are waiting for the next message."""
        return len(self._pending_added) + len(self._pending_dropped)

    @property
    def due(self) -> bool:
        """Whether a minute has passed since the last message."""
        if self._last_sent is None:
            return True
        return (self._clock() - self._last_sent) >= self.min_interval

    def sync(self, contacts: Iterable[PexContact]) -> int:
        """Fold a snapshot of the connected swarm into the ledger.

        The manager calls this once per pass with the peers it is talking to
        right now, which is more robust than an event per connect and
        disconnect: a missed event would leave a peer advertised forever, and a
        snapshot cannot drift. Contacts already advertised are left alone, and
        advertised contacts missing from the snapshot are queued for retraction.

        Args:
            contacts: Every peer this ledger's owner may be told about. The
                recipient itself must already be excluded — telling a peer about
                itself is legal but useless, and it would occupy a slot in a
                capped message.

        Returns:
            How many changes are pending, which is zero when there is nothing to
            send.
        """
        current: dict[tuple[str, int], PexContact] = {}
        for contact in contacts:
            current[contact.address.address] = contact
        for contact in current.values():
            self.note_connected(contact)
        for key in tuple(self._told_added):
            if key not in current:
                host, port = key
                self.note_disconnected(PeerAddress(host=host, port=port, source="pex"))
        return self.pending

    def note_connected(self, contact: PexContact) -> None:
        """A peer completed its handshake; it becomes advertisable.

        A contact already advertised stays advertised: BEP 11 has no "updated"
        list, so a peer that becomes a seed later is not re-announced, and one
        that flapped is not re-announced either. Cancelling the pending
        retraction is the whole effect, which is what "transient events may be
        elided" means.
        """
        key = contact.address.address
        self._pending_dropped.pop(key, None)
        if key in self._told_added:
            return
        self._pending_added[key] = contact

    def note_disconnected(self, address: PeerAddress) -> None:
        """A peer went away; withdraw it if we ever advertised it."""
        key = address.address
        self._pending_added.pop(key, None)
        if key in self._told_added:
            # A contact we never mentioned needs no retraction, and sending one
            # would be a dropped for an added the peer never saw.
            self._pending_dropped[key] = address

    def build(self) -> bytes | None:
        """The next message to send, or None if there is nothing to say yet.

        Returns None when the interval has not elapsed, or when the pending
        changes cancel out to nothing. A caller that gets bytes back has a
        message it is allowed to send.
        """
        if not self.due:
            return None
        added = self._drain_added()
        dropped = self._drain_dropped()
        if not added and not dropped:
            return None
        payload = encode_pex(added=added, dropped=dropped)
        self._last_sent = self._clock()
        self._initial_sent = True
        for contact in added:
            self._told_added.add(contact.address.address)
        return payload

    def _drain_added(self) -> list[PexContact]:
        limit = PEX_MAX_INITIAL if not self._initial_sent else PEX_MAX_PER_MESSAGE
        drained: list[PexContact] = []
        for key, contact in list(self._pending_added.items()):
            if len(drained) >= limit:
                break  # the rest waits for the next minute, not dropped forever
            drained.append(contact)
            del self._pending_added[key]
        return drained

    def _drain_dropped(self) -> list[PeerAddress]:
        """Take the retractions out, up to the cap.

        No contact can be in both lists: :meth:`note_connected` only queues a
        contact it has not advertised, and :meth:`note_disconnected` only queues
        one it has. The invariant BEP 11 demands is therefore held by
        construction rather than repaired here.
        """
        drained: list[PeerAddress] = []
        for key, address in list(self._pending_dropped.items()):
            if len(drained) >= PEX_MAX_PER_MESSAGE:
                break
            drained.append(address)
            del self._pending_dropped[key]
            self._told_added.discard(key)
        return drained

    def _clock(self) -> float:
        return self.now()


def _family(host: str) -> int | None:
    """The address family of a host string, or None if it is not an IP at all.

    A hostname cannot go in a compact record — the format is raw address bytes —
    so a name is not a family and the caller has to skip it rather than guess.
    """
    try:
        version = ipaddress.ip_address(host).version
    except ValueError:
        return None
    return socket.AF_INET6 if version == 6 else socket.AF_INET


def _compact_v4(addresses: Iterable[PeerAddress]) -> bytes:
    return b"".join(
        socket.inet_pton(socket.AF_INET, address.host)
        + address.port.to_bytes(2, "big", signed=False)
        for address in addresses
    )


def _compact_v6(addresses: Iterable[PeerAddress]) -> bytes:
    return b"".join(
        socket.inet_pton(socket.AF_INET6, address.host)
        + address.port.to_bytes(2, "big", signed=False)
        for address in addresses
    )


def _read_records(
    value: Mapping[bytes, BencodeValue], key: bytes, family: int
) -> tuple[PeerAddress, ...]:
    """Parse one compact list out of a decoded pex body."""
    raw = value.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, bytes):
        raise PexError(f"pex field {key.decode()} was not a string")
    size = COMPACT_IPV4_SIZE if family == socket.AF_INET else COMPACT_IPV6_SIZE
    if len(raw) % size:
        raise PexError(f"pex field {key.decode()} has {len(raw)} bytes, not a multiple of {size}")
    records: list[PeerAddress] = []
    for offset in range(0, len(raw), size):
        record = raw[offset : offset + size]
        host = socket.inet_ntop(family, record[: size - 2])
        port = int.from_bytes(record[size - 2 :], "big")
        if not MIN_PORT <= port <= MAX_PORT:
            continue  # port 0 is not a listening peer, and neither is 70 000
        records.append(PeerAddress(host=host, port=port, source="pex"))
    return tuple(records)


def _read_bytes(value: Mapping[bytes, BencodeValue], key: bytes) -> bytes:
    raw = value.get(key)
    if raw is None:
        return b""
    if not isinstance(raw, bytes):
        raise PexError(f"pex field {key.decode()} was not a string")
    return raw


__all__ = [
    "COMPACT_IPV4_SIZE",
    "COMPACT_IPV6_SIZE",
    "FLAG_HOLEPUNCH",
    "FLAG_PREFERS_ENCRYPTION",
    "FLAG_REACHABLE",
    "FLAG_SEED",
    "FLAG_UTP",
    "PEX_MAX_ACCEPTED_PER_MESSAGE",
    "PEX_MAX_INITIAL",
    "PEX_MAX_PER_MESSAGE",
    "PEX_MIN_INTERVAL",
    "UT_PEX",
    "PexContact",
    "PexError",
    "PexLedger",
    "PexMessage",
    "decode_pex",
    "encode_pex",
    "sanitize_incoming",
]
