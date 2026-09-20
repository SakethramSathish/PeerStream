"""Per-torrent facade: start, pause, resume, remove, inspect.

The UI and the CLI never touch :class:`~app.services.engine.Engine` methods
that could leave a torrent half-running. They talk to this, which is a smaller
and safer surface: ask for a state, ask for a snapshot, ask to start or stop.

Two rules shape the API:

* **Every action is idempotent.** Starting a started torrent, pausing a paused
  one, or removing twice are all no-ops. A button can be double-clicked; a
  torrent must not be double-started.
* **Nothing here blocks on the network.** Waiting for completion is an
  explicit, awaited call — the facade never turns a click into a stall.

"Remove" deserves a note: it stops the torrent and *asks* whether the data
should go too, because deleting someone's files as a side effect of a UI action
is not a thing this client does silently.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.event_bus import EventBus
from app.download.piece import Piece, PieceState
from app.peer.bitfield import Bitfield
from app.services.engine import Engine, ResumeSummary, TorrentState
from app.statistics.metrics import MetricsSnapshot
from app.torrent import Torrent
from app.tracker.base import TrackerStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TorrentView:
    """One torrent, as something that wants to display it sees it.

    Everything here is either measured or counted. Where a number is not
    available yet it is zero or ``None``, never a guess.
    """

    info_hash: str
    name: str
    state: TorrentState
    progress: float
    total_length: int
    verified_pieces: int
    missing_pieces: int
    piece_count: int
    port: int
    resumed: ResumeSummary | None = None
    """What a previous run left on disk, or ``None`` when the torrent did not
    say — which is not the same as saying it started from nothing."""

    error: str | None = None
    metrics: MetricsSnapshot | None = None

    @property
    def active(self) -> bool:
        """Whether this torrent is doing anything at all."""
        return self.state in {
            TorrentState.STARTING,
            TorrentState.DOWNLOADING,
            TorrentState.SEEDING,
        }

    def as_dict(self) -> dict[str, object]:
        """A plain, JSON-friendly view — the shape the UI binds to."""
        return {
            "info_hash": self.info_hash,
            "name": self.name,
            "state": self.state.value,
            "progress": round(self.progress, 5),
            "total_length": self.total_length,
            "verified_pieces": self.verified_pieces,
            "missing_pieces": self.missing_pieces,
            "piece_count": self.piece_count,
            "port": self.port,
            "resumed_pieces": self.resumed.pieces if self.resumed else 0,
            "error": self.error,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class FileView:
    """One file in a torrent, with the part of it that has verified.

    Attributes:
        path: Path relative to the download directory.
        length: Size in bytes (may be zero: padding files exist).
        offset: Where it starts in the torrent's byte stream.
        piece_count: How many pieces it spans.
        verified_pieces: How many of those have verified.
        progress: ``0.0``-``1.0``, by pieces. A zero-length file is complete.
    """

    path: str
    length: int
    offset: int
    piece_count: int = 0
    verified_pieces: int = 0
    progress: float = 0.0

    @property
    def name(self) -> str:
        """The file's name, without the folders above it."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def complete(self) -> bool:
        return self.piece_count == 0 or self.verified_pieces >= self.piece_count


class PieceMapState:
    """The five piece states the interface draws (PRD §10.5), as byte codes.

    The download engine tracks seven (:class:`~app.download.piece.PieceState`);
    the interface shows five, because "downloaded" and "verifying" are both
    "in flight" to a human, and a matrix that lit up two extra colours for a
    millisecond would be noise. The mapping is written down in
    :data:`PIECE_STATE_CODES` rather than implied by a colour.

    The codes are bytes because a piece map for a large torrent is tens of
    thousands of entries: one byte each, in a single immutable ``bytes``, is
    something the UI can copy and draw without allocating an object per piece.
    """

    MISSING: int = 0
    REQUESTED: int = 1
    DOWNLOADING: int = 2
    VERIFIED: int = 3
    FAILED: int = 4


PIECE_STATE_CODES: dict[PieceState, int] = {
    PieceState.MISSING: PieceMapState.MISSING,
    PieceState.REQUESTED: PieceMapState.REQUESTED,
    PieceState.DOWNLOADING: PieceMapState.DOWNLOADING,
    PieceState.DOWNLOADED: PieceMapState.DOWNLOADING,
    PieceState.VERIFYING: PieceMapState.DOWNLOADING,
    PieceState.VERIFIED: PieceMapState.VERIFIED,
    PieceState.FAILED: PieceMapState.FAILED,
}

PIECE_STATE_NAMES: tuple[str, ...] = ("missing", "requested", "downloading", "verified", "failed")


@dataclass(frozen=True, slots=True)
class PeerView:
    """One peer, as the swarm canvas and the peers table see it.

    Everything here is counted or measured by the connection itself. Rates are
    *not* included: a peer's rate is the difference between two reads, and the
    reading layer (the UI's view model) is the right place to difference them,
    with the clock it is already holding.

    Attributes:
        key: ``"host:port"``, the peer's identity in the UI's dictionaries.
        host / port: Where it is.
        client: What it said it was, from its peer id.
        state: Lifecycle position: connecting, handshaking, connected, closing.
        source: How we found it: tracker, incoming, pex, manual.
        downloaded / uploaded: Bytes moved with this peer, cumulative.
        pieces_held: How many of the torrent's pieces it claims.
        piece_count: How many the torrent has.
        choking_us: Whether it refuses to serve us.
        interested_in_us: Whether it wants our pieces.
        we_are_choking / we_are_interested: Our side of the same two flags.
        latency_ms: Round-trip time when we have measured one.
        idle_for: Seconds since it last said anything.
        blocks_in_flight: Requests we have outstanding with it.
    """

    key: str
    host: str
    port: int
    client: str = "Unknown"
    state: str = "connecting"
    source: str = ""
    downloaded: int = 0
    uploaded: int = 0
    pieces_held: int = 0
    piece_count: int = 0
    choking_us: bool = True
    interested_in_us: bool = False
    we_are_choking: bool = True
    we_are_interested: bool = False
    latency_ms: float | None = None
    idle_for: float = 0.0
    blocks_in_flight: int = 0

    @property
    def label(self) -> str:
        """How a human names this peer."""
        return f"{self.host}:{self.port}"

    @property
    def complete(self) -> bool:
        """Whether it has the whole torrent, which makes it a seed."""
        return self.piece_count > 0 and self.pieces_held >= self.piece_count

    @property
    def share(self) -> float:
        """How much of the torrent it holds, ``0.0``-``1.0``."""
        if self.piece_count <= 0:
            return 0.0
        return min(1.0, max(0.0, self.pieces_held / self.piece_count))

    @property
    def serving_us(self) -> bool:
        """Whether it is actually willing to send us bytes right now."""
        return self.state == "connected" and not self.choking_us


@dataclass(frozen=True, slots=True)
class PieceMap:
    """Every piece of a torrent, in the shape the matrix draws.

    Parallel arrays rather than a list of objects: a 20,000-piece torrent read
    twice a second should not mean 20,000 allocations twice a second.

    Attributes:
        piece_count: How many pieces the torrent has.
        piece_length: Nominal piece size (the last piece may be shorter).
        total_length: Size of the whole torrent's payload, which is what makes
            the *last* piece's real size knowable rather than assumed.
        states: One :class:`PieceMapState` code per piece.
        availability: How many connected peers hold each piece.
        filled: How much of each piece has arrived, ``0.0``-``1.0``. Zero for
            verified pieces is not a contradiction: a verified piece is done,
            and this array is about pieces still in flight.
        counts: How many pieces are in each state, by name.
        read_at: Monotonic time of the read, so rates can be differenced.
    """

    piece_count: int
    piece_length: int
    total_length: int
    states: bytes
    availability: tuple[int, ...]
    filled: tuple[float, ...]
    counts: dict[str, int]
    read_at: float = 0.0

    def __post_init__(self) -> None:
        if len(self.states) != self.piece_count:
            raise ValueError(
                f"{len(self.states)} piece states for a {self.piece_count}-piece torrent"
            )

    @property
    def verified(self) -> int:
        return self.counts.get("verified", 0)

    @property
    def complete(self) -> bool:
        return self.verified >= self.piece_count

    def state_of(self, index: int) -> int:
        """The state code for one piece."""
        return self.states[index]

    def name_of(self, index: int) -> str:
        """The state name for one piece, for tooltips."""
        return PIECE_STATE_NAMES[self.states[index]]

    def availability_of(self, index: int) -> int:
        return self.availability[index]

    def piece_size(self, index: int) -> int:
        """How many bytes are in one piece.

        The last piece is short — usually much shorter — and pretending
        otherwise overstates how much is left by up to a full piece, which is
        exactly the number a person reads when deciding whether to wait.
        """
        if index != self.piece_count - 1:
            return self.piece_length
        remainder = self.total_length - (self.piece_length * (self.piece_count - 1))
        return max(0, min(self.piece_length, remainder))

    def filled_of(self, index: int) -> float:
        return self.filled[index]


class TorrentService:
    """One torrent, managed.

    Args:
        engine: The engine that does the work.
        event_bus: Optional bus, used to announce removal.

    Example:
        >>> service = TorrentService(engine)     # doctest: +SKIP
        >>> await service.start()                # doctest: +SKIP
        >>> view = service.view()                # doctest: +SKIP
        >>> view.state                           # doctest: +SKIP
        <TorrentState.DOWNLOADING: 'downloading'>
    """

    def __init__(self, engine: Engine, *, event_bus: EventBus | None = None) -> None:
        self._engine = engine
        self._bus = event_bus

    @property
    def engine(self) -> Engine:
        """The engine underneath; for the rare case that needs one."""
        return self._engine

    @property
    def torrent(self) -> Torrent:
        return self._engine.torrent

    @property
    def hex_info_hash(self) -> str:
        return self._engine.hex_info_hash

    # ----------------------------------------------------------------- actions

    async def start(self) -> None:
        """Start (or resume) the torrent."""
        await self._engine.start()

    async def stop(self) -> None:
        """Stop transferring, keeping the data and the torrent."""
        await self._engine.stop()

    async def pause(self) -> None:
        """Stop transferring without letting go of the torrent."""
        await self._engine.pause()

    async def resume(self) -> None:
        """Continue a paused torrent."""
        await self._engine.resume()

    async def remove(self, *, delete_data: bool = False) -> None:
        """Stop the torrent and forget it.

        Args:
            delete_data: Also delete the downloaded files. Off by default: the
                destructive reading of "remove" is not the one a user expects.
        """
        await self._engine.stop()
        if delete_data:
            await self._engine.storage.delete_files()
        await self._engine.aclose()

    async def aclose(self) -> None:
        """Stop and release, without deleting anything."""
        await self._engine.aclose()

    # ------------------------------------------------------------- inspection

    async def wait_until_complete(self, timeout: float | None = None) -> bool:
        """Wait for every piece to verify. Returns ``False`` on timeout."""
        return await self._engine.wait_until_complete(timeout=timeout)

    def view(self) -> TorrentView:
        """A snapshot of what this torrent is doing right now."""
        engine = self._engine
        snapshot = engine.snapshot()
        return TorrentView(
            info_hash=engine.hex_info_hash,
            name=engine.torrent.name,
            state=engine.state,
            progress=engine.progress,
            total_length=engine.torrent.total_length,
            verified_pieces=snapshot.pieces_verified,
            missing_pieces=snapshot.pieces_missing,
            piece_count=engine.torrent.piece_count,
            port=engine.port,
            resumed=engine.resumed,
            error=engine.error,
            metrics=snapshot,
        )

    def snapshot(self) -> MetricsSnapshot:
        """The measured numbers, straight from the collector."""
        return self._engine.snapshot()

    @property
    def download_directory(self) -> Path:
        """Where this torrent's data is being written."""
        return Path(self._engine.storage.root)

    def files_view(self) -> tuple[FileView, ...]:
        """Every file in the torrent, with how much of it has verified.

        Per-file progress is counted, not estimated: a file is made of the
        pieces it overlaps, and a file is as complete as its verified pieces.
        It is reported per file rather than as one number for the torrent
        because "which file is finished" is the question a person asks.
        """
        engine = self._engine
        torrent = engine.torrent
        verified = set(engine.download.verified_pieces)
        piece_length = torrent.piece_length

        views: list[FileView] = []
        for entry in torrent.files:
            first = entry.offset // piece_length
            last = min(torrent.piece_count - 1, max(first, (entry.end_offset - 1) // piece_length))
            total = max(0, last - first + 1)
            done = sum(1 for index in range(first, last + 1) if index in verified)
            views.append(
                FileView(
                    path=str(entry.path),
                    length=entry.length,
                    offset=entry.offset,
                    piece_count=total,
                    verified_pieces=done,
                    progress=(done / total) if total else 1.0,
                )
            )
        return tuple(views)

    def trackers_view(self) -> tuple[TrackerStatus, ...]:
        """Health record for every tracker on this torrent.

        Empty means the torrent has no trackers — a magnet, or a torrent whose
        announce list was empty. It does not mean "not announced yet", which is
        a state each tracker reports for itself.
        """
        manager = self._engine.tracker
        return () if manager is None else tuple(manager.statuses)

    # ------------------------------------------------------------ swarm views

    def peers_view(self) -> tuple[PeerView, ...]:
        """Every peer this torrent knows about, connected or not.

        The candidate list comes first — peers the tracker told us about that we
        have not reached yet — because a swarm is not just the peers we happen
        to be talking to, and a canvas that showed only those would be a lie
        about how big the swarm is.

        This reads live connection state, so it belongs on the engine's loop;
        :meth:`~app.services.session.Session.peers_view` is the coroutine that
        puts it there.
        """
        engine = self._engine
        manager = engine.peers
        piece_count = engine.torrent.piece_count
        now = time.monotonic()

        seen: set[str] = set()
        views: list[PeerView] = []

        for connection in manager.connections:
            session = connection.session
            address = connection.address
            key = f"{address.host}:{address.port}"
            seen.add(key)
            views.append(
                PeerView(
                    key=key,
                    host=address.host,
                    port=address.port,
                    client=session.client,
                    state=session.state.value,
                    source=address.source,
                    downloaded=session.downloaded,
                    uploaded=session.uploaded,
                    pieces_held=_pieces_held(session.bitfield),
                    piece_count=piece_count,
                    choking_us=session.peer_choking,
                    interested_in_us=session.peer_interested,
                    we_are_choking=session.am_choking,
                    we_are_interested=session.am_interested,
                    latency_ms=session.latency_ms,
                    idle_for=now - session.last_activity,
                    blocks_in_flight=_in_flight(engine, key),
                )
            )

        for candidate in manager.candidates:
            address = candidate.address
            key = f"{address.host}:{address.port}"
            if key in seen:
                continue
            seen.add(key)
            views.append(
                PeerView(
                    key=key,
                    host=address.host,
                    port=address.port,
                    state="candidate",
                    source=address.source,
                    piece_count=piece_count,
                    latency_ms=None,
                    idle_for=(now - candidate.last_success) if candidate.last_success else 0.0,
                )
            )
        return tuple(views)

    def piece_map(self) -> PieceMap:
        """Every piece's state, availability and fill, read once.

        Like :meth:`peers_view` this touches live engine state and is meant to
        run on the engine loop.
        """
        engine = self._engine
        manager = engine.download
        pieces = manager.pieces
        availability = manager.availability

        states = bytearray(len(pieces))
        available: list[int] = []
        filled: list[float] = []
        counts = dict.fromkeys(PIECE_STATE_NAMES, 0)

        for piece in pieces:
            code = PIECE_STATE_CODES.get(piece.state, PieceMapState.MISSING)
            states[piece.index] = code
            counts[PIECE_STATE_NAMES[code]] += 1
            available.append(availability.count(piece.index))
            filled.append(_fill_of(piece, code))

        return PieceMap(
            piece_count=len(pieces),
            piece_length=engine.torrent.piece_length,
            total_length=engine.torrent.total_length,
            states=bytes(states),
            availability=tuple(available),
            filled=tuple(filled),
            counts=counts,
            read_at=time.monotonic(),
        )


def _fill_of(piece: Piece, code: int) -> float:
    """How much of a piece has arrived, ``0.0``-``1.0``.

    Only pieces in flight have a meaningful fill: a verified piece is simply
    done, and reporting it as 100 % here would make the matrix draw every
    finished piece as if it were one block from completion.
    """
    if code != PieceMapState.DOWNLOADING:
        return 0.0
    total = piece.block_count
    if total <= 0:
        return 0.0
    return min(1.0, piece.received_blocks / total)


def _pieces_held(bitfield: Bitfield) -> int:
    """How many pieces a peer's bitfield has set."""
    count = bitfield.count
    return count() if callable(count) else int(count)


def _in_flight(engine: Engine, key: str) -> int:
    """Requests outstanding with one peer, or 0 if the scheduler isn't saying.

    A missing number here is not a reason to skip the peer; the canvas draws
    what it knows and leaves the rest alone.
    """
    scheduler = engine.download.scheduler
    counter = getattr(scheduler, "in_flight_for", None)
    if not callable(counter):
        return 0
    try:
        return int(counter(key))
    except (TypeError, ValueError, KeyError):  # pragma: no cover - defensive
        return 0
