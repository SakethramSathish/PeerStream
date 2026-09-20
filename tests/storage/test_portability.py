"""Storage IO on a platform that is not POSIX.

:func:`app.storage.files.write_chunk` prefers ``os.pwrite`` and
:func:`~app.storage.files.read_chunk` prefers ``os.pread``, and neither exists on
Windows. Every ``os.open`` in the module also asks for ``O_CLOEXEC``, which does
not exist there either, and preallocation prefers ``posix_fallocate``, which
likewise does not. All four degrade rather than fail.

A Linux test runner cannot become Windows, but it can delete the attributes,
which is the same thing as far as this code is concerned: ``_POSITIONAL_IO`` and
``_CLOEXEC`` are computed once at import, so the tests either patch the flag or
re-import the module in a subprocess with the attributes already gone. The
subprocess test is the one that would catch an ``AttributeError`` at import time,
which is how this module would actually break on Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from app.storage import files
from app.storage.files import (
    preallocate_file,
    read_chunk,
    write_chunk,
)

REGION = 4096

# Held before any test patches anything, so the patched functions can delegate
# to the real ones for every descriptor except the one under test.
real_write = os.write
real_read = os.read


def track_descriptor(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Return the set of descriptors the code under test seeks.

    The non-POSIX fallback seeks before it reads or writes, so the descriptor it
    seeks is the one it is about to touch — and the only one a test may safely
    make fail without taking pytest's own capture down with it.
    """
    seen: set[int] = set()
    real_lseek = os.lseek

    def tracking_lseek(fd: int, offset: int, whence: int) -> int:
        seen.add(fd)
        return real_lseek(fd, offset, whence)

    monkeypatch.setattr(os, "lseek", tracking_lseek)
    return seen


@pytest.fixture
def no_positional_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ``os.pwrite``/``os.pread`` do not exist."""
    monkeypatch.setattr(files, "_POSITIONAL_IO", False)


class TestWriteFallback:
    """Writing without ``pwrite`` has to land in exactly the same bytes."""

    def test_a_write_lands_at_the_offset_it_asked_for(
        self, tmp_path: Path, no_positional_io: None
    ) -> None:
        path = tmp_path / "data.bin"
        path.write_bytes(b"\x00" * 16)

        assert write_chunk(path, 4, b"hello") == 5

        data = path.read_bytes()
        assert data[4:9] == b"hello"
        assert data[:4] == b"\x00" * 4
        assert data[9:] == b"\x00" * 7

    def test_a_write_past_the_end_grows_the_file(
        self, tmp_path: Path, no_positional_io: None
    ) -> None:
        path = tmp_path / "data.bin"

        assert write_chunk(path, 8, b"tail") == 4

        data = path.read_bytes()
        assert len(data) == 12
        assert data[8:] == b"tail"

    def test_the_fallback_never_reaches_for_pwrite(
        self, tmp_path: Path, no_positional_io: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def absent(*args: object, **kwargs: object) -> int:
            raise AssertionError("pwrite was called on a platform that does not have it")

        monkeypatch.setattr(os, "pwrite", absent, raising=False)
        path = tmp_path / "data.bin"

        assert write_chunk(path, 0, b"x" * REGION) == REGION

    def test_a_failed_write_is_still_reported(
        self, tmp_path: Path, no_positional_io: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the descriptor write_chunk opened may fail. Patching os.write
        # outright would also break pytest's own capture, and a test that fails
        # for that reason is testing nothing.
        seen = track_descriptor(monkeypatch)

        def selective_write(fd: int, data: Any) -> int:
            if fd in seen:
                raise OSError(5, "I/O error")
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", selective_write)

        with pytest.raises(files.AllocationError, match="write to"):
            write_chunk(tmp_path / "data.bin", 0, b"data")

    def test_concurrent_writers_do_not_interleave(
        self, tmp_path: Path, no_positional_io: None
    ) -> None:
        """The property ``pwrite`` exists for, held by the fallback too.

        Each call owns its descriptor, so a seek on it cannot move another
        writer's position. Eight threads writing eight distinct regions is the
        test that would fail if that reasoning were wrong.
        """
        path = tmp_path / "data.bin"
        path.write_bytes(b"\x00" * (REGION * 8))
        failures: list[Exception] = []

        def fill(index: int) -> None:
            try:
                # Four rounds each: one write per thread would rarely collide.
                for _ in range(4):
                    write_chunk(path, index * REGION, bytes([index + 1]) * REGION)
            except Exception as exc:  # noqa: BLE001 - reported, not raised, below
                failures.append(exc)

        threads = [threading.Thread(target=fill, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert failures == []
        data = path.read_bytes()
        for index in range(8):
            region = data[index * REGION : (index + 1) * REGION]
            assert region == bytes([index + 1]) * REGION, f"region {index} was corrupted"


class TestReadFallback:
    """Reading without ``pread`` has to return exactly the same bytes."""

    def test_a_read_returns_the_bytes_at_the_offset(
        self, tmp_path: Path, no_positional_io: None
    ) -> None:
        path = tmp_path / "data.bin"
        path.write_bytes(b"0123456789")

        assert read_chunk(path, 3, 4) == b"3456"

    def test_a_read_past_the_end_is_short_not_an_error(
        self, tmp_path: Path, no_positional_io: None
    ) -> None:
        path = tmp_path / "data.bin"
        path.write_bytes(b"abc")

        assert read_chunk(path, 1, 100) == b"bc"
        assert read_chunk(path, 99, 10) == b""

    def test_a_read_larger_than_one_block_arrives_whole(
        self, tmp_path: Path, no_positional_io: None
    ) -> None:
        path = tmp_path / "data.bin"
        payload = os.urandom(REGION * 3)
        path.write_bytes(payload)

        assert read_chunk(path, 0, len(payload)) == payload

    def test_the_fallback_never_reaches_for_pread(
        self, tmp_path: Path, no_positional_io: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def absent(*args: object, **kwargs: object) -> bytes:
            raise AssertionError("pread was called on a platform that does not have it")

        monkeypatch.setattr(os, "pread", absent, raising=False)
        path = tmp_path / "data.bin"
        path.write_bytes(b"x" * REGION)

        assert read_chunk(path, 0, REGION) == b"x" * REGION

    def test_a_failed_read_keeps_what_arrived(
        self, tmp_path: Path, no_positional_io: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "data.bin"
        path.write_bytes(b"abcdef")
        seen = track_descriptor(monkeypatch)
        calls = {"n": 0}

        def flaky(fd: int, length: int) -> bytes:
            if fd not in seen:
                return real_read(fd, length)
            calls["n"] += 1
            if calls["n"] == 1:
                return b"abc"
            raise OSError(5, "I/O error")

        monkeypatch.setattr(os, "read", flaky)

        assert read_chunk(path, 0, 6) == b"abc"


class TestAllocationFallback:
    """Preallocation without ``posix_fallocate`` still leaves a file of the size."""

    def test_a_file_is_sized_by_truncate_instead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(os, "posix_fallocate", raising=False)
        path = tmp_path / "data.bin"

        assert preallocate_file(path, REGION) == REGION

        assert path.stat().st_size == REGION

    def test_an_already_big_enough_file_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(os, "posix_fallocate", raising=False)
        path = tmp_path / "data.bin"
        path.write_bytes(b"x" * REGION)

        assert preallocate_file(path, 16) == 0


class TestNothingPosixAtAll:
    """Import the module with every POSIX-only attribute already gone.

    This is the test that fails if a future edit reintroduces a bare
    ``os.O_CLOEXEC`` or a module-level ``os.pwrite`` reference: those raise
    ``AttributeError`` at import, before any test here could patch anything.
    """

    def test_the_module_works_without_pwrite_pread_cloexec_or_fallocate(
        self, tmp_path: Path
    ) -> None:
        script = f"""
import os, sys, pathlib
for name in ("O_CLOEXEC", "pwrite", "pread", "posix_fallocate"):
    if hasattr(os, name):
        delattr(os, name)
from app.storage import files
assert files._POSITIONAL_IO is False, files._POSITIONAL_IO
assert files._CLOEXEC == 0, files._CLOEXEC
path = pathlib.Path({str(tmp_path)!r}) / "f.bin"
assert files.preallocate_file(path, 32) == 32, path.stat().st_size
assert files.write_chunk(path, 4, b"hello") == 5
assert files.read_chunk(path, 4, 5) == b"hello"
assert files.read_chunk(path, 0, 32)[4:9] == b"hello"
assert path.stat().st_size == 32
print("OK")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "OK"
