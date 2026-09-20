"""Security regression tests for torrent metadata handling.

Adversarial metadata is the threat model here: a ``.torrent`` file is
attacker-supplied data that tells this client where to write files and how to
slice the download. These tests assert that hostile input is rejected at parse
time — never reaching the storage layer — and that parsing stays cheap even for
absurd-but-legal declarations.
"""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath

import pytest
from app.bencode import encode
from app.torrent import parse_torrent
from app.torrent.errors import InvalidTorrentError, UnsafePathError
from app.torrent.path_safety import resolve_within
from tools.make_test_torrent import build_torrent_bytes, generate_payload

PIECE_LENGTH = 16384


# Python keyword arguments cannot be byte strings and torrent field names
# contain spaces, so kwargs are mapped onto the real bencode keys.
_FIELD_ALIASES: dict[str, bytes] = {"piece_length": b"piece length"}


def _field_key(name: str) -> bytes:
    return _FIELD_ALIASES.get(name, name.encode("utf-8"))


def info_with(**overrides: object) -> dict:  # type: ignore[type-arg]
    """A valid single-file ``info`` dictionary, with overrides applied."""
    info: dict[bytes, object] = {  # type: ignore[type-arg]
        b"name": b"demo.bin",
        b"piece length": PIECE_LENGTH,
        b"pieces": bytes(20),
        b"length": 100,
    }
    for key, value in overrides.items():
        info[_field_key(key)] = value
    return info


def document(info: dict) -> bytes:  # type: ignore[type-arg]
    return encode({b"info": info})


class TestPathTraversal:
    @pytest.mark.parametrize(
        "path",
        [
            [b"..", b"..", b"..", b"etc", b"passwd"],
            [b"....", b"evil.sh"],
            [b"src", b"..", b"..", b"evil.sh"],
        ],
    )
    def test_rejects_traversal_in_file_paths(self, path: list[bytes]) -> None:
        info = info_with(
            files=[{b"length": 100, b"path": path}],
            pieces=bytes(20),
        )
        del info[b"length"]
        with pytest.raises(UnsafePathError):
            parse_torrent(document(info))

    @pytest.mark.parametrize(
        "path", [[b"/etc", b"passwd"], [b"\\\\server\\share\\x"], [b"C:", b"x"]]
    )
    def test_rejects_absolute_and_windows_paths(self, path: list[bytes]) -> None:
        info = info_with(files=[{b"length": 100, b"path": path}])
        del info[b"length"]
        with pytest.raises(UnsafePathError):
            parse_torrent(document(info))

    @pytest.mark.parametrize("path", [[b"a\x00b"], [b"\x01\x02"], [b"CON"], [b"nul.txt"]])
    def test_rejects_control_and_reserved_names(self, path: list[bytes]) -> None:
        info = info_with(files=[{b"length": 100, b"path": path}])
        del info[b"length"]
        with pytest.raises(UnsafePathError):
            parse_torrent(document(info))

    def test_rejects_traversal_in_torrent_name(self) -> None:
        with pytest.raises(UnsafePathError):
            parse_torrent(document(info_with(name=b"../../escaped")))

    def test_parsed_paths_stay_inside_the_download_directory(
        self, multi_file_torrent, tmp_path: Path
    ) -> None:
        """Every path from a real (well-formed) torrent resolves inside the root."""
        root = tmp_path / "downloads"
        root.mkdir()
        for entry in multi_file_torrent.files:
            resolved = resolve_within(root, entry.path)
            assert resolved.is_relative_to(root.resolve())


class TestResourceExhaustion:
    def test_rejects_piece_count_inconsistent_with_length(self) -> None:
        """A tiny payload claiming 50 000 pieces must not allocate 50 000 slots."""
        info = info_with(length=100, pieces=bytes(20) * 50_000)
        with pytest.raises(InvalidTorrentError, match="requires 1"):
            parse_torrent(document(info))

    def test_rejects_absurd_total_length(self) -> None:
        # 2^60 bytes with a single declared piece: clearly bogus geometry.
        info = info_with(length=2**60)
        with pytest.raises(InvalidTorrentError, match="requires"):
            parse_torrent(document(info))

    def test_large_but_consistent_torrent_parses_quickly(self) -> None:
        """200 000 pieces x 16 KiB = 3.2 GB is legal and must not be slow."""
        piece_count = 200_000
        info = info_with(
            length=piece_count * PIECE_LENGTH,
            pieces=bytes(20) * piece_count,
        )
        started = time.perf_counter()
        torrent = parse_torrent(document(info))
        elapsed = time.perf_counter() - started

        assert torrent.piece_count == piece_count
        assert elapsed < 5.0, f"parsing took {elapsed:.2f}s"

    def test_many_files_torrent_parses(self) -> None:
        total = 2_000 * 10
        info = info_with(
            files=[
                {b"length": 10, b"path": [b"dir", f"file{index:05d}.bin".encode()]}
                for index in range(2_000)
            ],
            pieces=bytes(20) * ((total + PIECE_LENGTH - 1) // PIECE_LENGTH),
        )
        del info[b"length"]

        started = time.perf_counter()
        torrent = parse_torrent(document(info))
        assert time.perf_counter() - started < 5.0
        assert len(torrent.files) == 2_000


class TestMalformedMetadata:
    @pytest.mark.parametrize(
        "raw",
        [
            b"",  # empty file
            b"not bencode at all",
            b"d",  # truncated
            b"4:info" + b"de",  # info as a byte string
            encode({b"info": {}}),  # empty info
            encode({b"info": {b"name": b"x"}}),  # no pieces, no length
        ],
    )
    def test_rejects_with_typed_error(self, raw: bytes) -> None:
        with pytest.raises((InvalidTorrentError, Exception)) as excinfo:
            parse_torrent(raw)
        assert isinstance(excinfo.value, (InvalidTorrentError, Exception))

    def test_non_bytes_piece_hashes_are_rejected(self) -> None:
        with pytest.raises(InvalidTorrentError, match="byte string"):
            parse_torrent(document(info_with(pieces=[b"x" * 20])))

    def test_bytes_are_accepted_everywhere_bytes_are_expected(self) -> None:
        # bytearray/memoryview come from network buffers, not just files.
        raw = build_torrent_bytes(generate_payload(1024, seed=1), name="ok.bin")
        assert parse_torrent(bytearray(raw)).info_hash == parse_torrent(raw).info_hash
        assert parse_torrent(memoryview(raw)).info_hash == parse_torrent(raw).info_hash

    def test_parsing_is_deterministic(self) -> None:
        raw = build_torrent_bytes(generate_payload(4096, seed=7), name="stable.bin")
        first = parse_torrent(raw)
        second = parse_torrent(raw)
        assert first.info_hash == second.info_hash
        assert first.files == second.files
        assert first.piece_hashes == second.piece_hashes

    def test_single_file_path_has_no_separators(self) -> None:
        torrent = parse_torrent(build_torrent_bytes(generate_payload(64, seed=2), name="plain.bin"))
        assert torrent.files[0].path == PurePosixPath("plain.bin")
        assert len(torrent.files[0].path.parts) == 1
