"""Storage and filesystem exceptions (TRD §28, §29).

The split follows what a caller can *do* about the failure:

* :class:`StoragePathError` is **terminal for that torrent**. A file path that
  escapes the download directory is not a transient condition — it means the
  torrent's metadata is hostile, and the torrent must not be started.
* :class:`AllocationError` is **environmental**: the disk is full, the
  filesystem refuses the size, or a parent directory is not writable. Retrying
  later, or with preallocation off, may well succeed.
* :class:`PieceHashMismatch` is **normal-ish and recoverable**: a piece arrived
  and did not match its hash. The data is discarded and the piece is requested
  again, exactly as FR-09 requires.
* :class:`ResumeError` means our own state file is unreadable. Nothing about
  the torrent is wrong, and starting fresh is a perfectly good answer — the
  corrupt file is quarantined rather than deleted so the user can look at it.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for every storage-layer failure."""


class StoragePathError(StorageError, ValueError):
    """A path derived from torrent metadata is not safe to write to disk.

    Subclasses :class:`ValueError` as well, so generic filesystem code can
    catch it alongside the usual ``ValueError`` from :mod:`pathlib`.
    """


class AllocationError(StorageError):
    """Files could not be created or preallocated (disk full, permissions)."""


class PieceHashMismatch(StorageError):
    """A piece did not match the SHA-1 declared in the metainfo.

    Attributes:
        index: Piece index that failed verification.
        expected: Hash from the torrent metainfo (hex).
        actual: Hash of the data we received (hex).
    """

    def __init__(self, index: int, *, expected: bytes, actual: bytes) -> None:
        super().__init__(
            f"piece {index} failed verification: expected {expected.hex()}, got {actual.hex()}"
        )
        self.index = index
        self.expected = expected
        self.actual = actual


class ResumeError(StorageError):
    """Resume state could not be read, or does not match the torrent."""
