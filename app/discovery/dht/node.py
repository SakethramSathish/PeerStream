"""A DHT node: joins the network, answers questions, finds peers (BEP 5).

Two jobs, and they are the same job seen from each end:

**As a client**, find the peers for an info-hash without a tracker. That is an
*iterative* lookup: ask the ``alpha`` nodes we know are closest to the hash who
*they* think is close, ask those, and keep going until the answers stop getting
closer. Every reply teaches us about nodes we did not know, which is why the
routing table is fuller after a lookup than before it — a DHT is a network you
learn by walking.

**As a server**, answer the same questions from other nodes. This is not
optional politeness: nodes that only ask are leeches on the routing tables of
others, and several implementations quietly ignore them. So we answer ``ping``,
``find_node``, ``get_peers`` and ``announce_peer``, and we hold the peer
announcements we are given for half an hour, which is what makes the next
node's lookup succeed.

On announcing: BEP 5 makes a node hand out a *token* with every ``get_peers``
reply and requires it back on ``announce_peer``. That is the protocol's only
defence against announcing on behalf of an address you do not control, so
tokens are derived from a rotating secret and the requester's own address —
and a token from before the last rotation is still accepted, because rotating it
out from under a slow client would be punishing honesty.

Nothing here invents a peer, a node or a count. A lookup that finds nothing
returns nothing, and the caller says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.bencode.decoder import BencodeValue
from app.core.constants import DHT_ALPHA, DHT_K, DHT_NODE_ID_SIZE, MAX_PORT, MIN_PORT
from app.discovery.dht.errors import (
    DhtBootstrapError,
    DhtError,
    DhtRemoteError,
)
from app.discovery.dht.krpc import (
    ANNOUNCE_PEER,
    ERROR_METHOD_UNKNOWN,
    ERROR_PROTOCOL,
    FIND_NODE,
    GET_PEERS,
    KEY_ID,
    KEY_IMPLIED_PORT,
    KEY_INFO_HASH,
    KEY_PORT,
    KEY_TARGET,
    KEY_TOKEN,
    PING,
    KrpcQuery,
    KrpcResponse,
    NodeInfo,
    encode_nodes,
    encode_peers,
)
from app.discovery.dht.protocol import KrpcTransport
from app.discovery.dht.routing import DhtContact, RoutingTable, contacts_from, distance

logger = logging.getLogger(__name__)

#: How long we remember a peer someone announced for a torrent.
PEER_TTL_SECONDS: Final[float] = 1800.0  # 30 minutes, half of the announce interval
#: Cap per torrent, so a hostile node cannot use us as a database.
MAX_PEERS_PER_TORRENT: Final[int] = 200
MAX_NODES_PER_REPLY: Final[int] = DHT_K
#: A lookup that has not converged in this many rounds is not going to.
MAX_ROUNDS: Final[int] = 12
TOKEN_SIZE: Final[int] = 8
#: Tokens are minted from a secret that rotates; the previous one is still
#: honoured, so a client that got a token just before the rotation keeps working.
TOKEN_ROTATION_SECONDS: Final[float] = 600.0
DEFAULT_BOOTSTRAP_TIMEOUT: Final[float] = 3.0


@dataclass(frozen=True, slots=True)
class LookupResult:
    """What one iterative lookup found.

    Attributes:
        peers: Peers the network knows about, as ``(host, port)`` pairs.
        nodes: The closest contacts we learned, useful for a later announce.
        contacted: How many nodes answered at all. Zero means the DHT is
            unreachable, which is a different failure from "no peers".
    """

    peers: tuple[tuple[str, int], ...] = ()
    nodes: tuple[DhtContact, ...] = ()
    contacted: int = 0

    @property
    def found_peers(self) -> bool:
        return bool(self.peers)


@dataclass(slots=True)
class _PeerRecord:
    """A peer someone announced, and when it expires."""

    host: str
    port: int
    expires_at: float


class DhtNode:
    """One node in the Kademlia network.

    Args:
        node_id: Our 20-byte id. Generated randomly when omitted: ids are
            supposed to be uniform, and a chosen one clusters the network.
        host: Bind address.
        port: Bind port; ``0`` picks a free one (what tests use).
        bootstrap: Known nodes to join through, ``(host, port)`` pairs.
        alpha: How many nodes we query at once during a lookup.
        k: Bucket size, and how many results a lookup aims for.
        timeout: Per-query timeout, in seconds.
        now: Clock, for tests that need to expire things without waiting.

    Attributes:
        table: The routing table.
        peers_known: How many peer announcements we are holding, across all
            torrents. Reported, never invented.
    """

    def __init__(
        self,
        *,
        node_id: bytes | None = None,
        host: str = "0.0.0.0",
        port: int = 0,
        bootstrap_nodes: Sequence[tuple[str, int]] = (),
        alpha: int = DHT_ALPHA,
        k: int = DHT_K,
        timeout: float = DEFAULT_BOOTSTRAP_TIMEOUT,
    ) -> None:
        if node_id is not None and len(node_id) != DHT_NODE_ID_SIZE:
            raise DhtError(f"node id must be {DHT_NODE_ID_SIZE} bytes, got {len(node_id)}")
        self.node_id = node_id or os.urandom(DHT_NODE_ID_SIZE)
        self.host = host
        self.port = port
        self.bootstrap_nodes = tuple(bootstrap_nodes)
        self.alpha = alpha
        self.k = k
        self.timeout = timeout
        self.table = RoutingTable(self.node_id, capacity=self.k)
        self._transport = KrpcTransport(
            handler=self._handle_query,
            host=self.host,
            port=self.port,
            timeout=self.timeout,
        )
        self._tokens: dict[tuple[bytes, bytes], bytes] = {}
        self._peers: dict[bytes, dict[tuple[str, int], _PeerRecord]] = {}
        self._transport = KrpcTransport(
            handler=self._handle_query,
            host=self.host,
            port=self.port,
            timeout=self.timeout,
        )
        self._secret = os.urandom(16)
        self._previous_secret = os.urandom(16)
        self._secret_at = time.monotonic()

    # ------------------------------------------------------------------ reading

    @property
    def hex_id(self) -> str:
        return self.node_id.hex()

    @property
    def address(self) -> tuple[str, int]:
        """Our own ``(host, port)``, once bound."""
        return (self.host, self._transport.port or self.port)

    @property
    def size(self) -> int:
        """How many contacts the routing table holds."""
        return self.table.size

    @property
    def buckets(self) -> int:
        """How many k-buckets the routing table is split into.

        One at first, more as the buckets near our own id fill — which is the
        table spending its resolution where lookups actually start.
        """
        return self.table.buckets

    @property
    def peers_known(self) -> int:
        """How many peer announcements we are holding, across every torrent."""
        return sum(len(peers) for peers in self._peers.values())

    @property
    def bound(self) -> bool:
        return self._transport.bound

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> int:
        """Bind the socket and join the network through the bootstrap nodes.

        Returns:
            The bound UDP port.

        Raises:
            DhtBootstrapError: Every bootstrap node was silent. Joining is the
                one operation that cannot be retried against a different node
                when there are none, so it is reported distinctly — "the DHT is
                unreachable" and "nobody has this torrent" are not the same
                sentence.
        """
        port = await self._transport.start()
        if not self.bootstrap_nodes:
            return port
        learned = await self.bootstrap()
        if learned == 0:
            raise DhtBootstrapError("no bootstrap node answered; the DHT is unreachable from here")
        return port

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> DhtNode:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def bootstrap(self, nodes: Sequence[tuple[str, int]] | None = None) -> int:
        """Ask the known nodes who is close to us, and remember the answers.

        Bootstrap is a ``find_node`` for our own id, which is the standard
        trick: the nodes closest to *us* are exactly the ones whose buckets we
        belong in, and asking about ourselves also announces us to them.

        Returns:
            How many nodes we learned about (zero means nobody answered).
        """
        seed = list(nodes) if nodes is not None else list(self.bootstrap_nodes)
        before = self.table.size
        for host, port in seed:
            try:
                response = await self._transport.query(
                    (host, port),
                    FIND_NODE,
                    {KEY_ID: self.node_id, KEY_TARGET: self.node_id},
                    timeout=self.timeout,
                    retries=0,
                )
            except (DhtError, OSError) as exc:
                logger.debug("bootstrap node %s:%d did not answer: %s", host, port, exc)
                continue
            self._learn(response, (host, port))
        return self.table.size - before

    # ----------------------------------------------------------------- queries

    async def ping(self, address: tuple[str, int]) -> bool:
        """Is this node there? Returns False when it does not answer."""
        try:
            response = await self._transport.query(
                address, PING, {KEY_ID: self.node_id}, timeout=self.timeout, retries=0
            )
        except DhtError as exc:
            logger.debug("ping to %s failed: %s", address, exc)
            return False
        self._learn(response, address)
        return True

    async def find_node(self, target: bytes) -> LookupResult:
        """The iterative lookup for a node id. Mostly for tests and maintenance."""
        return await self._lookup(target, want_peers=False)

    async def get_peers(self, info_hash: bytes) -> LookupResult:
        """Find peers for an info-hash, without announcing ourselves.

        Returns what the network said. An empty :attr:`LookupResult.peers` with
        a non-zero :attr:`LookupResult.contacted` means the search ran and
        nobody had the torrent — which the UI must not render as "0 peers in
        the swarm".
        """
        return await self._lookup(info_hash, want_peers=True)

    async def lookup_peers(self, info_hash: bytes) -> tuple[tuple[str, int], ...]:
        """Just the peers, for callers that do not care about the rest."""
        return (await self.get_peers(info_hash)).peers

    async def announce_peer(
        self, info_hash: bytes, *, port: int | None = None, implied_port: bool = True
    ) -> int:
        """Tell the closest nodes that we are in this swarm.

        An announce is a lookup first: you cannot announce to "the DHT", only to
        the specific nodes that are closest to the hash, and each one only
        accepts the token *it* handed out.

        Args:
            info_hash: The torrent to announce for.
            port: Our TCP port. Ignored when ``implied_port`` is set, which is
                the honest choice behind NAT: the node records the port the
                packet came from, because that is the one it can reach.
            implied_port: Tell nodes to use the packet's source port.

        Returns:
            How many nodes accepted the announce.
        """
        if not MIN_PORT <= (port or self._transport.port) <= MAX_PORT:
            raise DhtError(f"announce port {port} is outside {MIN_PORT}-{MAX_PORT}")
        # The lookup has to be a get_peers, not a find_node: the token each node
        # hands out comes with its get_peers reply, and without a token the
        # announce is refused. Asking for peers we are about to join anyway
        # costs nothing and is what every implementation does.
        result = await self._lookup(info_hash, want_peers=True)
        announced = 0
        for contact in result.nodes[: self.k]:
            token = self._tokens.get((contact.node_id, info_hash))
            if token is None:
                continue
            try:
                await self._transport.query(
                    contact.address,
                    ANNOUNCE_PEER,
                    {
                        KEY_ID: self.node_id,
                        KEY_INFO_HASH: info_hash,
                        KEY_PORT: port or self._transport.port,
                        KEY_TOKEN: token,
                        KEY_IMPLIED_PORT: 1 if implied_port else 0,
                    },
                    timeout=self.timeout,
                    retries=0,
                )
            except DhtError as exc:
                logger.debug("announce to %s refused: %s", contact, exc)
                continue
            announced += 1
        return announced

    # ------------------------------------------------------------- the lookup

    async def _lookup(self, target: bytes, *, want_peers: bool) -> LookupResult:
        """Walk the network towards ``target``, asking ``alpha`` nodes a round.

        The rule that makes it converge: each round asks the closest nodes we
        have *not* asked yet, and we stop once every node in the closest ``k``
        has been asked — at which point the network has told us everything it
        knows about the neighbourhood.
        """
        if len(target) != DHT_NODE_ID_SIZE:
            raise DhtError(f"a DHT target must be {DHT_NODE_ID_SIZE} bytes, got {len(target)}")
        seen: dict[bytes, DhtContact] = {}
        peers: dict[tuple[str, int], None] = {}
        asked: set[bytes] = set()
        contacted = 0

        for _round in range(MAX_ROUNDS):
            candidates = self._next_round(target, seen, asked)
            if not candidates:
                break
            responses = await asyncio.gather(
                *(self._ask_one(contact, target, want_peers=want_peers) for contact in candidates),
                return_exceptions=True,
            )
            for contact, response in zip(candidates, responses, strict=True):
                asked.add(contact.node_id)
                if isinstance(response, BaseException):
                    self.table.note_failure(contact.node_id)
                    continue
                contacted += 1
                self.table.note_success(response.node_id, host=contact.host, port=contact.port)
                authoritative = DhtContact(
                    node_id=response.node_id, host=contact.host, port=contact.port
                )
                seen[response.node_id] = authoritative
                if want_peers:
                    for peer in response.peers:
                        peers[peer] = None
                    if response.token:
                        self._tokens[(response.node_id, target)] = response.token
                for node in response.nodes:
                    if node.node_id == self.node_id:
                        continue
                    self.table.add(DhtContact(node_id=node.node_id, host=node.host, port=node.port))
                    existing = seen.get(node.node_id)
                    if existing is None:
                        seen[node.node_id] = DhtContact(
                            node_id=node.node_id, host=node.host, port=node.port
                        )
        closest = tuple(
            sorted(seen.values(), key=lambda contact: distance(contact.node_id, target))
        )
        return LookupResult(peers=tuple(peers), nodes=closest[: self.k], contacted=contacted)

    def _next_round(
        self, target: bytes, seen: dict[bytes, DhtContact], asked: set[bytes]
    ) -> list[DhtContact]:
        """The ``alpha`` closest nodes we have not asked yet."""
        pool: dict[bytes, DhtContact] = {
            contact.node_id: contact for contact in self.table.closest(target, self.k * 4)
        }
        pool.update(seen)
        fresh = [
            contact
            for node_id, contact in pool.items()
            if node_id not in asked and node_id != self.node_id
        ]
        fresh.sort(key=lambda contact: distance(contact.node_id, target))
        return fresh[: self.alpha]

    async def _ask_one(
        self, contact: DhtContact, target: bytes, *, want_peers: bool
    ) -> KrpcResponse:
        """One query to one node. Raises on failure, for ``gather`` to collect."""
        method = GET_PEERS if want_peers else FIND_NODE
        arguments: dict[bytes, bytes] = {KEY_ID: self.node_id}
        if want_peers:
            arguments[KEY_INFO_HASH] = target
        else:
            arguments[KEY_TARGET] = target
        response = await self._transport.query(
            contact.address, method, arguments, timeout=self.timeout, retries=0
        )
        return response

    def _learn(self, response: KrpcResponse, address: tuple[str, int]) -> None:
        """Fold one response into the routing table."""
        self.table.note_success(response.node_id, host=address[0], port=address[1])
        for node in response.nodes:
            if node.node_id != self.node_id:
                self.table.add(DhtContact(node_id=node.node_id, host=node.host, port=node.port))

    # -------------------------------------------------------------- the server

    async def _handle_query(
        self, query: KrpcQuery, address: tuple[str, int]
    ) -> Mapping[bytes, BencodeValue]:
        """Answer one query. Raises :class:`DhtRemoteError` to refuse."""
        # Every query is also a hello: the sender told us its id and address,
        # and that is worth remembering even if we go on to refuse it.
        self.table.note_success(query.node_id, host=address[0], port=address[1])

        if query.method == PING:
            return {KEY_ID: self.node_id}
        if query.method == FIND_NODE:
            target = query.target
            if not target:
                raise DhtRemoteError("find_node needs a target", code=ERROR_PROTOCOL)
            return {KEY_ID: self.node_id, b"nodes": self._nodes_near(target)}
        if query.method == GET_PEERS:
            info_hash = query.info_hash
            if not info_hash:
                raise DhtRemoteError("get_peers needs an info_hash", code=ERROR_PROTOCOL)
            body: dict[bytes, BencodeValue] = {
                KEY_ID: self.node_id,
                KEY_TOKEN: self._mint_token(address, info_hash),
            }
            known = self._peers_for(info_hash)
            if known:
                body[b"values"] = encode_peers(known)
            else:
                body[b"nodes"] = self._nodes_near(info_hash)
            return body
        if query.method == ANNOUNCE_PEER:
            info_hash = query.info_hash
            token = query.token
            if not info_hash or token is None:
                raise DhtRemoteError(
                    "announce_peer needs an info_hash and a token", code=ERROR_PROTOCOL
                )
            if not self._token_is_valid(token, address, info_hash):
                raise DhtRemoteError("invalid token", code=203)
            port = address[1] if query.implied_port else (query.port or address[1])
            self._remember_peer(info_hash, address[0], port)
            return {KEY_ID: self.node_id}
        raise DhtRemoteError(f"unknown method {query.method!r}", code=ERROR_METHOD_UNKNOWN)

    def _nodes_near(self, target: bytes) -> bytes:
        """The closest contacts to a target, as a compact ``nodes`` string."""
        nodes = [
            NodeInfo(node_id=contact.node_id, host=contact.host, port=contact.port)
            for contact in self.table.closest(target, MAX_NODES_PER_REPLY)
        ]
        return encode_nodes(nodes)

    def _peers_for(self, info_hash: bytes) -> list[tuple[str, int]]:
        """Peers we are holding for a torrent, expired ones dropped first."""
        entries = self._peers.get(info_hash)
        if not entries:
            return []
        now = time.monotonic()
        expired = [key for key, record in entries.items() if record.expires_at <= now]
        for key in expired:
            del entries[key]
        if not entries:
            del self._peers[info_hash]
        return [(host, port) for host, port in entries]

    def _remember_peer(self, info_hash: bytes, host: str, port: int) -> None:
        if not MIN_PORT <= port <= MAX_PORT:
            raise DhtRemoteError(f"port {port} is not a port", code=ERROR_PROTOCOL)
        entries = self._peers.setdefault(info_hash, {})
        if (host, port) not in entries and len(entries) >= MAX_PEERS_PER_TORRENT:
            oldest = min(entries.values(), key=lambda record: record.expires_at)
            del entries[(oldest.host, oldest.port)]
        entries[(host, port)] = _PeerRecord(
            host=host, port=port, expires_at=time.monotonic() + PEER_TTL_SECONDS
        )

    # ------------------------------------------------------------------ tokens

    def _mint_token(self, address: tuple[str, int], info_hash: bytes) -> bytes:
        return self._token_for(self._secret, address, info_hash)

    def _token_is_valid(self, token: bytes, address: tuple[str, int], info_hash: bytes) -> bool:
        """Accept a token minted from the current secret, or the one before it.

        Rotation exists so a stolen token stops working; accepting the previous
        secret is what keeps a client that fetched a token moments before the
        rotation from being punished for being slightly slow.
        """
        return token in (
            self._token_for(self._secret, address, info_hash),
            self._token_for(self._previous_secret, address, info_hash),
        )

    @staticmethod
    def _token_for(secret: bytes, address: tuple[str, int], info_hash: bytes) -> bytes:
        material = secret + address[0].encode("utf-8") + info_hash
        return hashlib.sha1(material).digest()[:TOKEN_SIZE]

    def rotate_secret(self, *, now: float | None = None) -> None:
        """Move to a new token secret, honouring the old one until further notice."""
        moment = time.monotonic() if now is None else now
        if moment - self._secret_at < TOKEN_ROTATION_SECONDS:
            return
        self._previous_secret, self._secret = self._secret, os.urandom(16)
        self._secret_at = moment

    # ------------------------------------------------------------------ helpers

    def add_contacts(self, nodes: Iterable[NodeInfo]) -> int:
        """Seed the table directly — used by tests and by a saved node file."""
        contacts = contacts_from(tuple(nodes))
        return sum(1 for contact in contacts if self.table.add(contact))
