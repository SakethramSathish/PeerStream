"""Tests for persisted resume state (FR-11, TRD §30).

Resume data is *our own* input, but it is still untrusted: it may have been
truncated by a power cut, edited by hand, or left behind by a torrent that has
since been re-created with different geometry. Every one of those cases has to
end in "start fresh", never in "write data at the wrong offset".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from app.core.constants import RESUME_FILE_SUFFIX, RESUME_VERSION
from app.storage.errors import ResumeError
from app.storage.resume import ResumeState, ResumeStore
from app.torrent import Torrent

INFO_HASH = bytes(range(20))
OTHER_HASH = bytes(range(20, 40))


def make_state(**overrides: object) -> ResumeState:
    """A small, valid resume state (10 pieces of 4 bytes)."""
    fields: dict[str, object] = {
        "info_hash": INFO_HASH,
        "name": "sample",
        "piece_length": 4,
        "piece_count": 10,
        "total_length": 40,
        "completed_pieces": (0, 1, 2),
        "downloaded": 12,
        "uploaded": 3,
        "added_at": 1000.0,
        "saved_at": 1100.0,
        "download_directory": "/tmp/dl",
    }
    fields.update(overrides)
    return ResumeState(**fields)  # type: ignore[arg-type]


class TestStateGeometry:
    def test_derived_counters(self) -> None:
        state = make_state()

        assert state.hex_info_hash == INFO_HASH.hex()
        assert state.completed_count == 3
        assert state.progress == 0.3
        assert state.complete is False
        assert state.downloaded == 12

    def test_completed_bytes_counts_the_short_last_piece(self) -> None:
        # 3 pieces of 4 bytes, plus the last piece of 2 bytes.
        state = make_state(completed_pieces=(0, 1, 9), total_length=38)

        assert state.piece_bytes(0) == 4
        assert state.piece_bytes(9) == 38 - 36
        assert state.completed_bytes() == 10

    def test_with_and_without_pieces(self) -> None:
        state = make_state()

        assert state.with_piece(5).completed_pieces == (0, 1, 2, 5)
        assert state.with_piece(1) is state
        assert state.without_piece(1).completed_pieces == (0, 2)
        assert state.without_piece(7) is state
        assert state.has(2) is True and state.has(3) is False

    def test_with_piece_rejects_an_impossible_index(self) -> None:
        with pytest.raises(IndexError):
            make_state().with_piece(10)

    def test_unsorted_or_duplicate_pieces_are_rejected(self) -> None:
        with pytest.raises(ResumeError, match="sorted and unique"):
            make_state(completed_pieces=(2, 0, 2))

    def test_a_piece_outside_the_torrent_is_rejected(self) -> None:
        with pytest.raises(ResumeError, match="outside"):
            make_state(completed_pieces=(0, 10))

    def test_from_torrent_starts_empty(self, sample_torrent: Torrent, tmp_path: Path) -> None:
        state = ResumeState.from_torrent(sample_torrent, directory=tmp_path)

        assert state.info_hash == sample_torrent.info_hash
        assert state.piece_count == sample_torrent.piece_count
        assert state.completed_pieces == ()
        assert state.download_directory == str(tmp_path)
        assert state.added_at > 0

    def test_matches_rejects_changed_geometry(self, sample_torrent: Torrent) -> None:
        state = ResumeState.from_torrent(sample_torrent)

        assert state.matches(sample_torrent) is True
        assert make_state(info_hash=sample_torrent.info_hash).matches(sample_torrent) is False


class TestSerialisation:
    def test_round_trip(self) -> None:
        state = make_state()

        restored = ResumeState.from_dict(state.to_dict())

        assert restored == state

    def test_json_round_trip(self) -> None:
        state = make_state()

        document = json.loads(json.dumps(state.to_dict()))

        assert ResumeState.from_dict(document) == state

    def test_a_missing_version_defaults_to_the_current_one(self) -> None:
        document = make_state().to_dict()
        del document["version"]

        assert ResumeState.from_dict(document).version == RESUME_VERSION

    def test_a_future_version_is_refused(self) -> None:
        document = make_state().to_dict() | {"version": RESUME_VERSION + 1}

        with pytest.raises(ResumeError, match="newer than"):
            ResumeState.from_dict(document)

    def test_a_non_object_document_is_refused(self) -> None:
        with pytest.raises(ResumeError, match="JSON object"):
            ResumeState.from_dict(["not", "an", "object"])

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("info_hash", "not-hex", "not hex"),
            ("info_hash", "abcd", "must be 20 bytes"),
            ("piece_count", 0, "must be >= 1"),
            ("piece_count", "ten", "must be an integer"),
            ("piece_count", True, "must be an integer"),
            ("piece_length", 0, "must be >= 1"),
            ("total_length", -1, "must be >= 0"),
            ("downloaded", -5, "must be >= 0"),
            ("completed_pieces", {"a": 1}, "must be a list"),
            ("completed_pieces", [0, "1"], "must be integers"),
            ("completed_pieces", [0, 99], "outside"),
            ("name", 12, "must be a string"),
        ],
    )
    def test_bad_fields_are_refused(self, field: str, value: object, expected: str) -> None:
        document = make_state().to_dict() | {field: value}

        with pytest.raises(ResumeError, match=expected):
            ResumeState.from_dict(document)

    def test_missing_required_field_is_refused(self) -> None:
        document = make_state().to_dict()
        del document["piece_count"]

        with pytest.raises(ResumeError, match="missing the 'piece_count' field"):
            ResumeState.from_dict(document)

    def test_unsorted_pieces_on_disk_are_normalised(self) -> None:
        document = make_state().to_dict() | {"completed_pieces": [5, 1, 5, 0]}

        assert ResumeState.from_dict(document).completed_pieces == (0, 1, 5)


class TestStore:
    def test_save_then_load(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path / "state")

        path = store.save(make_state())

        assert path == tmp_path / "state" / f"{INFO_HASH.hex()}{RESUME_FILE_SUFFIX}"
        loaded = store.load(INFO_HASH)
        assert loaded is not None
        assert loaded.completed_pieces == (0, 1, 2)
        assert loaded.saved_at >= time.time() - 5

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert ResumeStore(tmp_path).load(INFO_HASH) is None

    def test_saving_creates_the_state_directory(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path / "deep" / "state")

        store.save(make_state())

        assert store.directory.is_dir()

    def test_no_temporary_files_are_left_behind(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)

        store.save(make_state())

        assert [path.name for path in tmp_path.iterdir()] == [
            f"{INFO_HASH.hex()}{RESUME_FILE_SUFFIX}"
        ]

    def test_save_overwrites_the_previous_state(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)

        store.save(make_state())
        store.save(make_state(completed_pieces=(0, 1, 2, 3, 4), downloaded=20))

        loaded = store.load(INFO_HASH)
        assert loaded is not None
        assert loaded.completed_pieces == (0, 1, 2, 3, 4)
        assert loaded.downloaded == 20

    def test_corrupt_json_raises(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        store.path_for(INFO_HASH).write_text("{not json", encoding="utf-8")

        with pytest.raises(ResumeError, match="not valid JSON"):
            store.load(INFO_HASH)

    def test_corrupt_state_is_quarantined_not_deleted(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        path = store.path_for(INFO_HASH)
        path.write_text("{not json", encoding="utf-8")

        assert store.load_or_none(INFO_HASH) is None
        assert not path.exists()
        quarantined = tmp_path / f"{path.name}.corrupt"
        assert quarantined.exists()
        assert quarantined.read_text() == "{not json"

    def test_quarantine_can_be_declined(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        path = store.path_for(INFO_HASH)
        path.write_text("{not json", encoding="utf-8")

        assert store.load_or_none(INFO_HASH, quarantine=False) is None
        assert path.exists()

    def test_state_describing_another_torrent_is_refused(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        # The file *name* claims one torrent; its contents describe another.
        # Neither is trusted, so the mismatch is an error, not a silent reload.
        store.path_for(INFO_HASH).write_text(
            json.dumps(make_state(info_hash=OTHER_HASH).to_dict()), encoding="utf-8"
        )

        with pytest.raises(ResumeError, match="describes"):
            store.load(INFO_HASH)

    def test_discard_removes_state(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        store.save(make_state())

        assert store.discard(INFO_HASH) is True
        assert store.discard(INFO_HASH) is False
        assert store.load(INFO_HASH) is None

    def test_available_lists_known_torrents(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        store.save(make_state())
        store.save(make_state(info_hash=OTHER_HASH))
        store.path_for(INFO_HASH).with_suffix(".other").write_text("x")

        assert store.available() == tuple(sorted([INFO_HASH.hex(), OTHER_HASH.hex()]))

    def test_available_on_a_missing_directory(self, tmp_path: Path) -> None:
        assert ResumeStore(tmp_path / "nope").available() == ()

    def test_load_all_skips_unreadable_files(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        store.save(make_state())
        store.save(make_state(info_hash=OTHER_HASH))
        store.path_for(OTHER_HASH).write_text("garbage", encoding="utf-8")

        states = store.load_all()

        assert [state.hex_info_hash for state in states] == [INFO_HASH.hex()]
        assert store.available() == (INFO_HASH.hex(),)

    def test_path_for_requires_a_real_info_hash(self, tmp_path: Path) -> None:
        with pytest.raises(ResumeError, match="20 bytes"):
            ResumeStore(tmp_path).path_for(b"short")


class TestFailurePaths:
    """Resume state is our own data, but it is still input that can be wrong."""

    def test_a_non_numeric_timestamp_is_refused(self) -> None:
        with pytest.raises(ResumeError, match="must be a number"):
            ResumeState.from_dict(make_state().to_dict() | {"added_at": "yesterday"})

    def test_a_short_info_hash_is_refused(self) -> None:
        with pytest.raises(ResumeError, match="20 bytes"):
            make_state(info_hash=b"short")

    def test_a_non_positive_piece_length_is_refused(self) -> None:
        with pytest.raises(ResumeError, match="piece length must be positive"):
            make_state(piece_length=0)

    def test_a_non_positive_piece_count_is_refused(self) -> None:
        with pytest.raises(ResumeError, match="piece count must be positive"):
            make_state(piece_count=0)

    def test_a_negative_total_length_is_refused(self) -> None:
        with pytest.raises(ResumeError, match="total length cannot be negative"):
            make_state(total_length=-1)

    def test_negative_counters_are_refused(self) -> None:
        with pytest.raises(ResumeError, match="counters cannot be negative"):
            make_state(uploaded=-1)

    def test_piece_bytes_rejects_an_unknown_piece(self) -> None:
        with pytest.raises(IndexError, match="outside"):
            make_state().piece_bytes(99)

    def test_saving_where_a_file_already_sits_fails(self, tmp_path: Path) -> None:
        blocker = tmp_path / "state"
        blocker.write_text("not a directory")

        with pytest.raises(ResumeError, match="cannot write resume state"):
            ResumeStore(blocker).save(make_state())

    def test_loading_a_directory_fails_loudly(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        store.path_for(INFO_HASH).mkdir()

        with pytest.raises(ResumeError, match="cannot read resume state"):
            store.load(INFO_HASH)

    def test_quarantining_nothing_is_fine(self, tmp_path: Path) -> None:
        assert ResumeStore(tmp_path).quarantine(INFO_HASH) is None

    def test_a_failed_quarantine_is_survivable(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        path = store.path_for(INFO_HASH)
        path.write_text("{not json")
        # os.replace onto an existing directory cannot work.
        path.with_name(f"{path.name}.corrupt").mkdir()

        assert store.load_or_none(INFO_HASH) is None
        assert path.exists()

    def test_deleting_a_directory_fails_loudly(self, tmp_path: Path) -> None:
        store = ResumeStore(tmp_path)
        directory = store.path_for(INFO_HASH)
        directory.mkdir()
        (directory / "inside").write_text("x")

        with pytest.raises(ResumeError, match="cannot delete resume state"):
            store.discard(INFO_HASH)
