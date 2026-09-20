"""Persistent resume state (FR-11, TRD §30).

What a download knows that is not recoverable from its files: *which pieces
have been verified*. The files themselves say how many bytes exist, but not
whether the bytes are the right ones — only a hash can answer that, and
re-hashing a whole 5 GiB torrent on every start-up is not acceptable. So the
answer is remembered:

.. code-block:: json

    {
      "version": 1,
      "info_hash": "481b6e...",
      "name": "debian-13.6.0-amd64-netinst.iso",
      "piece_length": 262144,
      "piece_count": 3020,
      "total_length": 791674880,
      "completed_pieces": [0, 1, 2, 5, 6],
      "downloaded": 1572864,
      "uploaded": 0,
      "saved_at": 1768123456.0
    }

Resume state is **advisory**. It is a hint about where to start, and every
piece it claims is still re-verified when the torrent is checked
(:meth:`app.storage.manager.StorageManager.verify_completed`). A resume file
that lies therefore costs a re-download, never a corrupt download.

Corrupt state is never fatal: :meth:`ResumeStore.load_or_none` quarantines the
unreadable file (renaming it, never deleting it) and returns ``None``, so the
client starts fresh instead of refusing to run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from app.core.constants import (
    INFO_HASH_SIZE,
    RESUME_CORRUPT_SUFFIX,
    RESUME_FILE_SUFFIX,
    RESUME_VERSION,
)
from app.storage.errors import ResumeError
from app.torrent.metadata import Torrent

logger = logging.getLogger(__name__)

# Keys written by this version of the format.
_KNOWN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "info_hash",
        "name",
        "piece_length",
        "piece_count",
        "total_length",
        "completed_pieces",
        "downloaded",
        "uploaded",
        "added_at",
        "saved_at",
        "download_directory",
    }
)


def _require_int(
    data: Mapping[str, Any], key: str, *, minimum: int = 0, default: int | None = None
) -> int:
    """Read a non-negative integer field, rejecting booleans and wrong types."""
    if key not in data:
        if default is not None:
            return default
        raise ResumeError(f"resume state is missing the {key!r} field")
    value = data[key]
    # bool is a subclass of int, and {"piece_count": true} is not a number.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResumeError(f"resume field {key!r} must be an integer, got {value!r}")
    if value < minimum:
        raise ResumeError(f"resume field {key!r} must be >= {minimum}, got {value}")
    return value


def _require_str(data: Mapping[str, Any], key: str, *, default: str = "") -> str:
    """Read a string field."""
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ResumeError(f"resume field {key!r} must be a string, got {value!r}")
    return value


def _require_float(data: Mapping[str, Any], key: str) -> float:
    """Read a numeric field as a float, defaulting to zero when absent."""
    value = data.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResumeError(f"resume field {key!r} must be a number, got {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class ResumeState:
    """What to remember about a torrent between runs.

    Args:
        info_hash: The torrent's identity; state is stored per info hash.
        name: Torrent name, for display before the metadata is re-parsed.
        piece_length: Nominal piece size.
        piece_count: Total number of pieces.
        total_length: Total size of the torrent's byte stream.
        completed_pieces: Indices of verified pieces, ascending and unique.
        downloaded: Bytes of piece data verified so far.
        uploaded: Bytes served to other peers.
        added_at: When the torrent was added (``time.time()``).
        saved_at: When this state was written.
        download_directory: Where the data was being written.
        version: Format version of this record.
    """

    info_hash: bytes
    name: str
    piece_length: int
    piece_count: int
    total_length: int
    completed_pieces: tuple[int, ...] = ()
    downloaded: int = 0
    uploaded: int = 0
    added_at: float = 0.0
    saved_at: float = 0.0
    download_directory: str = ""
    version: int = RESUME_VERSION

    def __post_init__(self) -> None:
        if len(self.info_hash) != INFO_HASH_SIZE:
            raise ResumeError(
                f"info hash must be {INFO_HASH_SIZE} bytes, got {len(self.info_hash)}"
            )
        if self.piece_length <= 0:
            raise ResumeError(f"piece length must be positive, got {self.piece_length}")
        if self.piece_count <= 0:
            raise ResumeError(f"piece count must be positive, got {self.piece_count}")
        if self.total_length < 0:
            raise ResumeError(f"total length cannot be negative, got {self.total_length}")
        if self.downloaded < 0 or self.uploaded < 0:
            raise ResumeError("counters cannot be negative")
        for index in self.completed_pieces:
            if not 0 <= index < self.piece_count:
                raise ResumeError(f"completed piece {index} is outside 0..{self.piece_count - 1}")
        if list(self.completed_pieces) != sorted(set(self.completed_pieces)):
            raise ResumeError("completed pieces must be sorted and unique")

    # ------------------------------------------------------------- derived

    @property
    def hex_info_hash(self) -> str:
        """The info hash as lowercase hex, used as the file name."""
        return self.info_hash.hex()

    @property
    def completed_count(self) -> int:
        """Number of pieces marked complete."""
        return len(self.completed_pieces)

    @property
    def progress(self) -> float:
        """Fraction of pieces marked complete, in ``[0.0, 1.0]``."""
        return self.completed_count / self.piece_count

    @property
    def complete(self) -> bool:
        """True when every piece is marked complete."""
        return self.completed_count == self.piece_count

    def piece_bytes(self, index: int) -> int:
        """Size of a piece, given the geometry recorded in this state."""
        if not 0 <= index < self.piece_count:
            raise IndexError(f"piece {index} is outside 0..{self.piece_count - 1}")
        if index == self.piece_count - 1:
            return self.total_length - (index * self.piece_length)
        return self.piece_length

    def completed_bytes(self) -> int:
        """Bytes covered by the completed pieces (last piece may be short)."""
        return sum(self.piece_bytes(index) for index in self.completed_pieces)

    def has(self, index: int) -> bool:
        """Whether a piece is marked complete."""
        return index in set(self.completed_pieces)

    def with_piece(self, index: int) -> ResumeState:
        """Return a copy with ``index`` marked complete.

        Args:
            index: Piece index to add.

        Returns:
            A new state; ``self`` is unchanged.
        """
        if not 0 <= index < self.piece_count:
            raise IndexError(f"piece {index} is outside 0..{self.piece_count - 1}")
        if index in self.completed_pieces:
            return self
        return replace(self, completed_pieces=tuple(sorted({*self.completed_pieces, index})))

    def without_piece(self, index: int) -> ResumeState:
        """Return a copy with ``index`` no longer marked complete."""
        if index not in self.completed_pieces:
            return self
        return replace(self, completed_pieces=tuple(i for i in self.completed_pieces if i != index))

    # ---------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        """Render as a JSON-safe dictionary."""
        return {
            "version": self.version,
            "info_hash": self.info_hash.hex(),
            "name": self.name,
            "piece_length": self.piece_length,
            "piece_count": self.piece_count,
            "total_length": self.total_length,
            "completed_pieces": list(self.completed_pieces),
            "downloaded": self.downloaded,
            "uploaded": self.uploaded,
            "added_at": self.added_at,
            "saved_at": self.saved_at,
            "download_directory": self.download_directory,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResumeState:
        """Rebuild state from parsed JSON.

        Args:
            data: The decoded resume document.

        Returns:
            The state.

        Raises:
            ResumeError: If any field is missing, wrongly typed, out of range,
                or the version is one this client cannot read.
        """
        if not isinstance(data, Mapping):
            raise ResumeError(f"resume state must be a JSON object, got {type(data).__name__}")

        version = _require_int(data, "version", default=RESUME_VERSION)
        if version > RESUME_VERSION:
            raise ResumeError(
                f"resume state version {version} is newer than the supported "
                f"version {RESUME_VERSION}; upgrade the client or delete the file"
            )

        raw_hash = _require_str(data, "info_hash")
        try:
            info_hash = bytes.fromhex(raw_hash)
        except ValueError as exc:
            raise ResumeError(f"resume field 'info_hash' is not hex: {exc}") from exc
        if len(info_hash) != INFO_HASH_SIZE:
            raise ResumeError(
                f"resume field 'info_hash' must be {INFO_HASH_SIZE} bytes, got {len(info_hash)}"
            )

        piece_count = _require_int(data, "piece_count", minimum=1)
        raw_pieces = data.get("completed_pieces", [])
        if not isinstance(raw_pieces, (list, tuple)):
            raise ResumeError(
                f"resume field 'completed_pieces' must be a list, got {type(raw_pieces).__name__}"
            )
        completed: set[int] = set()
        for value in raw_pieces:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ResumeError(f"completed_pieces entries must be integers, got {value!r}")
            if not 0 <= value < piece_count:
                raise ResumeError(
                    f"completed piece {value} is outside 0..{piece_count - 1}; "
                    "the torrent changed or the file is corrupt"
                )
            completed.add(value)

        total_length = _require_int(data, "total_length")
        piece_length = _require_int(data, "piece_length", minimum=1)
        name = _require_str(data, "name")

        return cls(
            info_hash=info_hash,
            name=name,
            piece_length=piece_length,
            piece_count=piece_count,
            total_length=total_length,
            completed_pieces=tuple(sorted(completed)),
            downloaded=_require_int(data, "downloaded", default=0),
            uploaded=_require_int(data, "uploaded", default=0),
            added_at=_require_float(data, "added_at"),
            saved_at=_require_float(data, "saved_at"),
            download_directory=_require_str(data, "download_directory"),
            version=version,
        )

    @classmethod
    def from_torrent(
        cls, torrent: Torrent, *, directory: Path | None = None, added_at: float | None = None
    ) -> ResumeState:
        """Empty state for a torrent that has not downloaded anything yet."""
        return cls(
            info_hash=torrent.info_hash,
            name=torrent.name,
            piece_length=torrent.piece_length,
            piece_count=torrent.piece_count,
            total_length=torrent.total_length,
            completed_pieces=(),
            added_at=time.time() if added_at is None else added_at,
            download_directory=str(directory) if directory is not None else "",
        )

    def matches(self, torrent: Torrent) -> bool:
        """Whether this state describes the same torrent geometry.

        An info hash match is not enough on its own in practice: a re-created
        torrent with a different piece length is a different download, and
        trusting the old piece list would write data at the wrong offsets.
        """
        return (
            self.info_hash == torrent.info_hash
            and self.piece_length == torrent.piece_length
            and self.piece_count == torrent.piece_count
            and self.total_length == torrent.total_length
        )


class ResumeStore:
    """Reads and writes resume files, one per torrent, in a state directory.

    Args:
        directory: Where ``<info hash>.resume.json`` files live. Created on
            first write.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """Directory holding the resume files."""
        return self._directory

    def path_for(self, info_hash: bytes) -> Path:
        """Path of the resume file for ``info_hash``."""
        if len(info_hash) != INFO_HASH_SIZE:
            raise ResumeError(f"info hash must be {INFO_HASH_SIZE} bytes, got {len(info_hash)}")
        return self._directory / f"{info_hash.hex()}{RESUME_FILE_SUFFIX}"

    # ----------------------------------------------------------------- writing

    def save(self, state: ResumeState) -> Path:
        """Write ``state`` atomically and return the path written.

        The file is written to a temporary name and then renamed, so a crash
        mid-write leaves either the old state or the new one — never a
        half-written document that would be unreadable next start.

        Raises:
            ResumeError: If the state directory cannot be created or written.
        """
        document = replace(state, saved_at=time.time()).to_dict()
        path = self.path_for(state.info_hash)
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(document, indent=2, sort_keys=True)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            raise ResumeError(f"cannot write resume state to {path}: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()
        logger.debug(
            "saved resume state for %s (%d pieces)", state.hex_info_hash, state.completed_count
        )
        return path

    # ----------------------------------------------------------------- reading

    def load(self, info_hash: bytes) -> ResumeState | None:
        """Load state for ``info_hash``.

        Returns:
            The state, or ``None`` when no file exists (a fresh torrent).

        Raises:
            ResumeError: If the file exists but cannot be trusted.
        """
        path = self.path_for(info_hash)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ResumeError(f"cannot read resume state {path}: {exc}") from exc

        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResumeError(f"resume state {path} is not valid JSON: {exc}") from exc

        state = ResumeState.from_dict(document)
        if state.info_hash != info_hash:
            raise ResumeError(
                f"resume state {path} describes {state.hex_info_hash}, "
                f"not the requested {info_hash.hex()}"
            )
        logger.debug(
            "loaded resume state for %s (%d pieces)", state.hex_info_hash, state.completed_count
        )
        return state

    def load_or_none(self, info_hash: bytes, *, quarantine: bool = True) -> ResumeState | None:
        """Load state, treating an unreadable file as "no state".

        Args:
            info_hash: The torrent's identity.
            quarantine: Rename an unreadable file instead of leaving it to fail
                again on every start-up.

        Returns:
            The state, or ``None`` if it is missing or unusable.
        """
        try:
            return self.load(info_hash)
        except ResumeError as exc:
            logger.warning("ignoring unusable resume state: %s", exc)
            if quarantine:
                self.quarantine(info_hash)
            return None

    def quarantine(self, info_hash: bytes) -> Path | None:
        """Rename a resume file so it stops being read, without deleting it.

        Returns:
            The new path, or ``None`` if there was nothing to quarantine.
        """
        path = self.path_for(info_hash)
        if not path.exists():
            return None
        target = path.with_name(f"{path.name}{RESUME_CORRUPT_SUFFIX}")
        try:
            os.replace(path, target)
        except OSError as exc:
            logger.warning("could not quarantine %s: %s", path, exc)
            return None
        logger.warning("quarantined unreadable resume state as %s", target)
        return target

    def discard(self, info_hash: bytes) -> bool:
        """Delete a torrent's resume state. Returns whether a file was removed."""
        path = self.path_for(info_hash)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ResumeError(f"cannot delete resume state {path}: {exc}") from exc
        return True

    def available(self) -> tuple[str, ...]:
        """Hex info hashes that have a resume file, sorted by name."""
        if not self._directory.is_dir():
            return ()
        return tuple(
            sorted(
                path.name[: -len(RESUME_FILE_SUFFIX)]
                for path in self._directory.glob(f"*{RESUME_FILE_SUFFIX}")
                if path.is_file()
            )
        )

    def load_all(self) -> tuple[ResumeState, ...]:
        """Load every readable resume file, quarantining the unreadable ones.

        Used at start-up to rebuild the torrent library: one bad file must not
        stop the other torrents from being restored.
        """
        states: list[ResumeState] = []
        for hex_hash in self.available():
            try:
                state = self.load(bytes.fromhex(hex_hash))
            except ResumeError as exc:
                logger.warning("ignoring unusable resume state: %s", exc)
                self.quarantine(bytes.fromhex(hex_hash))
                continue
            if state is not None:
                states.append(state)
        return tuple(states)
