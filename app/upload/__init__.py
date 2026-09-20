"""Upload engine: serving blocks, and deciding who has earned them (PRD FR-13/14).

A client that only downloads is a leech, and the protocol's answer to leeches
is choking: every peer decides for itself who it serves, and reciprocity is
what makes the swarm work. This package is that half of the client.

It is split the same way the download engine is, so every decision has one
home:

* :mod:`~app.upload.choke` — the policy. Pure: it is handed what every peer
  has done and returns who deserves a slot.
* :mod:`~app.upload.rate` — the pacer, so seeding does not crowd out the
  user's own connection.
* :mod:`~app.upload.manager` — :class:`UploadManager`, which validates
  requests, queues them, reads them off disk, sends them, and counts
  everything.

Quick start::

    upload = UploadManager(torrent, storage=storage, peers=peers)
    peers.on_request = upload.on_request
    peers.on_cancel = upload.on_cancel
    await upload.start()

Exports:
    UploadManager, UploadStats, QueuedRequest   — serving and its counters
    ChokePolicy, ChokeDecision, PeerAccounting  — who we upload to, and why
    TokenBucket                                 — pacing
"""

from __future__ import annotations

from app.upload.choke import ChokeDecision, ChokePolicy, PeerAccounting
from app.upload.manager import QueuedRequest, UploadManager, UploadStats
from app.upload.rate import TokenBucket

__all__ = [
    "ChokeDecision",
    "ChokePolicy",
    "PeerAccounting",
    "QueuedRequest",
    "TokenBucket",
    "UploadManager",
    "UploadStats",
]
