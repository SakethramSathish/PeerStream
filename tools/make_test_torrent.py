"""Generate deterministic torrents and payloads for local testing.

The whole point of this tool is to make the client testable **without the
public internet** (TRD §42). It produces a reproducible payload (seeded PRNG, so
piece hashes are identical on every machine and in CI) plus a matching
``.torrent`` file, and can write both to disk ready for a local mock tracker
and seeder.

Usage::

    # 4 MiB single-file torrent + payload
    python tools/make_test_torrent.py --size 4MiB --out data/torrents/test.torrent

    # 8 MiB multi-file torrent split across 5 files, payload under ./payload
    python tools/make_test_torrent.py --size 8MiB --files 5 --out test.torrent --dir payload

The module is also imported directly by the test suite
(``tests/conftest.py``), which builds torrents in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from app.bencode import encode
from app.bencode.decoder import BencodeValue

DEFAULT_PIECE_LENGTH: Final[int] = 16 * 1024  # 16 KiB, the protocol's classic unit
DEFAULT_ANNOUNCE: Final[str] = "http://127.0.0.1:8000/announce"
DEFAULT_NAME: Final[str] = "test-payload.bin"
CREATED_BY: Final[str] = "bittorrent-client test harness"

_SIZE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(i?b?)?\s*$", re.I
)
_SIZE_UNITS: Final[dict[str, int]] = {
    "": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
}


def parse_size(text: str | int) -> int:
    """Parse a human-readable size such as ``4MiB``, ``512K`` or ``1048576``.

    Args:
        text: Size with an optional binary-unit suffix.

    Returns:
        The size in bytes.

    Raises:
        ValueError: If the text cannot be parsed.
    """
    if isinstance(text, int):
        return text
    match = _SIZE_PATTERN.match(text)
    if match is None:
        raise ValueError(f"cannot parse size {text!r}")
    number, unit = match.group(1), (match.group(2) or "").lower()
    return int(float(number) * _SIZE_UNITS[unit])


def generate_payload(size: int, *, seed: int = 1234) -> bytes:
    """Generate a deterministic payload of ``size`` bytes.

    Seeded so that piece hashes are stable across machines and CI runs — a test
    that depends on random bytes is a test that fails on someone else's laptop.
    """
    if size < 0:
        raise ValueError(f"size must not be negative, got {size}")
    return random.Random(seed).randbytes(size)


def compute_piece_hashes(payload: bytes, piece_length: int = DEFAULT_PIECE_LENGTH) -> bytes:
    """Concatenate the SHA-1 hash of every piece of ``payload``.

    This is exactly the ``info.pieces`` blob a real torrent carries.
    """
    if piece_length <= 0:
        raise ValueError(f"piece_length must be positive, got {piece_length}")
    return b"".join(
        hashlib.sha1(payload[offset : offset + piece_length]).digest()
        for offset in range(0, len(payload), piece_length)
    )


def split_payload(payload: bytes, parts: int) -> list[bytes]:
    """Split a payload into ``parts`` chunks, last one absorbing the remainder."""
    if parts < 1:
        raise ValueError(f"parts must be at least 1, got {parts}")
    base, remainder = divmod(len(payload), parts)
    chunks: list[bytes] = []
    offset = 0
    for index in range(parts):
        size = base + (1 if index < remainder else 0)
        chunks.append(payload[offset : offset + size])
        offset += size
    return chunks


def default_paths(count: int) -> list[list[str]]:
    """Default file layout for a multi-file test torrent.

    Every third file lands in a subdirectory so tests exercise nested paths and
    (later) pieces that straddle file boundaries.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    if count == 1:
        return [[DEFAULT_NAME]]
    return [
        ["data", f"part{index:03d}.bin"] if index % 3 == 2 else [f"part{index:03d}.bin"]
        for index in range(count)
    ]


def build_info(
    *,
    name: str = DEFAULT_NAME,
    piece_length: int = DEFAULT_PIECE_LENGTH,
    pieces: bytes,
    length: int | None = None,
    files: Sequence[tuple[Sequence[str], int]] | None = None,
    private: bool = False,
) -> dict[bytes, BencodeValue]:
    """Assemble an ``info`` dictionary.

    Args:
        name: Torrent name.
        piece_length: Piece size in bytes.
        pieces: The concatenated SHA-1 blob for all pieces.
        length: Total size, for a single-file torrent.
        files: ``(path_parts, size)`` pairs, for a multi-file torrent.
        private: Set the BEP 27 private flag.

    Returns:
        A bencode-ready ``info`` dictionary.

    Raises:
        ValueError: If neither or both of ``length`` and ``files`` are given.
    """
    if (length is None) == (files is None):
        raise ValueError("provide exactly one of length (single-file) or files (multi-file)")

    info: dict[bytes, BencodeValue] = {
        b"name": name.encode("utf-8"),
        b"piece length": piece_length,
        b"pieces": pieces,
    }
    if files is not None:
        info[b"files"] = [
            {b"length": size, b"path": [part.encode("utf-8") for part in parts]}
            for parts, size in files
        ]
    else:
        info[b"length"] = int(length or 0)
    if private:
        info[b"private"] = 1
    return info


def build_torrent_bytes(
    payload: bytes,
    *,
    name: str = DEFAULT_NAME,
    piece_length: int = DEFAULT_PIECE_LENGTH,
    file_count: int = 1,
    paths: Sequence[Sequence[str]] | None = None,
    announce: str | None = DEFAULT_ANNOUNCE,
    announce_list: Sequence[Sequence[str]] | None = None,
    comment: str | None = "generated by tools/make_test_torrent.py",
    private: bool = False,
) -> bytes:
    """Build the bencoded bytes of a valid torrent for ``payload``.

    Args:
        payload: Concatenated file contents of the torrent.
        name: Torrent name.
        piece_length: Piece size in bytes.
        file_count: Number of files to split the payload across (1 = single-file).
        paths: Explicit path layout; overrides ``file_count``-based defaults.
        announce: Primary tracker URL.
        announce_list: Tracker tiers (BEP 12).
        comment: Free-text comment.
        private: Set the private flag.

    Returns:
        Bencoded ``.torrent`` contents whose piece hashes hash ``payload``.
    """
    pieces = compute_piece_hashes(payload, piece_length)
    if paths is not None:
        layout = [list(parts) for parts in paths]
    elif file_count == 1:
        # Single-file torrent: the file *is* the torrent name.
        layout = [[name]]
    else:
        layout = default_paths(file_count)
    chunks = split_payload(payload, len(layout))

    if len(layout) == 1 and layout[0] == [name]:
        info = build_info(
            name=name,
            piece_length=piece_length,
            pieces=pieces,
            length=len(payload),
            private=private,
        )
    else:
        info = build_info(
            name=name,
            piece_length=piece_length,
            pieces=pieces,
            files=list(zip(layout, (len(chunk) for chunk in chunks), strict=True)),
            private=private,
        )

    document: dict[bytes, BencodeValue] = {b"info": info, b"created by": CREATED_BY.encode()}
    if announce:
        document[b"announce"] = announce.encode("utf-8")
    if announce_list:
        document[b"announce-list"] = [
            [url.encode("utf-8") for url in tier] for tier in announce_list
        ]
    if comment:
        document[b"comment"] = comment.encode("utf-8")
    return encode(document)


def write_test_torrent(
    directory: str | Path,
    *,
    size: int,
    name: str = DEFAULT_NAME,
    piece_length: int = DEFAULT_PIECE_LENGTH,
    file_count: int = 1,
    announce: str | None = DEFAULT_ANNOUNCE,
    seed: int = 1234,
    torrent_filename: str | None = None,
) -> tuple[Path, list[Path]]:
    """Write a payload and its matching ``.torrent`` to disk.

    Files are laid out exactly as a completed download would be:
    ``<directory>/<name>`` for single-file torrents and
    ``<directory>/<name>/<path...>`` for multi-file ones, so the directory can
    be handed straight to a mock seeder.

    Args:
        directory: Where to write the payload and torrent.
        size: Total payload size in bytes.
        name: Torrent name (also the payload root name).
        piece_length: Piece size in bytes.
        file_count: Number of payload files (1 = single-file torrent).
        announce: Tracker URL to embed.
        seed: PRNG seed for the payload.
        torrent_filename: Override for the ``.torrent`` file name.

    Returns:
        ``(torrent_path, payload_paths)``.
    """
    root = Path(directory)
    payload = generate_payload(size, seed=seed)
    layout = default_paths(file_count)
    chunks = split_payload(payload, len(layout))

    payload_paths: list[Path] = []
    if file_count == 1:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        payload_paths.append(target)
    else:
        for parts, chunk in zip(layout, chunks, strict=True):
            target = root / name / Path(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(chunk)
            payload_paths.append(target)

    torrent_bytes = build_torrent_bytes(
        payload,
        name=name,
        piece_length=piece_length,
        paths=layout if file_count > 1 else None,
        file_count=file_count,
        announce=announce,
    )
    torrent_path = root / (torrent_filename or f"{name}.torrent")
    torrent_path.write_bytes(torrent_bytes)
    return torrent_path, payload_paths


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Generate a deterministic test payload and .torrent file.",
    )
    parser.add_argument(
        "--size", default="4MiB", help="total payload size (e.g. 4MiB, 512K, 1048576)"
    )
    parser.add_argument(
        "--out", default="data/torrents/test.torrent", help="path of the .torrent to write"
    )
    parser.add_argument(
        "--dir", default=None, help="directory for the payload (default: alongside the torrent)"
    )
    parser.add_argument("--name", default=DEFAULT_NAME, help="torrent name")
    parser.add_argument(
        "--piece-length", type=int, default=DEFAULT_PIECE_LENGTH, help="piece size in bytes"
    )
    parser.add_argument(
        "--files", type=int, default=1, help="number of files (1 = single-file torrent)"
    )
    parser.add_argument("--announce", default=DEFAULT_ANNOUNCE, help="tracker URL to embed")
    parser.add_argument("--seed", type=int, default=1234, help="PRNG seed for the payload")
    parser.add_argument("--no-payload", action="store_true", help="write the .torrent only")
    args = parser.parse_args(argv)

    size = parse_size(args.size)
    out_path = Path(args.out)
    target_dir = Path(args.dir) if args.dir else out_path.parent

    payload = generate_payload(size, seed=args.seed)
    layout = default_paths(args.files)
    torrent_bytes = build_torrent_bytes(
        payload,
        name=args.name,
        piece_length=args.piece_length,
        paths=layout if args.files > 1 else None,
        file_count=args.files,
        announce=args.announce,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(torrent_bytes)

    written: list[Path] = []
    if not args.no_payload:
        target_dir.mkdir(parents=True, exist_ok=True)
        chunks = split_payload(payload, len(layout))
        if args.files == 1:
            (target_dir / args.name).write_bytes(payload)
            written.append(target_dir / args.name)
        else:
            for parts, chunk in zip(layout, chunks, strict=True):
                target = target_dir / args.name / Path(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(chunk)
                written.append(target)

    from app.torrent import parse_torrent

    torrent = parse_torrent(torrent_bytes)
    print(f"torrent : {out_path}")
    print(f"name    : {torrent.name}")
    print(f"size    : {torrent.total_length:,} bytes in {len(torrent.files)} file(s)")
    print(f"pieces  : {torrent.piece_count} x {torrent.piece_length:,} bytes")
    print(f"infohash: {torrent.hex_info_hash}")
    print(f"announce: {args.announce}")
    if written:
        print(f"payload : {written[0].parent} ({len(written)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
