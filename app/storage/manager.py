"""Presents a torrent as a single byte stream backed by one or more files.

This is the module the rest of the client talks to. It owns three things:

* **the mapping** — piece index → offsets in one or more files
  (:class:`~app.storage.files.FileLayout`);
* **the integrity rule** — a piece is written only after ``SHA1(piece)``
  matches the metainfo (:class:`~app.storage.verify.PieceVerifier`);
* **the memory of what is done** — which pieces are on disk, persisted as
  resume state (:class:`~app.storage.resume.ResumeStore`).

The interface is deliberately piece-shaped, not block-shaped: blocks are a
transfer detail owned by the download engine (M7), while a piece is the unit
the torrent's hashes speak about.

::

    storage = StorageManager(torrent, Path("/tmp/dl"), config=..., event_bus=bus)
    await storage.prepare()                  # create + preallocate files
    await storage.write_piece(0, data)       # verified, then written
    await storage.verify_completed()         # after a restart: re-check
    await storage.save_resume()              # remember progress

All disk IO runs in worker threads (``asyncio.to_thread``), so a slow or
stalled disk never stops the event loop from serving peers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from app.core.config import StorageConfig
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.storage.errors import PieceHashMismatch, StorageError
from app.storage.files import (
    AllocationResult,
    FileLayout,
    MappedFile,
    create_files,
    file_sizes,
    read_chunk,
    write_chunk,
)
from app.storage.resume import ResumeState, ResumeStore
from app.storage.verify import PieceVerifier, hash_piece
from app.torrent.metadata import Torrent

logger = logging.getLogger(__name__)

# Pieces read from disk per batch during a resume check. Large enough to keep
# the disk and the hash threads busy, small enough that the batch never holds
# more than a few pieces in memory.
VERIFY_BATCH_FACTOR: Final[int] = 2


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Outcome of re-checking pieces that are already on disk.

    Attributes:
        checked: How many pieces were hashed.
        valid: Pieces whose data matches the metainfo.
        invalid: Pieces that must be downloaded again.
        elapsed: Wall-clock seconds the check took.
    """

    checked: int
    valid: tuple[int, ...]
    invalid: tuple[int, ...]
    elapsed: float

    @property
    def all_valid(self) -> bool:
        """True when nothing was rejected."""
        return not self.invalid


class StorageManager:
    """Reads and writes a torrent's byte stream on disk.

    Args:
        torrent: The validated torrent to store.
        directory: Where to write. Defaults to ``config.download_directory``.
        config: Storage settings (preallocation, verify-before-write, state dir).
        event_bus: Optional bus for ``DISK_*`` and ``RESUME_*`` events.
        verifier: Hash worker pool; one is created (and owned) when omitted.
        resume_store: Resume file store; one is created over
            ``config.state_directory`` when omitted.
        added_at: When the torrent was added, recorded in resume state.
    """

    def __init__(
        self,
        torrent: Torrent,
        directory: Path | None = None,
        *,
        config: StorageConfig | None = None,
        event_bus: EventBus | None = None,
        verifier: PieceVerifier | None = None,
        resume_store: ResumeStore | None = None,
        added_at: float | None = None,
    ) -> None:
        self._torrent = torrent
        self._config = config or StorageConfig()
        self._event_bus = event_bus
        self._verifier = verifier or PieceVerifier()
        self._owns_verifier = verifier is None
        self._added_at = time.time() if added_at is None else added_at

        root = Path(directory) if directory is not None else self._config.download_directory
        self._layout = FileLayout.from_torrent(torrent, root)
        self._resume_store = resume_store or ResumeStore(self._config.state_directory)
        self._completed: set[int] = set()

    # ------------------------------------------------------------- properties

    @property
    def torrent(self) -> Torrent:
        """The torrent being stored."""
        return self._torrent

    @property
    def config(self) -> StorageConfig:
        """Storage settings in force."""
        return self._config

    @property
    def layout(self) -> FileLayout:
        """Mapping from the byte stream onto files."""
        return self._layout

    @property
    def root(self) -> Path:
        """Directory every file lives under."""
        return self._layout.root

    @property
    def files(self) -> tuple[MappedFile, ...]:
        """Files in stream order."""
        return self._layout.files

    @property
    def piece_count(self) -> int:
        """Number of pieces in the torrent."""
        return self._torrent.piece_count

    @property
    def piece_length(self) -> int:
        """Nominal piece size in bytes."""
        return self._torrent.piece_length

    @property
    def total_length(self) -> int:
        """Total size of the byte stream."""
        return self._torrent.total_length

    @property
    def completed_pieces(self) -> tuple[int, ...]:
        """Pieces known to be on disk and verified, ascending."""
        return tuple(sorted(self._completed))

    @property
    def missing_pieces(self) -> tuple[int, ...]:
        """Pieces still to download, ascending."""
        done = self._completed
        return tuple(index for index in range(self.piece_count) if index not in done)

    @property
    def downloaded_bytes(self) -> int:
        """Bytes of verified piece data on disk (last piece counted correctly)."""
        return sum(self._torrent.piece_size(index) for index in self._completed)

    @property
    def progress(self) -> float:
        """Fraction of the torrent's bytes that are verified, in ``[0.0, 1.0]``."""
        if self.total_length == 0:
            return 1.0 if self.piece_count == 0 else 0.0
        return self.downloaded_bytes / self.total_length

    @property
    def complete(self) -> bool:
        """True when every piece is on disk."""
        return len(self._completed) == self.piece_count

    @property
    def resume_store(self) -> ResumeStore:
        """The resume file store backing this download."""
        return self._resume_store

    # -------------------------------------------------------------- lifecycle

    async def prepare(self, *, preallocate: bool | None = None) -> AllocationResult:
        """Create directories and files, optionally reserving the full size.

        Args:
            preallocate: Override ``config.preallocate_files``.

        Returns:
            How much was reserved. ``bytes_allocated`` is zero when resuming
            into files that already have their final size.

        Raises:
            app.storage.errors.AllocationError: If the disk refuses the files.
        """
        should_preallocate = self._config.preallocate_files if preallocate is None else preallocate
        result = await asyncio.to_thread(create_files, self._layout, preallocate=should_preallocate)
        self._emit(
            EventType.DISK_ALLOCATED,
            f"allocated {result.total_bytes} bytes across {result.file_count} file(s)"
            + ("" if result.preallocated else " (preallocation disabled)"),
            data={
                "bytes_allocated": result.bytes_allocated,
                "file_count": result.file_count,
                "preallocated": result.preallocated,
                "total_bytes": result.total_bytes,
            },
        )
        return result

    async def delete_files(self) -> int:
        """Delete the torrent's files and any directory they left empty.

        Only paths inside this storage's root are touched, and the root itself
        is kept: a shared download directory belongs to the user, not to one
        torrent. Padding files created for alignment are deleted too.

        Returns:
            How many files were removed. Files that were already gone are not
            counted — removal is about the result, not the attempt.
        """
        removed = await asyncio.to_thread(self._delete_files)
        self._completed = set()
        self._emit(
            EventType.DISK_DELETED,
            f"deleted {removed} file(s) under {self._layout.root}",
            data={"files": removed, "root": str(self._layout.root)},
        )
        return removed

    def _delete_files(self) -> int:
        """Blocking half of :meth:`delete_files`."""
        root = self._layout.root.resolve()
        removed = 0
        directories: set[Path] = set()
        for mapped in self._layout.files:
            target = mapped.path.resolve()
            if target != root and root not in target.parents:
                # Not ours: a layout that points outside the root is a bug or
                # an attack, and deleting it would be unforgivable.
                logger.warning("refusing to delete %s: outside %s", target, root)
                continue
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
                removed += 1
            directories.add(target.parent)
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            with contextlib.suppress(OSError):
                if directory != root and directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
        return removed

    async def aclose(self) -> None:
        """Release the verifier's threads, if this manager created them."""
        if self._owns_verifier:
            await self._verifier.aclose()

    # ---------------------------------------------------------------- writing

    async def write_piece(
        self,
        index: int,
        data: bytes | bytearray | memoryview,
        *,
        verify: bool | None = None,
    ) -> int:
        """Verify a piece against the metainfo, then write it to disk.

        This is the enforcement point for the client's integrity rule: data
        that does not hash to what the torrent promised is rejected *before* it
        reaches a file, so a corrupt or hostile piece can never displace good
        data. The mismatch is reported as
        :class:`~app.storage.errors.PieceHashMismatch` — an expected,
        recoverable event, not a crash.

        Args:
            index: Piece index.
            data: The piece's bytes; must be exactly :meth:`piece_size` long.
            verify: Override ``config.verify_before_write``.

        Returns:
            Number of bytes written.

        Raises:
            ValueError: If ``index`` is out of range or the data is the wrong
                size for that piece.
            PieceHashMismatch: If verification is on and the hash differs.
            app.storage.errors.AllocationError: If the disk write fails.
        """
        expected_length = self._torrent.piece_size(index)
        view = memoryview(data)
        if len(view) != expected_length:
            raise ValueError(f"piece {index} must be {expected_length} bytes, got {len(view)}")

        should_verify = self._config.verify_before_write if verify is None else verify
        if should_verify:
            expected_hash = self._torrent.piece_hash(index)
            if not await self._verifier.verify(view, expected_hash):
                self._emit(
                    EventType.PIECE_FAILED,
                    f"piece {index} rejected: hash mismatch",
                    level=logging.WARNING,
                    data={"index": index, "expected": expected_hash.hex()},
                )
                raise PieceHashMismatch(index, expected=expected_hash, actual=hash_piece(view))

        written = await self._write_stream(self._torrent.piece_offset(index), view)
        self._completed.add(index)
        self._emit(
            EventType.DISK_WRITE,
            f"piece {index} written ({written} bytes)",
            data={"index": index, "bytes": written},
        )
        return written

    # ---------------------------------------------------------------- reading

    async def read_piece(self, index: int) -> bytes:
        """Read a whole verified piece back off disk.

        Args:
            index: Piece index.

        Returns:
            The piece's bytes.

        Raises:
            StorageError: If the files are shorter than the torrent declares —
                the data is missing, and inventing it would be worse than
                reporting the shortfall.
        """
        size = self._torrent.piece_size(index)
        data = await self._read_stream(self._torrent.piece_offset(index), size)
        if len(data) != size:
            raise StorageError(f"piece {index} is incomplete on disk: {len(data)} of {size} bytes")
        return data

    async def read_block(self, index: int, begin: int, length: int) -> bytes:
        """Read one block out of a piece — the unit an upload serves (M8).

        Args:
            index: Piece index.
            begin: Offset within the piece.
            length: Number of bytes.

        Returns:
            The block's bytes.

        Raises:
            ValueError: If the range is not inside the piece.
            StorageError: If the data is not on disk.
        """
        piece_size = self._torrent.piece_size(index)
        if begin < 0 or length < 0 or begin + length > piece_size:
            raise ValueError(
                f"block {begin}..{begin + length} is outside piece {index} ({piece_size} bytes)"
            )
        offset = self._torrent.piece_offset(index) + begin
        data = await self._read_stream(offset, length)
        if len(data) != length:
            raise StorageError(f"block {begin}..{begin + length} of piece {index} is not on disk")
        return data

    # ----------------------------------------------------------- verification

    async def verify_piece(self, index: int) -> bool:
        """Re-hash one piece on disk against the metainfo.

        Args:
            index: Piece index.

        Returns:
            True if the bytes on disk are exactly what the torrent promised.
            A missing or short file is simply invalid, not an error.
        """
        size = self._torrent.piece_size(index)
        data = await self._read_stream(self._torrent.piece_offset(index), size)
        if len(data) != size:
            return False
        return await self._verifier.verify(data, self._torrent.piece_hash(index))

    async def verify_completed(self, indices: Iterable[int] | None = None) -> VerificationReport:
        """Re-check every piece we believe is finished.

        Run after a restart, or when a file may have been touched outside the
        client: resume state is a hint, and this is what turns the hint back
        into fact. Pieces that fail are dropped from the completed set so the
        download engine re-requests them.

        Args:
            indices: Pieces to check. Defaults to the pieces believed complete.

        Returns:
            What was checked and what survived.
        """
        targets = self.completed_pieces if indices is None else tuple(sorted(set(indices)))
        started = time.monotonic()
        valid: list[int] = []
        invalid: list[int] = []
        batch_size = max(1, self._verifier.workers * VERIFY_BATCH_FACTOR)

        for start in range(0, len(targets), batch_size):
            batch = targets[start : start + batch_size]
            pieces = await asyncio.gather(
                *(
                    self._read_stream(
                        self._torrent.piece_offset(index), self._torrent.piece_size(index)
                    )
                    for index in batch
                )
            )
            checks = [
                (index, data, self._torrent.piece_hash(index))
                for index, data in zip(batch, pieces, strict=True)
                if len(data) == self._torrent.piece_size(index)
            ]
            results = dict(await self._verifier.verify_many(checks))
            for index in batch:
                if results.get(index, False):
                    valid.append(index)
                else:
                    invalid.append(index)

        self._completed = set(valid) | (self._completed - set(targets))
        report = VerificationReport(
            checked=len(targets),
            valid=tuple(valid),
            invalid=tuple(invalid),
            elapsed=time.monotonic() - started,
        )
        logger.info(
            "verified %d piece(s) for %s: %d valid, %d invalid, %.2fs",
            report.checked,
            self._torrent.name,
            len(valid),
            len(invalid),
            report.elapsed,
        )
        return report

    # ---------------------------------------------------------------- resume

    async def save_resume(self, *, uploaded: int = 0) -> Path:
        """Persist which pieces are done, so a restart does not start over.

        Args:
            uploaded: Bytes served to peers, to carry across restarts.

        Returns:
            The file written.
        """
        state = ResumeState(
            info_hash=self._torrent.info_hash,
            name=self._torrent.name,
            piece_length=self._torrent.piece_length,
            piece_count=self._torrent.piece_count,
            total_length=self._torrent.total_length,
            completed_pieces=self.completed_pieces,
            downloaded=self.downloaded_bytes,
            uploaded=uploaded,
            added_at=self._added_at,
            download_directory=str(self._layout.root),
        )
        path = await asyncio.to_thread(self._resume_store.save, state)
        self._emit(
            EventType.RESUME_SAVED,
            f"resume state saved ({len(state.completed_pieces)} piece(s))",
            data={"path": str(path), "pieces": len(state.completed_pieces)},
        )
        return path

    async def load_resume(self, *, verify: bool = False) -> ResumeState | None:
        """Adopt persisted progress for this torrent.

        Args:
            verify: Re-hash every piece the state claims, and keep only the
                ones that still check out. Slower, but the only honest answer
                if the files may have changed.

        Returns:
            The state adopted, or ``None`` when there is none for this torrent.
            With ``verify``, its ``completed_pieces`` are the ones that passed
            re-verification, which may be fewer than the file listed.
        """
        state = await asyncio.to_thread(self._resume_store.load_or_none, self._torrent.info_hash)
        if state is None:
            return None

        if not state.matches(self._torrent):
            logger.warning(
                "resume state for %s does not match the torrent geometry; ignoring it",
                self._torrent.name,
            )
            self._completed = set()
            return None

        self._completed = set(state.completed_pieces)
        self._added_at = state.added_at or self._added_at
        if verify:
            report = await self.verify_completed()
            if report.invalid:
                logger.warning(
                    "resume state for %s claimed %d piece(s); %d failed re-verification "
                    "and will be downloaded again",
                    self._torrent.name,
                    len(state.completed_pieces),
                    len(report.invalid),
                )
            # Return what survived, not what the file claimed. A resume file
            # outlives the download directory it describes, and the count taken
            # from it is the count a caller shows a user: reporting pieces whose
            # bytes are not on disk says we resumed a download we are in fact
            # starting from scratch.
            state = replace(state, completed_pieces=self.completed_pieces)
        self._emit(
            EventType.RESUME_LOADED,
            f"resumed {len(self._completed)} piece(s) from {state.saved_at:.0f}",
            data={"pieces": len(self._completed), "saved_at": state.saved_at},
        )
        return state

    # ------------------------------------------------------------------ disk

    async def on_disk_sizes(self) -> tuple[int, ...]:
        """Current size of every file on disk, in stream order."""
        return await asyncio.to_thread(file_sizes, self._layout)

    # --------------------------------------------------------------- internals

    async def _write_stream(self, offset: int, data: memoryview) -> int:
        """Write bytes at a byte-stream offset, scattering them across files."""
        written = 0
        for mapped, local_offset, chunk in self._layout.plan(offset, data):
            written += await asyncio.to_thread(write_chunk, mapped.path, local_offset, chunk)
        return written

    async def _read_stream(self, offset: int, length: int) -> bytes:
        """Read bytes from a byte-stream offset, gathering them from files."""
        chunks: list[bytes] = []
        for mapped, local_start, local_end in self._layout.spans(offset, length):
            chunk = await asyncio.to_thread(
                read_chunk, mapped.path, local_start, local_end - local_start
            )
            chunks.append(chunk)
        return b"".join(chunks)

    def _emit(
        self,
        event_type: EventType,
        message: str,
        *,
        level: int = logging.INFO,
        data: dict[str, object] | None = None,
    ) -> None:
        """Publish an event, if a bus was provided. Never raises into disk IO."""
        if self._event_bus is None:
            return
        self._event_bus.emit(
            make_event(
                event_type,
                message=message,
                torrent_id=self._torrent.hex_info_hash,
                level=level,
                data=data,
            )
        )
