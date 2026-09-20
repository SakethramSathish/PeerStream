"""Publishing ourselves to the DHT, so a trackerless swarm can find us (BEP 5).

M15 taught the client to *ask* the DHT where a swarm is. Asking is only half
of the protocol: the other half is ``announce_peer``, which is how a node
tells the eight nodes closest to an info hash that it is in that swarm, and
without it a client that never has a tracker can download but can never be
downloaded from. :meth:`~app.discovery.dht.node.DhtNode.announce_peer` existed
and was tested against a local cluster, but nothing in the application called
it — the DHT was a phone book with our own number missing.

Three rules from BEP 5 shape this module, and each one is a deliberate choice
rather than a default:

**An announce is a lookup first.** A node only accepts an ``announce_peer``
carrying the token *it* handed out during a ``get_peers``, so publishing costs
a full iterative lookup per torrent. That is why the interval is fifteen
minutes and why the loop announces torrents one at a time.

**Announce no more often than every fifteen minutes, and stop before
shutdown.** BEP 5 asks for at most one announce per fifteen minutes, and asks
a node to stop announcing roughly two minutes before it goes away. There is no
``goodbye`` in BEP 5: an entry we published lives on the remote node until its
own TTL expires, so :meth:`unregister` stops us republishing and the record
ages out. :meth:`DhtAnnouncer.go_quiet` exists for callers that know a shutdown is
coming; :data:`SHUTDOWN_QUIET_SECONDS` records the figure BEP 5 asks for.

**Never publish an address that is not ours.** A torrent that is not listening
on a TCP port has nothing to be reached on; announcing it would send peers to
a socket that refuses them. Those torrents are skipped and counted as skipped,
which is what the UI shows. Private torrents (BEP 27) are refused outright:
the flag means "trackers only", and the DHT is a tracker we do not control.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Protocol

from app.core.constants import DEFAULT_DHT_ANNOUNCE_INTERVAL
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.discovery.dht.errors import DhtError

logger = logging.getLogger(__name__)

DEFAULT_ANNOUNCE_INTERVAL: float = DEFAULT_DHT_ANNOUNCE_INTERVAL
"""Seconds between announce passes. BEP 5's fifteen minutes, no more often."""

MIN_ANNOUNCE_INTERVAL: float = 0.05
"""The shortest interval the loop accepts.

This is the code's floor, not the network's: :data:`app.core.config` bounds the
user-facing setting at 60 s, because announcing faster than that is rude to
nodes that are not ours. The floor here only exists so a zero or negative
interval cannot spin the event loop, and so a test can watch the loop repeat
without waiting a minute.
"""

SHUTDOWN_QUIET_SECONDS: float = 120.0
"""How long before a known shutdown we stop announcing (BEP 5's two minutes)."""

SKIPPED_RETRY_INTERVAL: float = 30.0
"""How soon to retry a torrent we could not publish.

A torrent that is not listening yet is not unpublishable, it is *not yet*
publishable: its engine binds a port a moment later, and a session that starts
paused may be resumed an hour from now. Waiting a whole announce interval to
notice would leave the client invisible to a trackerless swarm for fifteen
minutes after every start, which is the difference between working and
appearing to work. Private torrents are the exception — that refusal never
changes, so they do not earn a retry.
"""

NOT_LISTENING: Final[str] = "not listening: no port to publish"
"""Why a torrent was skipped when it has no TCP port. This can change."""

PRIVATE: Final[str] = "private torrent: BEP 27 keeps it off the DHT"
"""Why a private torrent was skipped. This never changes, so it earns no retry."""


class AnnouncingNode(Protocol):
    """The part of :class:`~app.discovery.dht.node.DhtNode` this module uses.

    A protocol rather than the class itself, so the announce loop can be tested
    against a node that records calls instead of one that opens a socket. The
    real node satisfies it structurally.
    """

    @property
    def bound(self) -> bool:
        """Whether the node's socket is open and answering."""
        ...

    async def announce_peer(
        self, info_hash: bytes, *, port: int | None = None, implied_port: bool = True
    ) -> int:
        """Tell the closest nodes we are in this swarm; how many accepted."""
        ...


@dataclass(slots=True)
class AnnounceStatus:
    """What the last announce attempt did for one torrent.

    Attributes:
        info_hash: The torrent, as raw bytes.
        announced: How many nodes accepted our last announce. Zero means the
            lookup found nobody with a token for us, which is what a node that
            has not bootstrapped looks like.
        attempts: How many passes have tried this torrent.
        failures: How many of those raised.
        last_attempt: Clock reading of the last pass; ``None`` if never.
        last_error: The most recent failure's text, or ``""``.
        skipped: Why the last pass declined, or ``""`` when it announced.
    """

    info_hash: bytes
    announced: int = 0
    attempts: int = 0
    failures: int = 0
    last_attempt: float | None = None
    last_error: str = ""
    skipped: str = ""

    @property
    def hex_info_hash(self) -> str:
        """The info hash as hex, which is how the rest of the app names things."""
        return self.info_hash.hex()

    @property
    def published(self) -> bool:
        """Whether some node has accepted an announce and none has refused since."""
        return self.announced > 0 and not self.skipped


@dataclass(slots=True)
class _Registration:
    """One torrent we publish, and how to ask what port it listens on."""

    info_hash: bytes
    port: Callable[[], int]
    private: bool = False
    status: AnnounceStatus = field(init=False)

    def __post_init__(self) -> None:
        self.status = AnnounceStatus(info_hash=self.info_hash)


class DhtAnnouncer:
    """The announce loop: which torrents we publish, and how often.

    Args:
        node: The DHT node to publish through. Mutable, because a session
            starts its torrents before it knows whether the DHT will bind; set
            it (or :meth:`attach`) once the node is up.
        interval: Seconds between passes.
        retry_interval: Seconds before retrying a torrent that was skipped
            because it had no port yet. See :data:`SKIPPED_RETRY_INTERVAL`.
        event_bus: Where progress and failures are reported.
        now: Clock, injectable so a test can age an interval out instantly.

    The loop is deliberately dumb: one pass over the registrations, in
    insertion order, awaiting each announce so a slow lookup cannot pile up
    behind a fast one.
    """

    def __init__(
        self,
        node: AnnouncingNode | None = None,
        *,
        interval: float = DEFAULT_ANNOUNCE_INTERVAL,
        retry_interval: float = SKIPPED_RETRY_INTERVAL,
        event_bus: EventBus | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval < MIN_ANNOUNCE_INTERVAL:
            raise ValueError(f"announce interval must be >= {MIN_ANNOUNCE_INTERVAL}s")
        self.node = node
        self.interval = interval
        self.retry_interval = retry_interval
        self.event_bus = event_bus
        self._now = now
        self._wake = asyncio.Event()
        self._registrations: dict[bytes, _Registration] = {}
        self._task: asyncio.Task[None] | None = None
        self._quiet = False

    # ------------------------------------------------------------------ reading

    @property
    def registered(self) -> tuple[bytes, ...]:
        """Every info hash we are publishing for, in registration order."""
        return tuple(self._registrations)

    @property
    def published(self) -> int:
        """How many torrents some node currently has a record of.

        Counted from the last pass's outcome, not from what we asked for: an
        announce that found nobody is not a publication.
        """
        return sum(1 for item in self._registrations.values() if item.status.published)

    @property
    def running(self) -> bool:
        """Whether the periodic loop is alive."""
        return self._task is not None and not self._task.done()

    @property
    def retry_soon(self) -> bool:
        """Whether the next wait is the short one.

        True when a torrent was skipped for want of a listening port — a
        condition that fixes itself the moment its engine binds — rather than for
        a reason that will never change.
        """
        return any(
            registration.status.skipped == NOT_LISTENING
            for registration in self._registrations.values()
        )

    def status(self, info_hash: bytes) -> AnnounceStatus | None:
        """What the last pass did for one torrent, or None if it is not ours."""
        registration = self._registrations.get(info_hash)
        return None if registration is None else registration.status

    def statuses(self) -> tuple[AnnounceStatus, ...]:
        """Every torrent's last outcome, in registration order."""
        return tuple(item.status for item in self._registrations.values())

    # -------------------------------------------------------------- registering

    def attach(self, node: AnnouncingNode | None) -> None:
        """Point the loop at a node, or away from one. Safe to call twice."""
        self.node = node

    def register(
        self,
        info_hash: bytes,
        port: Callable[[], int] | int,
        *,
        private: bool = False,
    ) -> bool:
        """Publish this torrent from now on. Returns whether it was new.

        Args:
            info_hash: The torrent to publish.
            port: Our TCP listen port, read at announce time. A callable
                because the engine does not know its port until it binds, and
                ``0`` at that moment means "not listening yet".
            private: BEP 27's flag. A private torrent is never published: the
                flag means the swarm is tracker-only, and a DHT entry would
                leak it to clients the tracker cannot see.
        """
        if info_hash in self._registrations:
            return False
        getter = (lambda: port) if isinstance(port, int) else port
        self._registrations[info_hash] = _Registration(
            info_hash=info_hash, port=getter, private=private
        )
        return True

    def unregister(self, info_hash: bytes) -> bool:
        """Stop publishing this torrent. Returns whether it was registered.

        BEP 5 has no retraction, so the entries already on remote nodes stay
        until those nodes' own TTLs expire them. Stopping here means we stop
        refreshing them, which is the only thing a client can do.
        """
        return self._registrations.pop(info_hash, None) is not None

    # ------------------------------------------------------------------ announcing

    async def announce_once(self) -> int:
        """One pass over every registration. Returns how many nodes accepted.

        Called by the loop, and directly by anything that wants an announce now
        rather than at the end of the interval — a torrent added mid-run, for
        instance, should not wait fifteen minutes to be findable.
        """
        if not self._registrations:
            return 0
        if self.node is None or not self.node.bound:
            return 0
        if self._quiet:
            return 0

        accepted = 0
        for registration in tuple(self._registrations.values()):
            accepted += await self._announce_one(registration)
        if accepted:
            self._emit(
                EventType.DHT_ANNOUNCED,
                f"published {self.published} torrent(s) to {accepted} node(s)",
                data={
                    "accepted": accepted,
                    "torrents": self.published,
                    "registered": len(self._registrations),
                },
            )
        return accepted

    @property
    def quiet(self) -> bool:
        """Whether we have stopped announcing ahead of a shutdown."""
        return self._quiet

    def go_quiet(self) -> None:
        """Stop announcing, as BEP 5 asks a node to before it shuts down.

        The protocol wants roughly :data:`SHUTDOWN_QUIET_SECONDS` of silence
        before a node goes away, so the entries it published are not being
        refreshed as it leaves.
        Registrations and the periodic task survive: :meth:`resume_announcing`
        undoes this, which is what a cancelled shutdown needs.
        """
        self._quiet = True

    def resume_announcing(self) -> None:
        """Undo :meth:`go_quiet`."""
        self._quiet = False

    def poke(self) -> None:
        """Ask for a pass now instead of at the end of the interval.

        A torrent added mid-run should be findable promptly rather than up to
        fifteen minutes later. Nothing happens when the loop is not running:
        :meth:`announce_once` is there for callers that want to await a pass.
        """
        self._wake.set()

    async def run(self) -> None:
        """Announce, then wait to be poked or for the interval, forever."""
        while True:
            # Cleared *before* the pass, not after: a torrent registered while a
            # pass is running pokes us, and clearing on the way into the wait
            # would throw that poke away and leave the new torrent unpublished
            # for a whole interval.
            self._wake.clear()
            # Suppressed because one dead node must not end the loop. Cancellation
            # is not caught: a stopped task has to stay stopped.
            with contextlib.suppress(Exception):
                await self.announce_once()
            await self._wait()

    async def _wait(self) -> None:
        """Sleep out the interval, returning early if :meth:`poke` fires.

        The interval shortens to :attr:`retry_interval` while any torrent is
        unpublished for a reason that can change, so a client that started before
        its listener bound does not stay invisible for fifteen minutes.
        """
        timeout = self.retry_interval if self.retry_soon else self.interval
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)

    def start(self) -> asyncio.Task[None]:
        """Begin the periodic loop. Returns the task, so a caller can await it."""
        if self.running:
            return self._task  # type: ignore[return-value]
        self._task = asyncio.create_task(self.run(), name="dht-announce")
        return self._task

    async def stop(self) -> None:
        """Stop the loop. Registrations survive, so the node can be re-attached."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def aclose(self) -> None:
        """Stop the loop and forget every registration."""
        await self.stop()
        self._registrations.clear()
        self.node = None

    # ---------------------------------------------------------------- internals

    async def _announce_one(self, registration: _Registration) -> int:
        """Publish one torrent, recording what happened either way."""
        status = registration.status
        status.attempts += 1
        status.last_attempt = self._now()

        if registration.private:
            status.skipped = PRIVATE
            status.announced = 0
            return 0

        port = registration.port()
        if port <= 0:
            # Nothing to be reached on. Announcing would hand out a socket that
            # refuses connections, which is worse than not being found.
            status.skipped = NOT_LISTENING
            status.announced = 0
            return 0

        status.skipped = ""
        assert self.node is not None  # announce_once checked
        try:
            # implied_port=False: we have a real TCP port, so we publish that
            # rather than asking the node to record our UDP source port.
            accepted = await self.node.announce_peer(
                registration.info_hash, port=port, implied_port=False
            )
        except DhtError as exc:
            status.failures += 1
            status.last_error = str(exc)
            status.announced = 0
            self._emit(
                EventType.DHT_FAILED,
                f"DHT announce for {status.hex_info_hash[:8]} failed: {exc}",
                torrent_id=status.hex_info_hash,
                level=logging.WARNING,
            )
            logger.warning("dht announce failed for %s: %s", status.hex_info_hash[:8], exc)
            return 0
        except asyncio.CancelledError:
            raise

        status.announced = accepted
        status.last_error = ""
        logger.info(
            "announced %s to %d node(s) on port %d", status.hex_info_hash[:8], accepted, port
        )
        return accepted

    def _emit(
        self,
        event_type: EventType,
        message: str,
        torrent_id: str = "",
        data: dict[str, object] | None = None,
        *,
        level: int = logging.INFO,
    ) -> None:
        if self.event_bus is None:
            return
        self.event_bus.emit(
            make_event(event_type, message=message, torrent_id=torrent_id, level=level, data=data)
        )


__all__ = [
    "DEFAULT_ANNOUNCE_INTERVAL",
    "MIN_ANNOUNCE_INTERVAL",
    "NOT_LISTENING",
    "PRIVATE",
    "SHUTDOWN_QUIET_SECONDS",
    "SKIPPED_RETRY_INTERVAL",
    "AnnounceStatus",
    "AnnouncingNode",
    "DhtAnnouncer",
]
