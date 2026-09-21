"""Turning a magnet link into a torrent: peers first, then metadata.

A magnet carries a name and nothing else, so resolving one is two searches in
sequence:

1. **Find peers.** Three sources, tried together because each is unreliable on
   its own: the ``x.pe`` addresses in the link itself, the ``tr`` trackers, and
   the DHT. Trackers are the ones most likely to answer and the ones most
   likely to be dead; the DHT needs no server at all but only works if we are
   already in the network.
2. **Ask one of them for the info dictionary.** That is
   :func:`app.peer.metadata_exchange.fetch_metadata_from_any`, which verifies
   the bytes against the info hash before we believe a thing they say.

Only then does a torrent exist — one built by
:func:`app.torrent.parser.torrent_from_info`, from bytes a stranger gave us,
which is why the hash check in the previous step is the whole security model.

One honest limitation: BEP 27's ``private`` flag lives *inside* the info
dictionary, so it cannot be honoured until the metadata has already arrived.
What we can do is stop there — a torrent that turns out to be private is
reported as such, DHT-discovered peers are dropped from the result, and the
flag travels with the torrent so nothing uses the DHT for it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from hashlib import sha1

from app.core.config import Config, TrackerConfig
from app.core.constants import DEFAULT_LISTEN_PORT, PEER_ID_SIZE
from app.core.peer_id import generate_peer_id
from app.discovery.dht.node import DhtNode
from app.peer.errors import MetadataError, PeerError
from app.peer.metadata_exchange import MetadataResult, fetch_metadata_from_any
from app.torrent.magnet import MagnetUri
from app.torrent.metadata import Torrent
from app.torrent.parser import torrent_from_info
from app.tracker.base import AnnounceRequest, PeerAddress
from app.tracker.errors import TrackerError
from app.tracker.factory import build_tracker

logger = logging.getLogger(__name__)

Fetcher = Callable[..., Awaitable[MetadataResult]]
"""A metadata fetcher: peers and an info hash in, verified metadata out."""

Announcer = Callable[[str, bytes], Awaitable[Sequence[PeerAddress]]]
"""A tracker announce: a URL and an info hash in, peers out."""

MAX_TRACKERS: int = 10
"""How many of a magnet's trackers to ask.

A link can carry a dozen, most of them dead. Ten gives a good chance to find a live one
without overwhelming the network.
"""

MAX_PEERS: int = 60
"""Upper bound on the peers we will try for metadata.

Most will not answer; beyond this the marginal peer is not worth the connect.
"""

DEFAULT_RESOLVE_TIMEOUT: float = 30.0
"""Deadline for finding peers, before metadata fetching even starts."""


@dataclass(frozen=True, slots=True)
class MagnetResolution:
    """A magnet that has become a torrent.

    Attributes:
        magnet: The link we were given.
        torrent: The torrent built from the metadata, ready to add to a
            session. Its trackers are the magnet's.
        metadata: The fetch itself — raw bytes, which peer, how long it took.
        peers: Every address we found worth keeping, with their sources.
        sources: Which sources answered: ``x.pe``, ``tracker``, ``dht``.
        elapsed: Wall-clock seconds for the whole resolution.
    """

    magnet: MagnetUri
    torrent: Torrent
    metadata: MetadataResult
    peers: tuple[PeerAddress, ...] = ()
    sources: tuple[str, ...] = ()
    elapsed: float = 0.0

    @property
    def hex_info_hash(self) -> str:
        """The torrent's hex info hash, which is the magnet's."""
        return self.torrent.hex_info_hash

    @property
    def sized(self) -> bool:
        """Whether the metadata told us how big the download is."""
        return self.torrent.total_length > 0


@dataclass(slots=True)
class _Found:
    """Peers from one source, and whether that source worked at all."""

    source: str
    peers: list[PeerAddress] = field(default_factory=list)
    error: str | None = None


class MagnetResolver:
    """Resolves magnet links into torrents.

    Args:
        peer_id: Our peer id; generated when omitted.
        dht: A running DHT node, when we have one. Peers found through it are
            dropped if the torrent turns out to be private (BEP 27).
        config: Tracker configuration for the announces.
        port: The TCP port we announce to trackers, and tell the DHT.
        timeout: Deadline for finding peers.
        fetch: Metadata fetcher, replaced in tests by one that never touches a
            socket.
        announce: Tracker announce function, likewise replaceable. It is given
            a tracker URL and returns the peers it reported.

    Example::

        resolver = MagnetResolver(dht=node)
        resolution = await resolver.resolve(parse_magnet(link))
        await session.add_torrent(resolution.torrent)
    """

    def __init__(
        self,
        *,
        peer_id: bytes | None = None,
        dht: DhtNode | None = None,
        config: Config | TrackerConfig | None = None,
        port: int = DEFAULT_LISTEN_PORT,
        timeout: float = DEFAULT_RESOLVE_TIMEOUT,
        fetch: Fetcher | None = None,
        announce: Announcer | None = None,
    ) -> None:
        self.peer_id = peer_id if peer_id is not None else generate_peer_id()
        if len(self.peer_id) != PEER_ID_SIZE:
            raise ValueError(f"peer_id must be {PEER_ID_SIZE} bytes, got {len(self.peer_id)}")
        self.dht = dht
        self.config = config if isinstance(config, TrackerConfig) else (config.tracker if config else None)
        self.port = port
        self.timeout = timeout
        self._fetch = fetch or fetch_metadata_from_any
        self._announce = announce

    # ------------------------------------------------------------- resolution

    async def resolve(
        self,
        magnet: MagnetUri,
        *,
        peers: Sequence[PeerAddress] | Sequence[tuple[str, int]] = (),
        attempts: int = 40,
        metadata_timeout: float = 20.0,
    ) -> MagnetResolution:
        """Turn ``magnet`` into a torrent.

        Args:
            magnet: The link to resolve.
            peers: Extra addresses to try, for a caller that already knows
                some — a DHT node, a previous run, a peer exchange.
            attempts: How many peers to try for metadata before giving up.
            metadata_timeout: Per-peer deadline inside the metadata fetch.

        Returns:
            The resolution, whose ``torrent`` is verified against the magnet's
            info hash.

        Raises:
            MetadataError: No peer supplied usable metadata.
        """
        started = time.monotonic()
        info_hash = magnet.info_hash

        found = await self._find_peers(magnet, peers)
        candidates = _dedupe(found)
        if not candidates:
            raise MetadataError(
                f"no peers found for {magnet.hex_info_hash}: "
                + "; ".join(_describe(found))
            )

        dht_port = self.dht.address[1] if self.dht is not None and self.dht.bound else None
        result = await self._fetch(
            [(address.host, address.port) for address in candidates],
            info_hash,
            peer_id=self.peer_id,
            timeout=metadata_timeout,
            dht_port=dht_port,
            attempts=attempts,
        )

        if sha1(result.raw).digest() != info_hash:
            # The fetcher checks this too, but the resolver's promise is the
            # one the rest of the app relies on: a resolution is a torrent for
            # *this* magnet, or it is nothing.
            raise MetadataError(f"metadata did not match the info hash {magnet.hex_info_hash}")

        torrent = torrent_from_info(
            result.info,
            announce=magnet.trackers[0] if magnet.trackers else None,
            announce_list=magnet.tiers,
        )
        if torrent.private:
            # BEP 27: the flag was inside the metadata, so this is the earliest
            # moment we could know. Stop using the DHT for this swarm from here.
            candidates = tuple(a for a in candidates if a.source != "dht")
            logger.info("%s is a private torrent; DHT peers dropped", torrent.name)

        return MagnetResolution(
            magnet=magnet,
            torrent=torrent,
            metadata=result,
            peers=candidates,
            sources=tuple(f.source for f in found if f.peers),
            elapsed=time.monotonic() - started,
        )

    # ----------------------------------------------------------------- peers

    async def _find_peers(
        self,
        magnet: MagnetUri,
        extra: Sequence[PeerAddress] | Sequence[tuple[str, int]],
    ) -> list[_Found]:
        """Ask every source at once, and keep whatever answers.

        The sources are independent, so they are asked concurrently: a dead
        tracker costs its timeout and nothing else.
        """
        tasks: list[tuple[str, asyncio.Task[object]]] = []

        from_link = _Found("x.pe", [PeerAddress(host, port, source="x.pe") for host, port in magnet.peers])
        given = _Found(
            "manual",
            [
                address if isinstance(address, PeerAddress) else PeerAddress(*address, source="manual")
                for address in extra
            ],
        )

        if magnet.trackers:
            tasks.append(("tracker", asyncio.create_task(self._from_trackers(magnet))))
        if self.dht is not None and self.dht.bound:
            tasks.append(("dht", asyncio.create_task(self._from_dht(magnet.info_hash))))

        results: list[_Found] = [from_link, given]
        for source, task in tasks:
            try:
                outcome = await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                results.append(_Found(source, error=f"{source} did not answer in {self.timeout}s"))
                continue
            except (TrackerError, MetadataError, PeerError, OSError) as exc:
                results.append(_Found(source, error=f"{exc}"))
                continue
            if isinstance(outcome, _Found):
                results.append(outcome)
        return results

    async def _from_trackers(self, magnet: MagnetUri) -> _Found:
        """Announce to a few of the magnet's trackers and collect their peers."""
        found = _Found("tracker")
        urls = list(magnet.trackers)[:MAX_TRACKERS]
        if not urls:
            return found

        import asyncio

        async def fetch(url: str):
            try:
                peers = await self._announce_to(url, magnet.info_hash)
                if peers:
                    found.peers.extend(peers)
                    logger.debug("magnet tracker %s returned %d peers", url, len(peers))
            except Exception as exc:
                found.error = f"{url}: {exc}"
                logger.debug("magnet tracker %s failed: %s", url, exc)

        tasks = [asyncio.create_task(fetch(url)) for url in urls]
        # Wait just shy of the outer timeout so we don't lose all results if one tracker hangs
        done, pending = await asyncio.wait(tasks, timeout=max(0.1, self.timeout - 2.0))
        for p in pending:
            p.cancel()

        return found

    async def _announce_to(self, url: str, info_hash: bytes) -> tuple[PeerAddress, ...]:
        """Announce once, to one tracker, and close it afterwards."""
        if self._announce is not None:
            peers = await self._announce(url, info_hash)
            return tuple(PeerAddress(address.host, address.port, source="tracker") for address in peers)

        tracker = build_tracker(url, config=self.config)
        try:
            response = await tracker.announce(
                AnnounceRequest(info_hash=info_hash, peer_id=self.peer_id, port=self.port)
            )
        finally:
            await tracker.aclose()
        return tuple(response.peers)

    async def _from_dht(self, info_hash: bytes) -> _Found:
        """Walk the DHT for peers on this info hash."""
        assert self.dht is not None
        try:
            addresses = await self.dht.lookup_peers(info_hash)
        except (PeerError, OSError, TimeoutError) as exc:
            return _Found("dht", error=str(exc))
        return _Found(
            "dht", [PeerAddress(host, port, source="dht") for host, port in addresses]
        )


def _dedupe(found: Sequence[_Found], *, limit: int = MAX_PEERS) -> tuple[PeerAddress, ...]:
    """Flatten sources into one list, keeping the first sighting of a peer.

    The same address often comes from several sources. Keeping the first —
    link, tracker, then DHT — is deliberate: a peer named in the magnet was
    vouched for by whoever shared the link.
    """
    seen: set[tuple[str, int]] = set()
    ordered: list[PeerAddress] = []
    for group in found:
        for address in group.peers:
            key = (address.host, address.port)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(address)
            if len(ordered) >= limit:
                return tuple(ordered)
    return tuple(ordered)


def _describe(found: Sequence[_Found]) -> list[str]:
    """One line per source, for the error a user will actually read."""
    lines = []
    for group in found:
        if group.peers:
            lines.append(f"{group.source}: {len(group.peers)} peers")
        elif group.error:
            lines.append(f"{group.source}: {group.error}")
        else:
            lines.append(f"{group.source}: no peers")
    return lines or ["no sources tried"]
