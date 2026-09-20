"""Unit tests for the .torrent parser.

The parser is where untrusted metadata becomes a trusted domain object, so the
tests are weighted heavily toward rejection: for every rule in TRD §41 and
PRD §13 FR-01 there is a case proving a bad torrent is refused with a specific,
actionable error rather than a generic failure.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import pytest
from app.bencode import encode
from app.torrent import (
    parse_torrent,
    parse_torrent_file,
    torrent_from_info,
)
from app.torrent.errors import InvalidTorrentError, TorrentParseError, UnsafePathError
from app.torrent.info_hash import compute_info_hash
from tools.make_test_torrent import compute_piece_hashes

from tests.torrent.test_info_hash import UNSORTED_TORRENT_INFO

PIECE_LENGTH = 16384


def pieces_for(total_length: int, piece_length: int = PIECE_LENGTH) -> bytes:
    """Piece-hash blob for a synthetic payload of ``total_length`` bytes."""
    payload = bytes((index * 7) % 256 for index in range(total_length))
    return compute_piece_hashes(payload, piece_length)


# Python keyword arguments cannot be byte strings, and torrent field names
# contain spaces and dashes. These helpers map readable kwargs onto the real
# bencode keys.
_FIELD_ALIASES: dict[str, bytes] = {
    "announce_list": b"announce-list",
    "piece_length": b"piece length",
    "creation_date": b"creation date",
    "created_by": b"created by",
}


def _field_key(name: str) -> bytes:
    return _FIELD_ALIASES.get(name, name.encode("utf-8"))


def single_file_info(**overrides: object) -> dict:  # type: ignore[type-arg]
    """A valid single-file ``info`` dictionary, with overrides applied."""
    info: dict[bytes, object] = {  # type: ignore[type-arg]
        b"name": b"demo.bin",
        b"piece length": PIECE_LENGTH,
        b"pieces": pieces_for(100),
        b"length": 100,
    }
    for key, value in overrides.items():
        info[_field_key(key)] = value
    return info


def torrent_bytes(info: dict, **top_level: object) -> bytes:  # type: ignore[type-arg]
    """Bencode a torrent document with the given info dict and top-level fields."""
    document: dict[bytes, object] = {b"info": info}  # type: ignore[type-arg]
    for key, value in top_level.items():
        document[_field_key(key)] = value
    return encode(document)


class TestSingleFile:
    def test_parses_minimal_torrent(self) -> None:
        torrent = parse_torrent(torrent_bytes(single_file_info()))

        assert torrent.name == "demo.bin"
        assert torrent.total_length == 100
        assert torrent.piece_count == 1
        assert torrent.piece_length == PIECE_LENGTH
        assert len(torrent.info_hash) == 20
        assert torrent.is_single_file
        assert torrent.files[0].path == PurePosixPath("demo.bin")
        assert torrent.files[0].offset == 0

    def test_info_hash_matches_independent_calculation(self) -> None:
        info = single_file_info()
        raw = torrent_bytes(info)
        # Locate the encoded info value and hash it independently.
        start = raw.index(b"4:info") + len(b"4:info")
        end = start + len(encode(info))
        assert parse_torrent(raw).info_hash == hashlib.sha1(raw[start:end]).digest()

    def test_parses_optional_top_level_fields(self) -> None:
        raw = torrent_bytes(
            single_file_info(),
            announce=b"http://tracker.example/announce",
            creation_date=1_700_000_000,
            comment=b"a comment",
            created_by=b"mktorrent 1.1",
        )
        torrent = parse_torrent(raw)

        assert torrent.announce == "http://tracker.example/announce"
        assert torrent.creation_date == 1_700_000_000
        assert torrent.comment == "a comment"
        assert torrent.created_by == "mktorrent 1.1"

    def test_accepts_zero_length_single_file(self) -> None:
        # An empty file is legal; the piece count check still applies.
        info = single_file_info(pieces=pieces_for(1), length=1)
        assert parse_torrent(torrent_bytes(info)).total_length == 1


class TestMultiFile:
    def _info(self) -> dict:  # type: ignore[type-arg]
        return {
            b"name": b"bundle",
            b"piece length": PIECE_LENGTH,
            b"pieces": pieces_for(PIECE_LENGTH + 500),
            b"files": [
                {b"length": PIECE_LENGTH, b"path": [b"first.bin"]},
                {b"length": 400, b"path": [b"sub", b"second.bin"]},
                {b"length": 100, b"path": [b"sub", b"deeper", b"third.bin"]},
            ],
        }

    def test_parses_files_with_contiguous_offsets(self) -> None:
        torrent = parse_torrent(torrent_bytes(self._info()))

        assert not torrent.is_single_file
        assert torrent.total_length == PIECE_LENGTH + 500
        assert [entry.offset for entry in torrent.files] == [0, PIECE_LENGTH, PIECE_LENGTH + 400]
        assert [str(entry.path) for entry in torrent.files] == [
            "bundle/first.bin",
            "bundle/sub/second.bin",
            "bundle/sub/deeper/third.bin",
        ]

    def test_piece_count_covers_total_length(self) -> None:
        torrent = parse_torrent(torrent_bytes(self._info()))
        assert torrent.piece_count == 2
        assert torrent.piece_size(0) == PIECE_LENGTH
        assert torrent.piece_size(1) == 500

    def test_rejects_empty_file_list(self) -> None:
        info = self._info()
        info[b"files"] = []
        with pytest.raises(InvalidTorrentError, match="empty"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_non_list_file_entry(self) -> None:
        info = self._info()
        info[b"files"] = [b"not-a-dict"]
        with pytest.raises(InvalidTorrentError, match="must be a dictionary"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_duplicate_paths(self) -> None:
        info = self._info()
        info[b"files"] = [
            {b"length": 1, b"path": [b"same.bin"]},
            {b"length": 1, b"path": [b"same.bin"]},
        ]
        with pytest.raises(InvalidTorrentError, match="duplicate file path"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_missing_path(self) -> None:
        info = self._info()
        info[b"files"] = [{b"length": 10}]
        with pytest.raises(InvalidTorrentError, match="path must be a list"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_negative_file_length(self) -> None:
        info = self._info()
        info[b"files"] = [{b"length": -1, b"path": [b"x.bin"]}]
        with pytest.raises(InvalidTorrentError, match="must not be negative"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_non_list_files(self) -> None:
        info = single_file_info(files=b"nope")
        del info[b"length"]
        with pytest.raises(InvalidTorrentError, match="must be a list"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_non_bytes_path_component(self) -> None:
        info = single_file_info(files=[{b"length": 10, b"path": [123]}])
        del info[b"length"]
        with pytest.raises(InvalidTorrentError, match="must be a byte string"):
            parse_torrent(torrent_bytes(info))

    def test_allows_zero_length_padding_file(self) -> None:
        info = self._info()
        info[b"files"] = [
            {b"length": 10, b"path": [b"real.bin"]},
            {b"length": 0, b"path": [b".pad"]},
        ]
        info[b"pieces"] = pieces_for(10)
        torrent = parse_torrent(torrent_bytes(info))
        assert torrent.total_length == 10
        assert torrent.files[1].length == 0


class TestStructureValidation:
    def test_rejects_non_dictionary_document(self) -> None:
        with pytest.raises(TorrentParseError, match="not a valid torrent file"):
            parse_torrent(b"li1ee")

    def test_rejects_missing_info(self) -> None:
        with pytest.raises(InvalidTorrentError, match="no 'info' dictionary"):
            parse_torrent(encode({b"announce": b"http://t/announce"}))

    def test_rejects_non_dictionary_info(self) -> None:
        with pytest.raises(InvalidTorrentError, match="'info' must be a dictionary"):
            parse_torrent(encode({b"info": b"nope"}))

    def test_rejects_both_length_and_files(self) -> None:
        info = single_file_info(files=[{b"length": 1, b"path": [b"a.bin"]}])
        with pytest.raises(InvalidTorrentError, match="both 'length' and 'files'"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_neither_length_nor_files(self) -> None:
        info = single_file_info()
        del info[b"length"]
        with pytest.raises(InvalidTorrentError, match="neither 'length' nor 'files'"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_missing_name(self) -> None:
        info = single_file_info()
        del info[b"name"]
        with pytest.raises(InvalidTorrentError, match=r"missing required field 'info\.name'"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_non_bytes_name(self) -> None:
        with pytest.raises(InvalidTorrentError, match="must be a byte string"):
            parse_torrent(torrent_bytes(single_file_info(name=42)))

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(InvalidTorrentError, match="empty path component"):
            parse_torrent(torrent_bytes(single_file_info(name=b"")))


class TestPieceValidation:
    def test_rejects_missing_pieces(self) -> None:
        info = single_file_info()
        del info[b"pieces"]
        with pytest.raises(InvalidTorrentError, match="no 'pieces' field"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_empty_pieces(self) -> None:
        with pytest.raises(InvalidTorrentError, match="pieces' is empty"):
            parse_torrent(torrent_bytes(single_file_info(pieces=b"")))

    def test_rejects_pieces_not_multiple_of_20(self) -> None:
        with pytest.raises(InvalidTorrentError, match="multiple of 20"):
            parse_torrent(torrent_bytes(single_file_info(pieces=b"\x00" * 25)))

    def test_rejects_non_bytes_pieces(self) -> None:
        with pytest.raises(InvalidTorrentError, match="must be a byte string"):
            parse_torrent(torrent_bytes(single_file_info(pieces=[1, 2, 3])))

    @pytest.mark.parametrize("piece_length", [0, -16384])
    def test_rejects_non_positive_piece_length(self, piece_length: int) -> None:
        with pytest.raises(InvalidTorrentError, match="must be positive"):
            parse_torrent(torrent_bytes(single_file_info(piece_length=piece_length)))

    def test_rejects_non_integer_piece_length(self) -> None:
        with pytest.raises(InvalidTorrentError, match="must be an integer"):
            parse_torrent(torrent_bytes(single_file_info(piece_length=b"16384")))

    def test_rejects_piece_count_mismatch(self) -> None:
        # 100 bytes at 16 KiB needs 1 piece; declaring 3 is a corrupt torrent.
        info = single_file_info(pieces=b"\x00" * 60)
        with pytest.raises(InvalidTorrentError, match="requires 1"):
            parse_torrent(torrent_bytes(info))

    def test_rejects_zero_total_length(self) -> None:
        info = single_file_info(length=0, pieces=b"\x00" * 20)
        with pytest.raises(InvalidTorrentError, match="total length of 0 bytes"):
            parse_torrent(torrent_bytes(info))


class TestTrackers:
    def test_parses_announce(self) -> None:
        raw = torrent_bytes(single_file_info(), announce=b"http://t.example/announce")
        assert parse_torrent(raw).announce == "http://t.example/announce"

    def test_parses_announce_list_tiers(self) -> None:
        raw = torrent_bytes(
            single_file_info(),
            announce_list=[
                [b"http://a.example/announce", b"http://b.example/announce"],
                [b"udp://c.example:6969/announce"],
            ],
        )
        torrent = parse_torrent(raw)
        assert torrent.announce_list == (
            ("http://a.example/announce", "http://b.example/announce"),
            ("udp://c.example:6969/announce",),
        )

    def test_trackers_property_is_deduplicated_and_ordered(self) -> None:
        raw = torrent_bytes(
            single_file_info(),
            announce=b"http://a.example/announce",
            announce_list=[[b"http://a.example/announce"], [b"udp://c.example:6969/announce"]],
        )
        assert parse_torrent(raw).trackers == (
            "http://a.example/announce",
            "udp://c.example:6969/announce",
        )

    @pytest.mark.parametrize(
        "url", [b"wss://t.example/announce", b"file:///etc/passwd", b"not-a-url"]
    )
    def test_drops_unsupported_schemes(self, url: bytes) -> None:
        raw = torrent_bytes(single_file_info(), announce=url)
        assert parse_torrent(raw).announce is None

    def test_accepts_flat_announce_list(self) -> None:
        """Some encoders emit a flat list of URLs instead of tiers."""
        raw = torrent_bytes(single_file_info(), announce_list=[b"http://a.example/announce"])
        assert parse_torrent(raw).announce_list == (("http://a.example/announce",),)

    def test_ignores_malformed_announce_list_tiers(self) -> None:
        raw = torrent_bytes(single_file_info(), announce_list=[42, [b"http://a.example/announce"]])
        assert parse_torrent(raw).announce_list == (("http://a.example/announce",),)

    def test_torrent_without_trackers_is_still_valid(self) -> None:
        torrent = parse_torrent(torrent_bytes(single_file_info()))
        assert torrent.announce is None
        assert torrent.trackers == ()


class TestPrivateFlag:
    def test_private_true(self) -> None:
        assert parse_torrent(torrent_bytes(single_file_info(private=1))).private is True

    def test_private_false(self) -> None:
        assert parse_torrent(torrent_bytes(single_file_info(private=0))).private is False

    def test_absent_flag_defaults_to_false(self) -> None:
        assert parse_torrent(torrent_bytes(single_file_info())).private is False

    def test_rejects_non_integer_flag(self) -> None:
        with pytest.raises(InvalidTorrentError, match="must be an integer"):
            parse_torrent(torrent_bytes(single_file_info(private=b"1")))


class TestFileIO:
    def test_parses_file_from_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.torrent"
        path.write_bytes(torrent_bytes(single_file_info()))
        assert parse_torrent_file(path).name == "demo.bin"

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.torrent"
        path.write_bytes(torrent_bytes(single_file_info()))
        assert parse_torrent_file(str(path)).name == "demo.bin"

    def test_missing_file_raises_parse_error(self, tmp_path: Path) -> None:
        with pytest.raises(TorrentParseError, match="cannot read torrent file"):
            parse_torrent_file(tmp_path / "does-not-exist.torrent")

    def test_directory_as_torrent_raises_parse_error(self, tmp_path: Path) -> None:
        with pytest.raises(TorrentParseError):
            parse_torrent_file(tmp_path)


class TestGeneratedFixtures:
    """End-to-end checks against deterministically generated torrents."""

    def test_single_file_fixture(self, sample_torrent, payload: bytes) -> None:
        assert sample_torrent.name == "payload.bin"
        assert sample_torrent.total_length == len(payload)
        assert sample_torrent.piece_count == len(payload) // PIECE_LENGTH
        assert sample_torrent.is_single_file

    def test_every_piece_hash_matches_the_payload(self, sample_torrent, payload: bytes) -> None:
        for index in range(sample_torrent.piece_count):
            start = sample_torrent.piece_offset(index)
            end = start + sample_torrent.piece_size(index)
            assert hashlib.sha1(payload[start:end]).digest() == sample_torrent.piece_hash(index)

    def test_multi_file_fixture_offsets_cover_the_stream(self, multi_file_torrent) -> None:
        offset = 0
        for entry in multi_file_torrent.files:
            assert entry.offset == offset
            offset = entry.end_offset
        assert offset == multi_file_torrent.total_length

    def test_multi_file_paths_include_the_root(self, multi_file_torrent) -> None:
        assert all(
            entry.path.parts[0] == multi_file_torrent.name for entry in multi_file_torrent.files
        )


class TestTorrentFromInfo:
    """The magnet-link path: metadata without the original bytes."""

    def test_builds_from_info_dictionary(self) -> None:
        info = single_file_info()
        torrent = torrent_from_info(info, announce="http://t.example/announce")
        assert torrent.info_hash == compute_info_hash(info)
        assert torrent.announce == "http://t.example/announce"

    def test_rejects_non_mapping(self) -> None:
        with pytest.raises(InvalidTorrentError, match="must be a dictionary"):
            torrent_from_info(b"nope")  # type: ignore[arg-type]


class TestUnsafePaths:
    @pytest.mark.parametrize(
        "name",
        [b"../escape", b"/etc/passwd", b"..", b".", b"a/b", b"a\\b", b"a\x00b", b"C:"],
    )
    def test_rejects_unsafe_torrent_name(self, name: bytes) -> None:
        with pytest.raises(UnsafePathError):
            parse_torrent(torrent_bytes(single_file_info(name=name)))


class TestLenientHandling:
    """Malformed *optional* metadata is ignored with a warning, not fatal.

    A torrent with a junk comment or an unusable tracker URL is still perfectly
    usable, so refusing to open it would be user-hostile. Structural problems
    (bad piece geometry, unsafe paths) stay fatal - see the classes above.
    """

    def test_announce_list_that_is_not_a_list_is_ignored(self, caplog) -> None:
        raw = torrent_bytes(single_file_info(), announce_list=b"http://t/announce")
        assert parse_torrent(raw).announce_list == ()
        assert "expected a list of tiers" in caplog.text

    def test_non_bytes_announce_is_ignored(self, caplog) -> None:
        assert parse_torrent(torrent_bytes(single_file_info(), announce=1234)).announce is None
        assert "expected a byte string" in caplog.text

    def test_blank_announce_is_ignored(self) -> None:
        assert parse_torrent(torrent_bytes(single_file_info(), announce=b"   ")).announce is None

    def test_tracker_without_a_host_is_ignored(self, caplog) -> None:
        raw = torrent_bytes(single_file_info(), announce=b"http:///announce")
        assert parse_torrent(raw).announce is None
        assert "no host" in caplog.text

    def test_non_integer_creation_date_is_ignored(self, caplog) -> None:
        raw = torrent_bytes(single_file_info(), creation_date=b"yesterday")
        assert parse_torrent(raw).creation_date is None
        assert "expected an integer" in caplog.text

    def test_non_bytes_comment_is_ignored(self, caplog) -> None:
        raw = torrent_bytes(single_file_info(), comment=42)
        assert parse_torrent(raw).comment is None
        assert "expected a byte string" in caplog.text

    def test_piece_length_not_a_multiple_of_16kib_warns_but_parses(self, caplog) -> None:
        info = single_file_info(piece_length=1000, pieces=pieces_for(1000, 1000))
        torrent = parse_torrent(torrent_bytes(info))
        assert torrent.piece_length == 1000
        assert "not a multiple of 16 KiB" in caplog.text

    def test_extreme_piece_length_warns_but_parses(self, caplog) -> None:
        huge = 128 * 1024 * 1024  # 128 MiB pieces are legal, just unusual
        info = single_file_info(piece_length=huge, length=huge)
        torrent = parse_torrent(torrent_bytes(info))
        assert torrent.piece_length == huge
        assert "outside the typical" in caplog.text

    def test_warns_when_info_keys_are_not_canonical(self, caplog) -> None:
        # Hand-built so the wire bytes really are out of order.
        raw = b"d4:info" + UNSORTED_TORRENT_INFO + b"e"
        parse_torrent(raw)
        assert "non-canonically ordered info keys" in caplog.text
