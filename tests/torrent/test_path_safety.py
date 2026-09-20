"""Unit tests for torrent path sanitisation.

A torrent's file list is a remote instruction set for writing to the local
disk, so this module gets adversarial input on purpose. Every case here
corresponds to a way a hostile or merely broken torrent could otherwise escape
the download directory (TRD §41: "path traversal in multi-file torrents").
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from app.torrent.errors import UnsafePathError
from app.torrent.path_safety import (
    decode_component,
    resolve_within,
    sanitize_name,
    sanitize_path_components,
    validate_component,
)


class TestComponentValidation:
    @pytest.mark.parametrize(
        "component",
        [
            "",  # empty
            ".",  # current directory
            "..",  # parent directory
            "a/b",  # POSIX separator
            "a\\b",  # Windows separator
            "a\x00b",  # NUL termination trick
            "\x01\x02",  # control characters
            "C:",  # Windows drive
            "c:something",  # NTFS alternate data stream
            "CON",  # reserved device name
            "con.txt",  # reserved name with extension
            "NUL",
            "COM1",
            "LPT9",
            " name",  # leading whitespace
            "name ",  # trailing whitespace
            "name.",  # trailing dot (aliases on Windows)
        ],
    )
    def test_rejects_unsafe_components(self, component: str) -> None:
        with pytest.raises(UnsafePathError):
            validate_component(component, field="path[0]")

    @pytest.mark.parametrize(
        "component",
        ["normal.bin", "file with spaces.tar.gz", "Ünïcödé-файл.bin", ".hidden", "a" * 255],
    )
    def test_accepts_safe_components(self, component: str) -> None:
        validate_component(component, field="path[0]")

    def test_rejects_over_long_component(self) -> None:
        with pytest.raises(UnsafePathError, match="exceeds 255 bytes"):
            validate_component("a" * 256, field="path[0]")

    def test_rejects_over_long_utf8_component(self) -> None:
        # 100 characters, 200 bytes: legal by character count, too long in bytes.
        with pytest.raises(UnsafePathError, match="exceeds 255 bytes"):
            validate_component("é" * 200, field="path[0]")


class TestDecoding:
    def test_decodes_utf8(self) -> None:
        assert decode_component("Ünïcödé.bin".encode(), field="name") == "Ünïcödé.bin"

    def test_falls_back_to_latin1_for_legacy_encodings(self) -> None:
        # Torrents created before the UTF-8 convention may use latin-1 names.
        assert decode_component(b"caf\xe9.txt", field="name") == "café.txt"

    def test_decoding_does_not_bypass_validation(self) -> None:
        with pytest.raises(UnsafePathError):
            decode_component(b"..", field="name")


class TestNameSanitisation:
    def test_accepts_plain_name(self) -> None:
        assert sanitize_name(b"ubuntu.iso") == "ubuntu.iso"

    def test_accepts_unicode_name(self) -> None:
        assert sanitize_name("файл.bin".encode()) == "файл.bin"

    def test_rejects_traversal(self) -> None:
        # "../../evil" is one component containing separators, so it is caught by
        # the separator rule; ".." alone is caught by the traversal rule.
        with pytest.raises(UnsafePathError, match=r"traverse|forbidden"):
            sanitize_name(b"../../evil")
        with pytest.raises(UnsafePathError, match="traverse"):
            sanitize_name(b"..")

    def test_rejects_absolute_path(self) -> None:
        with pytest.raises(UnsafePathError, match="forbidden"):
            sanitize_name(b"/etc/passwd")

    def test_error_names_the_field(self) -> None:
        with pytest.raises(UnsafePathError) as excinfo:
            sanitize_name(b"..", field="info.name")
        assert excinfo.value.field == "info.name"


class TestPathComponents:
    def test_joins_components(self) -> None:
        path = sanitize_path_components([b"src", b"main.py"])
        assert path == PurePosixPath("src/main.py")
        assert isinstance(path, PurePosixPath)

    def test_single_component(self) -> None:
        assert sanitize_path_components([b"README.md"]) == PurePosixPath("README.md")

    def test_rejects_empty_list(self) -> None:
        with pytest.raises(UnsafePathError, match="empty path"):
            sanitize_path_components([])

    def test_rejects_traversal_component(self) -> None:
        with pytest.raises(UnsafePathError, match="traverse"):
            sanitize_path_components([b"src", b"..", b"evil.py"])

    def test_rejects_absolute_component(self) -> None:
        with pytest.raises(UnsafePathError, match="forbidden"):
            sanitize_path_components([b"/etc", b"passwd"])

    def test_rejects_non_bytes_component(self) -> None:
        with pytest.raises(UnsafePathError, match="must be a byte string"):
            sanitize_path_components(["src"])  # type: ignore[list-item]

    def test_error_reports_the_component_index(self) -> None:
        with pytest.raises(UnsafePathError) as excinfo:
            sanitize_path_components([b"ok", b".."], field="info.files[3].path")
        assert excinfo.value.field == "info.files[3].path[1]"

    def test_rejects_over_long_total_path(self) -> None:
        # 120 components x 40 chars > 4096 bytes of path.
        long_path = [b"c" * 40] * 120
        with pytest.raises(UnsafePathError, match="path is too long"):
            sanitize_path_components(long_path)

    def test_deep_nesting_is_allowed(self) -> None:
        path = sanitize_path_components([b"a", b"b", b"c", b"d", b"e.bin"])
        assert str(path) == "a/b/c/d/e.bin"


class TestResolveWithin:
    def test_joins_inside_base(self, tmp_path: Path) -> None:
        resolved = resolve_within(tmp_path, PurePosixPath("bundle/file.bin"))
        assert resolved == (tmp_path / "bundle" / "file.bin").resolve()

    def test_rejects_escaping_path(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafePathError, match="escapes the download directory"):
            resolve_within(tmp_path, PurePosixPath("../../etc/passwd"))

    def test_allows_the_root_itself(self, tmp_path: Path) -> None:
        assert resolve_within(tmp_path, PurePosixPath(".")) == tmp_path.resolve()

    def test_detects_symlink_escape(self, tmp_path: Path) -> None:
        """Even a sanitised name cannot smuggle data out through a symlink."""
        outside = tmp_path.parent / f"outside-{tmp_path.name}"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "link"
        if not link.exists():
            link.symlink_to(outside, target_is_directory=True)

        with pytest.raises(UnsafePathError):
            resolve_within(link, PurePosixPath("../../escaped.bin"))
