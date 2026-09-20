"""Path sanitisation for untrusted torrent metadata.

Torrents are attacker-supplied data, and a torrent's file list is a set of
instructions about where to write on the local disk.  A file entry whose path
is ``../../.ssh/authorized_keys`` — or simply ``/etc/passwd`` — would turn
"open a torrent" into "overwrite a system file", so every component is
validated here, at the point the metadata enters the process, rather than
trusting the storage layer to notice later.

This module is deliberately independent of the rest of the torrent package so
that it can be unit-tested exhaustively and reused by the storage layer (M6),
which calls :func:`resolve_within` as a final belt-and-braces check.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from app.torrent.errors import UnsafePathError

logger = logging.getLogger(__name__)

MAX_COMPONENT_LENGTH: int = 255  # typical filesystem limit (NAME_MAX)
MAX_PATH_LENGTH: int = 4096  # PATH_MAX on Linux; generous, not a security control

# Characters that are never legal in a torrent-provided path component.
_FORBIDDEN_CHARACTERS: frozenset[str] = frozenset({"/", "\\", "\x00"})
# Windows drive prefixes ("C:", "D:") and NTFS stream syntax ("file:stream").
_DRIVE_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z]:")
# Reserved Windows device names, regardless of extension (CON.txt is CON).
_RESERVED_WINDOWS_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def decode_component(raw: bytes | bytearray | memoryview, *, field: str) -> str:
    """Decode one path component from raw torrent bytes.

    UTF-8 is attempted first; torrents in the wild predate the UTF-8 convention
    and may contain legacy encodings, so a latin-1 fallback keeps such files
    usable instead of rejecting them outright.  Safety is enforced by
    :func:`validate_component`, not by the decoding step.

    Args:
        raw: Raw bytes of a single path component.
        field: Field name used in error messages (e.g. ``"name"``, ``"files[2].path"``).

    Raises:
        UnsafePathError: If the decoded component is not safe to use.
    """
    data = bytes(raw)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        logger.debug("path component in %s is not valid UTF-8; using latin-1 fallback", field)
        text = data.decode("latin-1")

    validate_component(text, field=field)
    return text


def validate_component(component: str, *, field: str) -> None:
    """Validate a single path component.

    Rejects: empty names, ``.`` and ``..``, separators, NUL and control
    characters, Windows drive prefixes, reserved device names, names that are
    only whitespace or trailing dots/spaces (Windows strips these, which can
    silently alias two different files), and over-long components.

    Raises:
        UnsafePathError: On any of the above.
    """
    if not component:
        raise UnsafePathError("empty path component", field=field, value=component)
    if component in {".", ".."}:
        raise UnsafePathError(
            f"path component {component!r} would traverse the directory tree",
            field=field,
            value=component,
        )
    if bad := _FORBIDDEN_CHARACTERS.intersection(component):
        raise UnsafePathError(
            f"path component contains forbidden character(s) {sorted(bad)!r}",
            field=field,
            value=component,
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in component):
        raise UnsafePathError(
            "path component contains control characters", field=field, value=component
        )
    if _DRIVE_PATTERN.match(component):
        raise UnsafePathError(
            "path component looks like a Windows drive prefix", field=field, value=component
        )
    if component.split(".")[0].upper() in _RESERVED_WINDOWS_NAMES:
        raise UnsafePathError(
            "path component uses a reserved device name", field=field, value=component
        )
    if component != component.strip():
        raise UnsafePathError(
            "path component has leading or trailing whitespace", field=field, value=component
        )
    if component.endswith((".", " ")):
        raise UnsafePathError(
            "path component ends with a dot or space (aliases on Windows)",
            field=field,
            value=component,
        )
    if len(component.encode("utf-8")) > MAX_COMPONENT_LENGTH:
        raise UnsafePathError(
            f"path component exceeds {MAX_COMPONENT_LENGTH} bytes", field=field, value=component
        )


def sanitize_name(raw: bytes | bytearray | memoryview, *, field: str = "name") -> str:
    """Sanitise the torrent ``name`` field (used as the download directory).

    Args:
        raw: Raw bytes of the ``name`` field.
        field: Field name for error messages.

    Returns:
        A safe, non-empty file or directory name.

    Raises:
        UnsafePathError: If the name is empty or unsafe.
    """
    return decode_component(raw, field=field)


def sanitize_path_components(
    components: Sequence[bytes | bytearray | memoryview],
    *,
    field: str = "path",
) -> PurePosixPath:
    """Sanitise a torrent ``files[].path`` list into a relative POSIX path.

    Args:
        components: The decoded ``path`` list from the info dictionary.
        field: Field name for error messages.

    Returns:
        A relative :class:`PurePosixPath` that is guaranteed to stay inside the
        torrent's root directory.

    Raises:
        UnsafePathError: If the list is empty or any component is unsafe.
    """
    if not components:
        raise UnsafePathError("file entry has an empty path", field=field)

    parts: list[str] = []
    for index, raw in enumerate(components):
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise UnsafePathError(
                f"path component {index} must be a byte string, got {type(raw).__name__}",
                field=field,
            )
        parts.append(decode_component(raw, field=f"{field}[{index}]"))

    path = PurePosixPath(*parts)
    if len(str(path).encode("utf-8")) > MAX_PATH_LENGTH:
        raise UnsafePathError("path is too long", field=field, value=str(path))
    return path


def resolve_within(base: Path, relative: PurePosixPath) -> Path:
    """Join ``relative`` onto ``base`` and prove the result stays inside it.

    This is the last line of defence used by the storage layer: even if a path
    slipped past :func:`sanitize_path_components`, a symlinked or surprising
    component cannot escape ``base`` without this check failing.

    Args:
        base: Trusted root directory (the user's chosen download location).
        relative: Path from torrent metadata, already sanitised.

    Returns:
        The absolute resolved path.

    Raises:
        UnsafePathError: If the result is not contained within ``base``.
    """
    root = base.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise UnsafePathError(
            f"path escapes the download directory ({candidate} is not inside {root})",
            field="path",
            value=str(relative),
        )
    return candidate
