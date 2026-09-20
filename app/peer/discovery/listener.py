"""Incoming peer connections (PRD Phase 5).

Everything until now dialled out: the client asked a tracker for peers and
opened a socket to each one. That is only half a conversation. A torrent with
no incoming connections cannot be seeded to anyone who was not told about us
in time, and a swarm where every client only dials out is a swarm that stops
growing the moment the tracker goes away.

This module is the other half: a TCP listener that accepts peers who dial
*us*, hands each socket to the peer manager, and gets out of the way.

Two things are enforced here, both of them boring and both of them necessary:

* **Slot discipline.** An accepted socket is not a connection. If there is no
  free slot, the socket is closed immediately — accepting it and then queueing
  it would let a stranger hold a file descriptor open for as long as they like.
* **One place that answers.** The listener does not track connections, does
  not read messages and does not know about torrents. It accepts, lets the
  peer manager validate the handshake, and forgets. The peer manager owns
  everything after that, so incoming and outgoing peers are indistinguishable
  from then on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace

from app.peer.discovery.peer_manager import PeerManager

logger = logging.getLogger(__name__)

DEFAULT_LISTEN_BACKLOG: int = 32


@dataclass(frozen=True, slots=True)
class ListenerStats:
    """What the listener has seen.

    Attributes:
        accepted: Sockets taken in and handed to the peer manager.
        refused: Sockets closed without a connection — no free slot, a failed
            handshake, or a peer we were already talking to.
        handshake_failures: Refusals that were the peer's fault, counted
            separately because "many peers are asking for the wrong torrent"
            is a real signal and not the same as "we are full".
        errors: Sockets that failed in a way not worth classifying further.
    """

    accepted: int = 0
    refused: int = 0
    handshake_failures: int = 0
    errors: int = 0

    @property
    def total(self) -> int:
        """Every socket that reached the listener."""
        return self.accepted + self.refused + self.errors


class PeerListener:
    """Accepts peers that connect to us.

    Args:
        peers: The peer manager each accepted socket is handed to. It owns the
            connection from the handshake onwards.
        host: Interface to bind. ``127.0.0.1`` by default, because a desktop
            client on an untrusted network should not publish a port to the
            world until the user asks it to.
        port: Port to bind; ``0`` (the default) picks a free one, which is
            what tests and ephemeral clients want.
        backlog: How many half-open sockets the kernel may queue.

    Example:
        >>> listener = PeerListener(peers, port=6881)   # doctest: +SKIP
        >>> port = await listener.start()               # doctest: +SKIP
        >>> ...                                         # doctest: +SKIP
        >>> await listener.stop()                       # doctest: +SKIP
    """

    def __init__(
        self,
        peers: PeerManager,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        backlog: int = DEFAULT_LISTEN_BACKLOG,
    ) -> None:
        self._peers = peers
        self._host = host
        self._port = port
        self._backlog = backlog

        self._server: asyncio.Server | None = None
        self._accepting: set[asyncio.Task[None]] = set()
        self._stats = ListenerStats()

    # ------------------------------------------------------------------ state

    @property
    def port(self) -> int:
        """The port we are listening on, once started."""
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def host(self) -> str:
        """The interface we are bound to."""
        return self._host

    @property
    def listening(self) -> bool:
        """Whether the listener is accepting connections."""
        return self._server is not None and self._server.is_serving()

    @property
    def stats(self) -> ListenerStats:
        """What the listener has seen so far."""
        return self._stats

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> int:
        """Bind the socket and begin accepting.

        Returns:
            The port that was bound, which matters when port ``0`` was asked
            for: it is the number to announce to a tracker.
        """
        if self._server is not None:
            return self.port
        self._server = await asyncio.start_server(
            self._on_connect,
            host=self._host,
            port=self._port,
            backlog=self._backlog,
        )
        logger.info("listening for incoming peers on %s:%d", self._host, self.port)
        return self.port

    async def stop(self) -> None:
        """Stop accepting and wait for in-flight handshakes to settle."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            # Not wait_closed(): since 3.12 that waits for every accepted
            # connection to drop as well, and connections we adopted belong to
            # the peer manager now. Waiting for them here would deadlock
            # anyone who stops the listener before they stop the swarm.
        if self._accepting:
            await asyncio.gather(*self._accepting, return_exceptions=True)

    async def __aenter__(self) -> PeerListener:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # ---------------------------------------------------------------- internals

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle one accepted socket: hand it over in its own task.

        Adopting happens concurrently so a peer that connects and then says
        nothing cannot block the next peer behind it.
        """
        task = asyncio.create_task(self._adopt(reader, writer))
        self._accepting.add(task)
        task.add_done_callback(self._accepting.discard)

    async def _adopt(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Hand a socket to the peer manager, or close it."""
        full = len(self._peers.connections) >= self._peers.max_connections
        try:
            connection = await self._peers.adopt(reader, writer)
        except Exception as exc:  # noqa: BLE001 - a hostile socket must not kill us
            logger.debug("incoming connection failed: %s", exc)
            self._stats = replace(self._stats, errors=self._stats.errors + 1)
            await _close_quietly(writer)
            return

        if connection is not None:
            self._stats = replace(self._stats, accepted=self._stats.accepted + 1)
            return

        # No slot means we were simply full; with a slot free, a refusal is the
        # peer's fault: wrong torrent, silent, or already connected.
        self._stats = replace(
            self._stats,
            refused=self._stats.refused + 1,
            handshake_failures=self._stats.handshake_failures + (0 if full else 1),
        )
        await _close_quietly(writer)


async def _close_quietly(writer: asyncio.StreamWriter) -> None:
    """Close a socket without letting a broken pipe become our problem."""
    with contextlib.suppress(OSError, asyncio.CancelledError):
        writer.close()
        await writer.wait_closed()
