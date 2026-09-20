"""Piece hashing and verification (FR-09, TRD §28).

The rule this module exists to enforce is the client's central integrity
invariant: **a piece is only ever written to disk once ``SHA1(piece)`` matches
the hash from the metainfo.** Everything else — which peer sent it, how fast,
in what order — is someone else's problem.

Two things make it more than a ``hashlib`` call:

**Hashing runs in threads.** A 4 MiB piece takes roughly 10 ms to hash. At 50
peers that is half a second of event-loop stall per second of traffic, so the
work goes to a small, bounded :class:`~concurrent.futures.ThreadPoolExecutor`
and the loop keeps serving sockets while a piece is checked.

**Failure is data, not an exception.** :meth:`PieceVerifier.verify` answers a
question with a boolean; a mismatch is an expected outcome on a hostile
network, not an error path. Only the storage manager turns that answer into an
exception, at the point where bad data would otherwise reach the disk.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import logging
import weakref
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from app.core.constants import DEFAULT_HASH_WORKERS

logger = logging.getLogger(__name__)

# A piece is the unit of integrity, so this is the SHA-1 of a whole piece —
# never of a single block, which is only a transfer unit.
_HASH_NAME: Final[str] = "sha1"

_LIVE_EXECUTORS: weakref.WeakSet[ThreadPoolExecutor] = weakref.WeakSet()
"""Executors this module created and that are still open.

A thread pool's workers are not daemons, so a verifier that is never closed
would keep the interpreter alive at exit, waiting on threads that nobody will
ever wake. This is the safety net for that mistake: closing is still the
caller's job, but forgetting it must not hang the process.
"""


def _shutdown_live_executors() -> None:
    """Release every executor we created, at interpreter shutdown."""
    for executor in list(_LIVE_EXECUTORS):
        executor.shutdown(wait=False, cancel_futures=True)
    _LIVE_EXECUTORS.clear()


atexit.register(_shutdown_live_executors)


def hash_piece(data: bytes | bytearray | memoryview) -> bytes:
    """Return the SHA-1 digest of a piece.

    Args:
        data: Exactly the bytes of one piece (last piece may be short).

    Returns:
        The raw 20-byte digest.
    """
    return hashlib.sha1(data).digest()


def hash_matches(data: bytes | bytearray | memoryview, expected: bytes) -> bool:
    """Compare a piece against its expected hash.

    Args:
        data: The piece data.
        expected: The 20-byte hash from the metainfo.

    Returns:
        True when the piece is exactly what the torrent promised. A different
        length is a mismatch, since the digest already covers the length.
    """
    return hash_piece(data) == expected


class PieceVerifier:
    """Verifies pieces off the event loop, with a bounded number of threads.

    Args:
        workers: Maximum concurrent hashes. Two is plenty: SHA-1 releases the
            GIL, and the point is to keep the loop free, not to parallelise.
        executor: Optional executor to reuse (tests inject their own).

    Example:
        >>> verifier = PieceVerifier(workers=2)            # doctest: +SKIP
        >>> await verifier.verify(data, expected_hash)     # doctest: +SKIP
        True
    """

    def __init__(
        self,
        *,
        workers: int = DEFAULT_HASH_WORKERS,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError(f"workers must be at least 1, got {workers}")
        self._workers = workers
        self._executor = executor
        self._owns_executor = executor is None

    @property
    def workers(self) -> int:
        """Maximum number of concurrent hashes."""
        return self._workers

    def _executor_or_create(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._workers, thread_name_prefix="sha1"
            )
            _LIVE_EXECUTORS.add(self._executor)
            self._owns_executor = True
        return self._executor

    async def verify(self, data: bytes | bytearray | memoryview, expected: bytes) -> bool:
        """Check one piece against its expected hash.

        Args:
            data: The piece data.
            expected: The 20-byte hash from the metainfo.

        Returns:
            True if the piece is valid.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor_or_create(), hash_matches, data, expected)

    async def verify_many(
        self, pieces: Sequence[tuple[int, bytes | bytearray | memoryview, bytes]]
    ) -> list[tuple[int, bool]]:
        """Verify several pieces, hashing them concurrently.

        Used by resume: after a restart every piece marked complete has to be
        re-hashed, and doing that one at a time on a large torrent is slow.

        Args:
            pieces: ``(index, data, expected_hash)`` triples.

        Returns:
            ``(index, valid)`` pairs in the order given.
        """
        if not pieces:
            return []
        loop = asyncio.get_running_loop()
        executor = self._executor_or_create()

        async def check(
            item: tuple[int, bytes | bytearray | memoryview, bytes],
        ) -> tuple[int, bool]:
            index, data, expected = item
            valid = await loop.run_in_executor(executor, hash_matches, data, expected)
            return index, valid

        results = await asyncio.gather(*(check(item) for item in pieces))
        return list(results)

    async def aclose(self) -> None:
        """Shut down the executor, if this verifier created it.

        Idempotent: closing twice does nothing. An executor that was injected
        belongs to whoever passed it in and is left alone.
        """
        if self._executor is not None and self._owns_executor:
            executor, self._executor = self._executor, None
            _LIVE_EXECUTORS.discard(executor)
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=False)
