"""Download engine: piece/block bookkeeping, selection and scheduling.

The engine turns a swarm into bytes on disk. It is deliberately split so each
decision lives in one place:

* :mod:`~app.download.block` and :mod:`~app.download.piece` — state machines.
  Blocks are the unit of transfer, pieces the unit of integrity.
* :mod:`~app.download.selector` — *which* piece to want next (sequential,
  rarest-first, random).
* :mod:`~app.download.endgame` — racing the last blocks so one slow peer
  cannot decide the download's finish time.
* :mod:`~app.download.scheduler` — *who* fetches *which* block, and how a
  request is recovered when its peer disappears.
* :mod:`~app.download.manager` — :class:`DownloadManager`, which wires all of
  the above to the peer manager (M5) and storage (M6).

Quick start::

    manager = DownloadManager(torrent, storage=storage, peers=peers)
    await manager.start()
    await manager.wait_until_complete(timeout=120)

Exports:
    DownloadManager, DownloadStats    — the engine itself and its counters
    Scheduler, Assignment, PeerSlot   — request planning and pipelining
    PieceSelector, EndgameTracker     — what to want, and when to race
    Piece, PieceState, build_pieces   — the integrity unit
    Block, BlockState, plan_blocks    — the transfer unit
"""

from __future__ import annotations

from app.download.block import Block, BlockKey, BlockState, block_at, plan_blocks
from app.download.endgame import EndgameTracker
from app.download.manager import DownloadManager, DownloadStats
from app.download.piece import Piece, PieceState, build_pieces
from app.download.scheduler import Assignment, PeerSlot, Scheduler, peer_key
from app.download.selector import PieceSelector

__all__ = [
    "Assignment",
    "Block",
    "BlockKey",
    "BlockState",
    "DownloadManager",
    "DownloadStats",
    "EndgameTracker",
    "PeerSlot",
    "Piece",
    "PieceSelector",
    "PieceState",
    "Scheduler",
    "block_at",
    "build_pieces",
    "peer_key",
    "plan_blocks",
]
