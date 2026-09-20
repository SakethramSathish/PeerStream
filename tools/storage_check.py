"""Check the storage layer against a real payload, end to end.

The unit tests prove the mapping arithmetic is right. This tool proves the
whole path works on a real filesystem: create files, hash every piece, write
it, read it back, re-verify it on "restart", and persist resume state.

Every number printed is measured — file sizes come from ``stat``, piece counts
from what was actually written, and the verification result from hashing bytes
that are genuinely on disk.

Examples::

    # generate an 8 MiB, 4-file torrent in a temporary directory and store it
    python tools/storage_check.py --size 8MiB --files 4 --out /tmp/storage-check

    # store a real torrent whose data you already have
    python tools/storage_check.py --torrent debian.torrent --payload debian.iso \\
        --out /tmp/storage-check

    # prove a corrupt piece is rejected before it reaches the disk
    python tools/storage_check.py --size 1MiB --corrupt 3 --out /tmp/storage-check
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.config import StorageConfig
from app.storage import StorageManager
from app.torrent import Torrent, parse_torrent, parse_torrent_file

from tools.make_test_torrent import (
    build_torrent_bytes,
    generate_payload,
    parse_size,
)

logger = logging.getLogger(__name__)

DEFAULT_SIZE: str = "8MiB"
DEFAULT_PIECE_LENGTH: int = 256 * 1024
DEFAULT_FILE_COUNT: int = 4
DEFAULT_NAME: str = "storage-check"
DEFAULT_SEED: int = 2024


@dataclass(frozen=True, slots=True)
class FileCheck:
    """One file as declared by the torrent and as found on disk.

    Attributes:
        path: Path relative to the download directory.
        declared: Size the torrent promised.
        on_disk: Size actually found after the run.
    """

    path: str
    declared: int
    on_disk: int

    @property
    def ok(self) -> bool:
        """True when the file is exactly the size the torrent declared."""
        return self.declared == self.on_disk


@dataclass(frozen=True, slots=True)
class StorageCheck:
    """Everything one run of the storage layer produced."""

    name: str
    hex_info_hash: str
    total_length: int
    piece_count: int
    piece_length: int
    files: tuple[FileCheck, ...]
    pieces_written: int
    pieces_rejected: tuple[int, ...]
    valid_pieces: int
    invalid_pieces: tuple[int, ...]
    resumed_pieces: int
    resume_path: str
    directory: str
    bytes_allocated: int
    preallocated: bool
    elapsed: float

    @property
    def files_ok(self) -> bool:
        """True when every file has the size the torrent promised."""
        return all(file.ok for file in self.files)

    @property
    def bytes_on_disk(self) -> int:
        """Total bytes found on disk across all files."""
        return sum(file.on_disk for file in self.files)

    @property
    def successful(self) -> bool:
        """True when nothing was rejected and every file matches the torrent."""
        return not self.pieces_rejected and not self.invalid_pieces and self.files_ok


def corrupt_piece(data: bytes) -> bytes:
    """Flip one bit in the middle of a piece, keeping its length."""
    if not data:
        return data
    middle = len(data) // 2
    return data[:middle] + bytes([data[middle] ^ 0x01]) + data[middle + 1 :]


async def run_check(
    torrent: Torrent,
    payload: bytes,
    *,
    directory: Path,
    state_directory: Path,
    preallocate: bool = True,
    corrupt: Sequence[int] = (),
) -> StorageCheck:
    """Store a whole payload through the storage layer and check the result.

    Args:
        torrent: The torrent describing ``payload``.
        payload: The real data; piece hashes were computed over exactly this.
        directory: Where to write the files.
        state_directory: Where resume state is kept.
        preallocate: Whether to reserve the full file sizes up front.
        corrupt: Piece indices to corrupt on purpose, to prove that a bad hash
            is caught before the data reaches a file.

    Returns:
        The measured outcome.

    Raises:
        ValueError: If the payload is not the size the torrent describes.
    """
    if len(payload) != torrent.total_length:
        raise ValueError(
            f"payload is {len(payload)} bytes but {torrent.name} describes "
            f"{torrent.total_length} bytes"
        )

    config = StorageConfig(preallocate_files=preallocate, state_directory=state_directory)
    started = time.monotonic()
    storage = StorageManager(torrent, directory, config=config)
    allocation = await storage.prepare()

    rejected: list[int] = []
    for index in range(torrent.piece_count):
        start = torrent.piece_offset(index)
        piece = payload[start : start + torrent.piece_size(index)]
        if index in set(corrupt):
            piece = corrupt_piece(piece)
        try:
            await storage.write_piece(index, piece)
        except Exception as exc:  # noqa: BLE001 - a rejected piece is data, not a crash
            rejected.append(index)
            logger.info("piece %d rejected: %s", index, exc)

    report = await storage.verify_completed()
    sizes = await storage.on_disk_sizes()
    resume_path = await storage.save_resume()

    # A second manager is a stand-in for a restart: it knows nothing until it
    # reads the state file back.
    restarted = StorageManager(torrent, directory, config=config)
    state = await restarted.load_resume()
    resumed = len(state.completed_pieces) if state is not None else 0
    elapsed = time.monotonic() - started

    check = StorageCheck(
        name=torrent.name,
        hex_info_hash=torrent.hex_info_hash,
        total_length=torrent.total_length,
        piece_count=torrent.piece_count,
        piece_length=torrent.piece_length,
        files=tuple(
            FileCheck(
                path=str(mapped.relative_path),
                declared=mapped.length,
                on_disk=size,
            )
            for mapped, size in zip(storage.files, sizes, strict=True)
        ),
        pieces_written=len(storage.completed_pieces),
        pieces_rejected=tuple(rejected),
        valid_pieces=len(report.valid),
        invalid_pieces=report.invalid,
        resumed_pieces=resumed,
        resume_path=str(resume_path),
        directory=str(directory),
        bytes_allocated=allocation.bytes_allocated,
        preallocated=allocation.preallocated,
        elapsed=elapsed,
    )
    await storage.aclose()
    await restarted.aclose()
    return check


def _human_bytes(value: int) -> str:
    """Render a byte count the way a download client should."""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def render(check: StorageCheck, *, title: str) -> None:
    """Print a storage check report.

    Args:
        check: The measured outcome.
        title: Heading printed above the report.
    """
    print(title)
    print(f"  info hash    {check.hex_info_hash}")
    print(
        f"  size         {_human_bytes(check.total_length)} "
        f"in {len(check.files)} file(s), {check.piece_count} pieces of "
        f"{_human_bytes(check.piece_length)}"
    )
    print(f"  directory    {check.directory}")
    print()

    for file in check.files:
        status = "ok  " if file.ok else "BAD "
        print(f"  {status} {file.path:<40} {file.on_disk:>12} / {file.declared:<12} bytes")

    print()
    print(
        f"  wrote {check.pieces_written}/{check.piece_count} pieces in "
        f"{check.elapsed:.2f}s ({_human_bytes(check.bytes_on_disk)} on disk)"
    )
    if check.preallocated:
        print(f"  preallocated {_human_bytes(check.bytes_allocated)}")
    else:
        print("  preallocation disabled (files grew as data arrived)")
    print(f"  verified     {check.valid_pieces} piece(s) re-hashed from disk")
    if check.invalid_pieces:
        print(
            f"  invalid      {len(check.invalid_pieces)} piece(s): {sorted(check.invalid_pieces)}"
        )
    if check.pieces_rejected:
        print(
            f"  rejected     {len(check.pieces_rejected)} piece(s) before writing: "
            f"{sorted(check.pieces_rejected)}"
        )
    print(f"  resume       {check.resumed_pieces} piece(s) restored from {check.resume_path}")

    print()
    if check.successful:
        print("  RESULT ok: every byte on disk hashes to what the torrent promised")
    else:
        print("  RESULT failed: see the lines above for the pieces and files involved")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="storage_check",
        description="Store a payload through the storage layer and verify every byte.",
    )
    parser.add_argument("--torrent", type=Path, help="use an existing .torrent file")
    parser.add_argument("--payload", type=Path, help="data to store (required with --torrent)")
    parser.add_argument(
        "--size",
        default=DEFAULT_SIZE,
        help=f"size of the generated payload (default: {DEFAULT_SIZE})",
    )
    parser.add_argument(
        "--piece-length",
        type=int,
        default=DEFAULT_PIECE_LENGTH,
        help=f"piece size for a generated torrent (default: {DEFAULT_PIECE_LENGTH})",
    )
    parser.add_argument(
        "--files",
        type=int,
        default=DEFAULT_FILE_COUNT,
        help=f"files in a generated torrent (default: {DEFAULT_FILE_COUNT})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/storage-check"),
        help="download directory (default: data/storage-check)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="resume state directory (default: <out>/../state)",
    )
    parser.add_argument(
        "--no-preallocate",
        action="store_true",
        help="let files grow as data arrives instead of reserving space up front",
    )
    parser.add_argument(
        "--corrupt",
        type=int,
        action="append",
        default=[],
        metavar="PIECE",
        help="deliberately corrupt a piece to prove it is rejected (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Returns:
        0 when the storage layer did everything the torrent promised (pieces
        corrupted on purpose with ``--corrupt`` do not count as failures),
        1 otherwise.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.torrent:
        try:
            torrent = parse_torrent_file(args.torrent)
        except Exception as exc:  # noqa: BLE001 - a diagnostic tool reports, never crashes
            print(f"cannot read torrent: {exc}", file=sys.stderr)
            return 1
        if args.payload is None:
            print("--payload is required with --torrent", file=sys.stderr)
            return 1
        try:
            payload = args.payload.read_bytes()
        except OSError as exc:
            print(f"cannot read payload: {exc}", file=sys.stderr)
            return 1
    else:
        payload = generate_payload(parse_size(args.size), seed=DEFAULT_SEED)
        torrent = parse_torrent(
            build_torrent_bytes(
                payload,
                name=DEFAULT_NAME,
                piece_length=args.piece_length,
                file_count=max(1, args.files),
            )
        )

    directory: Path = args.out
    state_directory = args.state_dir or directory.parent / "state"
    try:
        check = asyncio.run(
            run_check(
                torrent,
                payload,
                directory=directory,
                state_directory=state_directory,
                preallocate=not args.no_preallocate,
                corrupt=args.corrupt,
            )
        )
    except ValueError as exc:
        print(f"cannot check storage: {exc}", file=sys.stderr)
        return 1

    render(check, title=f"Storage check: {check.name}")
    unexpected = (set(check.invalid_pieces) | set(check.pieces_rejected)) - set(args.corrupt)
    if unexpected or not check.files_ok:
        return 1
    if check.pieces_rejected:
        print(f"\n  (--corrupt: piece(s) {sorted(check.pieces_rejected)} rejected as asked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
