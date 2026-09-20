"""File layout, path containment, allocation and scatter/gather IO (TRD §29).

A torrent is **one continuous byte stream**; its files are windows onto that
stream. Everything in this module follows from that single idea: a piece is
written at a *stream offset*, and :meth:`FileLayout.spans` works out which
files — and which local offsets inside them — that range touches. A piece that
straddles two files is therefore not a special case, it is just a range whose
spans contain two entries.

Splitting the layout (pure arithmetic, fully unit-testable) from the IO (thin
``os`` calls) keeps the interesting logic out of the filesystem's way:

::

    layout.plan(offset, data) -> [(mapped_file, local_offset, chunk), ...]
    write_chunk(mapped_file.path, local_offset, chunk)      # blocking, sync

The manager runs those sync helpers in worker threads, so the event loop never
blocks on a disk write.

Path safety is enforced here as well, even though :mod:`app.torrent.path_safety`
already validated the metadata, because this is the last point at which a path
is turned into a file on disk: :func:`app.torrent.path_safety.resolve_within`
proves every path stays under the download root, and duplicate resolved paths
are rejected (two entries writing the same file would corrupt each other).
"""

from __future__ import annotations

import errno
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.storage.errors import AllocationError, StoragePathError
from app.torrent.metadata import FileEntry, Torrent
from app.torrent.path_safety import resolve_within

logger = logging.getLogger(__name__)

# Mode for files we create: readable/writable by the user, group/other
# read-only, before umask is applied. Deliberately not executable.
_FILE_MODE: int = 0o666

# posix_fallocate fails with these on filesystems that do not support it
# (tmpfs with older kernels, some network mounts); a sparse truncate is the
# correct fallback rather than an error.
_FALLOCATE_UNSUPPORTED: frozenset[int] = frozenset(
    {errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL, errno.EPERM}
)

# Two of the calls below are POSIX-only, and both degrade rather than fail,
# because the alternative is a client that cannot write a byte on Windows:
#
# ``os.O_CLOEXEC`` does not exist there. It also does not matter: CPython marks
# handles non-inheritable by default, which is the property the flag asks for.
# ``os.pwrite``/``os.pread`` do not exist there either. They exist so that two
# writers aiming at different offsets in one file cannot move each other's file
# position — but every call here opens its *own* descriptor, so the position is
# already private, and a seek plus a write on it is just as safe.
_CLOEXEC: Final[int] = getattr(os, "O_CLOEXEC", 0)
_BINARY: Final[int] = getattr(os, "O_BINARY", 0)
_OPEN_WRITE: Final[int] = os.O_WRONLY | os.O_CREAT | _CLOEXEC | _BINARY
_OPEN_READ: Final[int] = os.O_RDONLY | _CLOEXEC | _BINARY
_POSITIONAL_IO: Final[bool] = hasattr(os, "pwrite") and hasattr(os, "pread")


def _write_at(fd: int, view: memoryview[bytes], offset: int) -> int:
    """Write all of ``view`` at ``offset`` on ``fd``.

    Args:
        fd: An open, writable descriptor private to this call.
        view: The bytes to write.
        offset: Where in the file they belong.

    Returns:
        The number of bytes written.

    Raises:
        OSError: If the write fails. Both loops may legally write fewer bytes
            than asked, so each runs until the view is exhausted.
    """
    written = 0
    if _POSITIONAL_IO:
        while written < len(view):
            written += os.pwrite(fd, view[written:], offset + written)
        return written
    os.lseek(fd, offset, os.SEEK_SET)
    while written < len(view):
        written += os.write(fd, view[written:])
    return written


def _read_at(fd: int, length: int, offset: int, out: list[bytes]) -> None:
    """Read up to ``length`` bytes from ``offset`` on ``fd``, appending to ``out``.

    The blocks land in a caller-owned list rather than a return value so that a
    failure part-way through still hands back what arrived: resume verification
    would rather see three good bytes than an exception.

    Args:
        fd: An open, readable descriptor private to this call.
        length: The most bytes wanted.
        offset: Where in the file to start.
        out: Appended with the blocks read, in order. Possibly fewer than
            ``length`` bytes' worth: a short result means the file ends before
            the torrent says it should.

    Raises:
        OSError: If a read fails, with whatever was read already in ``out``.
    """
    remaining = length
    position = offset
    if not _POSITIONAL_IO:
        os.lseek(fd, offset, os.SEEK_SET)
    while remaining > 0:
        block = os.pread(fd, remaining, position) if _POSITIONAL_IO else os.read(fd, remaining)
        if not block:
            break
        out.append(block)
        position += len(block)
        remaining -= len(block)


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """Outcome of creating a torrent's files on disk.

    Attributes:
        file_count: Number of files in the layout (including empty ones).
        total_bytes: Total length declared by the torrent.
        bytes_allocated: Bytes actually reserved by this call — zero when the
            files already existed at full size, as on a resumed download.
        preallocated: Whether allocation was requested at all.
    """

    file_count: int
    total_bytes: int
    bytes_allocated: int
    preallocated: bool


@dataclass(frozen=True, slots=True)
class MappedFile:
    """One torrent file, resolved to a real path under the download root.

    Attributes:
        entry: The metadata entry (relative path, length, stream offset).
        path: Absolute path on disk that is proven to be inside the root.
    """

    entry: FileEntry
    path: Path

    @property
    def length(self) -> int:
        """Size of the file in bytes (may be zero for padding files)."""
        return self.entry.length

    @property
    def offset(self) -> int:
        """Offset of this file's first byte in the torrent byte stream."""
        return self.entry.offset

    @property
    def end_offset(self) -> int:
        """Offset one past this file's last byte in the byte stream."""
        return self.entry.end_offset

    @property
    def relative_path(self) -> Path:
        """Path as declared by the torrent, relative to the download root."""
        return Path(self.entry.path)

    def clip(self, start: int, end: int) -> tuple[int, int] | None:
        """Clip a byte-stream range to this file, in local coordinates.

        Args:
            start: First byte of the range in the torrent stream.
            end: Offset one past the last byte of the range.

        Returns:
            ``(local_start, local_end)`` relative to this file, or ``None``
            when the range does not touch it.
        """
        if self.length == 0 or end <= self.offset or start >= self.end_offset:
            return None
        return max(start, self.offset) - self.offset, min(end, self.end_offset) - self.offset


@dataclass(frozen=True, slots=True)
class FileLayout:
    """The torrent byte stream mapped onto files under one root directory.

    Args:
        files: Mapped files, in stream order.
        root: Directory every file is contained within.
        total_length: Length of the whole byte stream.
    """

    files: tuple[MappedFile, ...]
    root: Path
    total_length: int

    def __post_init__(self) -> None:
        if not self.files:
            raise StoragePathError("a torrent must contain at least one file")
        if self.total_length < 0:
            raise StoragePathError(f"total length cannot be negative: {self.total_length}")

        root = self.root.resolve()
        seen: dict[Path, str] = {}
        for mapped in self.files:
            target = mapped.path.resolve()
            if target != root and root not in target.parents:
                raise StoragePathError(
                    f"refusing to write {mapped.relative_path} outside the "
                    f"download directory ({target} is not inside {root})"
                )
            previous = seen.get(mapped.path)
            if previous is not None:
                raise StoragePathError(
                    f"two torrent entries resolve to the same file {mapped.path} "
                    f"({previous} and {mapped.relative_path})"
                )
            seen[mapped.path] = str(mapped.relative_path)

    # ------------------------------------------------------------ construction

    @classmethod
    def from_torrent(cls, torrent: Torrent, root: Path) -> FileLayout:
        """Build a layout for ``torrent`` inside ``root``.

        Args:
            torrent: The validated torrent.
            root: Download directory; created lazily by :func:`create_files`.

        Returns:
            The layout, with every path proven to be inside ``root``.

        Raises:
            StoragePathError: If a path escapes ``root`` or two entries collide.
        """
        base = Path(root).expanduser()
        mapped: list[MappedFile] = []
        for entry in torrent.files:
            try:
                path = resolve_within(base, entry.path)
            except Exception as exc:  # UnsafePathError from the metadata layer
                raise StoragePathError(
                    f"refusing to write {entry.path} outside {base}: {exc}"
                ) from exc
            mapped.append(MappedFile(entry=entry, path=path))
        return cls(files=tuple(mapped), root=base.resolve(), total_length=torrent.total_length)

    # -------------------------------------------------------------- geometry

    @property
    def file_count(self) -> int:
        """Number of files in the layout."""
        return len(self.files)

    @property
    def directories(self) -> tuple[Path, ...]:
        """Every directory that must exist, parents first, de-duplicated."""
        ordered: list[Path] = [self.root]
        for mapped in self.files:
            parent = mapped.path.parent
            if parent not in ordered:
                ordered.append(parent)
        return tuple(ordered)

    def validate_range(self, start: int, length: int) -> None:
        """Check that ``[start, start + length)`` lies inside the byte stream.

        Raises:
            StoragePathError: On negative offsets or a range past the end.
        """
        if start < 0:
            raise StoragePathError(f"offset cannot be negative: {start}")
        if length < 0:
            raise StoragePathError(f"length cannot be negative: {length}")
        if start + length > self.total_length:
            raise StoragePathError(
                f"range {start}..{start + length} is past the end of the "
                f"{self.total_length}-byte torrent"
            )

    def spans(self, start: int, length: int) -> Iterator[tuple[MappedFile, int, int]]:
        """Yield ``(file, local_start, local_end)`` for a byte-stream range.

        Empty files are skipped, so a range that lands entirely in a zero-length
        padding file yields nothing.

        Args:
            start: Offset in the torrent byte stream.
            length: Number of bytes.

        Yields:
            One entry per file the range touches, in stream order, with offsets
            local to that file.
        """
        self.validate_range(start, length)
        if length == 0:
            return
        end = start + length
        for mapped in self.files:
            clipped = mapped.clip(start, end)
            if clipped is None:
                continue
            local_start, local_end = clipped
            if local_end > local_start:
                yield mapped, local_start, local_end

    def plan(
        self, offset: int, data: bytes | bytearray | memoryview
    ) -> list[tuple[MappedFile, int, memoryview]]:
        """Split a write into per-file chunks.

        Args:
            offset: Byte-stream offset the data belongs at.
            data: The bytes to write.

        Returns:
            ``(file, local_offset, chunk)`` triples covering all of ``data``.

        Raises:
            StoragePathError: If the range is outside the torrent, or the data
                would not fit the files it lands in.
        """
        view = memoryview(data)
        plan: list[tuple[MappedFile, int, memoryview]] = []
        consumed = 0
        for mapped, local_start, local_end in self.spans(offset, len(view)):
            size = local_end - local_start
            plan.append((mapped, local_start, view[consumed : consumed + size]))
            consumed += size
        if consumed != len(view):
            raise StoragePathError(
                f"cannot write {len(view)} bytes at offset {offset}: "
                f"only {consumed} bytes of the torrent's files cover that range"
            )
        return plan


# -------------------------------------------------------------------- disk IO
# Every function below blocks. The storage manager runs them in worker threads;
# nothing here may touch the event loop.


def create_directories(layout: FileLayout) -> None:
    """Create the root and every parent directory a torrent needs."""
    for directory in layout.directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AllocationError(f"cannot create directory {directory}: {exc}") from exc


def preallocate_file(path: Path, length: int) -> int:
    """Grow ``path`` to ``length`` bytes, never shrinking what is there.

    Reserving the space up front turns a mid-download "disk full" into a
    startup error the user can act on, and keeps the file from fragmenting as
    pieces arrive out of order.

    Existing data is preserved: on a resumed download the file is already the
    right size, and truncating it would throw away verified pieces.

    Args:
        path: File to size.
        length: Desired size in bytes.

    Returns:
        Number of bytes actually reserved (0 if the file was already big enough).

    Raises:
        AllocationError: If the file cannot be created or grown.
    """
    try:
        exists = path.exists()
        current = path.stat().st_size if exists else 0
    except OSError as exc:
        raise AllocationError(f"cannot stat {path}: {exc}") from exc
    # A zero-length file still has to exist (BEP 47 padding files are real
    # entries), so "already big enough" only short-circuits for files that are
    # actually there.
    if exists and current >= length:
        return 0

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, _OPEN_WRITE, _FILE_MODE)
    except OSError as exc:
        raise AllocationError(f"cannot create {path}: {exc}") from exc

    try:
        if length > current and hasattr(os, "posix_fallocate"):
            try:
                os.posix_fallocate(fd, 0, length)
                return length - current
            except OSError as exc:
                if exc.errno not in _FALLOCATE_UNSUPPORTED:
                    raise
                logger.debug("posix_fallocate unsupported on %s; using truncate", path)
        if length > current:
            os.ftruncate(fd, length)
    except OSError as exc:
        raise AllocationError(f"cannot allocate {length} bytes for {path}: {exc}") from exc
    finally:
        os.close(fd)
    return length - current


def create_files(layout: FileLayout, *, preallocate: bool = True) -> AllocationResult:
    """Create every file in the layout, optionally at full size.

    Zero-length files are created too: a torrent may legitimately contain
    padding files (BEP 47) and a missing one would fail hashing later.

    Args:
        layout: The layout to materialise.
        preallocate: Whether to reserve the full size now. When false, files
            are created empty and grow as data arrives (sparse, slower, but
            friendly to quota-limited or copy-on-write filesystems).

    Returns:
        How many bytes were reserved.

    Raises:
        AllocationError: If any file cannot be created or sized.
    """
    create_directories(layout)
    allocated = 0
    for mapped in layout.files:
        if preallocate:
            allocated += preallocate_file(mapped.path, mapped.length)
        else:
            try:
                mapped.path.parent.mkdir(parents=True, exist_ok=True)
                mapped.path.touch(exist_ok=True)
            except OSError as exc:
                raise AllocationError(f"cannot create {mapped.path}: {exc}") from exc
    return AllocationResult(
        file_count=layout.file_count,
        total_bytes=layout.total_length,
        bytes_allocated=allocated,
        preallocated=preallocate,
    )


def write_chunk(path: Path, offset: int, data: bytes | bytearray | memoryview) -> int:
    """Write ``data`` at ``offset`` in ``path``, creating the file if needed.

    ``pwrite`` is used rather than seek+write so that concurrent writes to
    different parts of the same file cannot interleave — several pieces may be
    in flight, and they all target one file. On a platform without ``pwrite``
    the fallback seeks on this call's own descriptor instead, which is private
    to it and therefore equally safe.

    Args:
        path: File to write to.
        offset: Offset within the file.
        data: Bytes to write.

    Returns:
        Number of bytes written.

    Raises:
        AllocationError: If the file cannot be opened or the write fails.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, _OPEN_WRITE, _FILE_MODE)
    except OSError as exc:
        raise AllocationError(f"cannot open {path} for writing: {exc}") from exc
    try:
        written = _write_at(fd, memoryview(data), offset)
    except OSError as exc:
        raise AllocationError(f"write to {path} at {offset} failed: {exc}") from exc
    finally:
        os.close(fd)
    return written


def read_chunk(path: Path, offset: int, length: int) -> bytes:
    """Read up to ``length`` bytes from ``offset`` in ``path``.

    A short result is not an error: it means the file is smaller than the
    torrent declares, which is exactly what resume verification needs to see.

    Args:
        path: File to read from.
        offset: Offset within the file.
        length: Maximum number of bytes to read.

    Returns:
        The bytes read; shorter than ``length`` at end of file, empty if the
        file does not exist or the offset is past its end.
    """
    if length <= 0:
        return b""
    try:
        fd = os.open(path, _OPEN_READ)
    except FileNotFoundError:
        logger.debug("read at %s: file does not exist yet", path)
        return b""
    except OSError as exc:
        logger.warning("cannot open %s for reading: %s", path, exc)
        return b""
    chunks: list[bytes] = []
    try:
        _read_at(fd, length, offset, chunks)
    except OSError as exc:
        logger.warning("read from %s at %s failed: %s", path, offset, exc)
        return b"".join(chunks)
    finally:
        os.close(fd)
    return b"".join(chunks)


def file_sizes(layout: FileLayout) -> tuple[int, ...]:
    """Current on-disk size of every file in the layout, in stream order.

    A file that is missing counts as zero: that is exactly the state of a
    download that has not written anything yet.
    """
    sizes: list[int] = []
    for mapped in layout.files:
        try:
            sizes.append(mapped.path.stat().st_size)
        except OSError:
            sizes.append(0)
    return tuple(sizes)
