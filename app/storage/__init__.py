"""Storage: file mapping, buffered IO, allocation and resume state.

The torrent is one byte stream; this package is where that stream becomes
files on disk. Its central rule is that **data is only written once its SHA-1
matches the metainfo**, so a corrupt or hostile piece can never reach a file.

Quick start::

    from pathlib import Path
    from app.storage import StorageManager

    storage = StorageManager(torrent, Path("/tmp/downloads"))
    await storage.prepare()                 # create + preallocate files
    await storage.write_piece(0, piece)     # hash-checked, then written
    await storage.save_resume()             # remember progress for a restart

Exports:
    StorageManager, VerificationReport        — the interface the client uses
    FileLayout, MappedFile, AllocationResult  — byte stream → files
    PieceVerifier, hash_piece, hash_matches   — SHA-1 integrity
    ResumeState, ResumeStore                  — persisted progress
    StorageError and subclasses               — error types
"""

from __future__ import annotations

from app.storage.errors import (
    AllocationError,
    PieceHashMismatch,
    ResumeError,
    StorageError,
    StoragePathError,
)
from app.storage.files import AllocationResult, FileLayout, MappedFile
from app.storage.manager import StorageManager, VerificationReport
from app.storage.resume import ResumeState, ResumeStore
from app.storage.verify import PieceVerifier, hash_matches, hash_piece

__all__ = [
    "AllocationError",
    "AllocationResult",
    "FileLayout",
    "MappedFile",
    "PieceHashMismatch",
    "PieceVerifier",
    "ResumeError",
    "ResumeState",
    "ResumeStore",
    "StorageError",
    "StorageManager",
    "StoragePathError",
    "VerificationReport",
    "hash_matches",
    "hash_piece",
]
