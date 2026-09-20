"""Tests for the storage manager: the interface the rest of the client uses.

These tests write real files to real (temporary) directories. The payload is
the same deterministic buffer the torrent's piece hashes were computed from, so
"valid data" here means actual valid data — not a mock standing in for it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from app.core.config import StorageConfig
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.storage import StorageManager
from app.storage.errors import PieceHashMismatch, StorageError
from app.storage.verify import PieceVerifier
from app.torrent import Torrent
from app.torrent.metadata import FileEntry

PieceData = Callable[[Torrent, int], bytes]


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def events(bus: EventBus) -> list[Event]:
    """Every event published, in order."""
    captured: list[Event] = []
    bus.subscribe_all(captured.append)
    return captured


def flip_first_byte(path: Path) -> None:
    """Corrupt one byte in place, leaving the file the same size.

    Truncating would be a different bug: a short file makes every piece after
    the cut invalid, which is not what these tests are about.
    """
    with path.open("r+b") as handle:
        original = handle.read(1)
        handle.seek(0)
        handle.write(bytes([original[0] ^ 0xFF]))


def storage_for(torrent: Torrent, directory: Path, **kwargs: object) -> StorageManager:
    config = kwargs.pop("config", None) or StorageConfig(
        preallocate_files=True, state_directory=directory.parent / "state"
    )
    return StorageManager(torrent, directory, config=config, **kwargs)  # type: ignore[arg-type]


class TestPreparation:
    async def test_prepare_creates_and_sizes_every_file(
        self, multi_file_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(multi_file_torrent, tmp_path / "dl")

        result = await storage.prepare()

        assert result.file_count == 4
        assert result.bytes_allocated == multi_file_torrent.total_length
        assert await storage.on_disk_sizes() == tuple(
            entry.length for entry in multi_file_torrent.files
        )
        await storage.aclose()

    async def test_preallocation_can_be_disabled(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")

        result = await storage.prepare(preallocate=False)

        assert result.preallocated is False
        assert await storage.on_disk_sizes() == (0,)
        await storage.aclose()

    async def test_preparing_twice_allocates_nothing_new(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")

        await storage.prepare()
        again = await storage.prepare()

        assert again.bytes_allocated == 0
        await storage.aclose()

    async def test_the_download_directory_comes_from_the_config(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        config = StorageConfig(download_directory=tmp_path / "from-config")

        storage = StorageManager(sample_torrent, config=config)

        assert storage.root == (tmp_path / "from-config").resolve()
        await storage.aclose()

    async def test_preparing_reports_on_the_event_bus(
        self, sample_torrent: Torrent, tmp_path: Path, bus: EventBus, events: list[Event]
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl", event_bus=bus)

        await storage.prepare()

        assert events[0].type is EventType.DISK_ALLOCATED
        assert events[0].data["file_count"] == 1
        assert events[0].torrent_id == sample_torrent.hex_info_hash
        await storage.aclose()


class TestWritingAndReading:
    async def test_a_piece_survives_the_round_trip(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        data = piece_of(sample_torrent, 5)

        written = await storage.write_piece(5, data)

        assert written == len(data)
        assert await storage.read_piece(5) == data
        assert storage.completed_pieces == (5,)
        await storage.aclose()

    async def test_every_piece_of_a_multi_file_torrent_lands_in_the_right_place(
        self, crossing_torrent: Torrent, tmp_path: Path, piece_of: PieceData, payload: bytes
    ) -> None:
        storage = storage_for(crossing_torrent, tmp_path / "dl")
        await storage.prepare()

        for index in range(crossing_torrent.piece_count):
            await storage.write_piece(index, piece_of(crossing_torrent, index))
        await storage.save_resume()

        assert storage.complete
        assert storage.progress == 1.0
        # Each file holds exactly its slice of the payload — the reassembly the
        # torrent promised, boundaries or no boundaries.
        for entry in crossing_torrent.files:
            on_disk = (storage.root / entry.path).read_bytes()
            assert on_disk == payload[entry.offset : entry.end_offset]
        await storage.aclose()

    async def test_a_piece_crossing_two_files_is_read_back_whole(
        self, crossing_torrent: Torrent, tmp_path: Path, piece_of: PieceData, payload: bytes
    ) -> None:
        storage = storage_for(crossing_torrent, tmp_path / "dl")
        await storage.prepare()
        data = piece_of(crossing_torrent, 1)  # 100 KiB..200 KiB: two files

        await storage.write_piece(1, data)

        assert await storage.read_piece(1) == data
        assert data == payload[100 * 1024 : 200 * 1024]
        await storage.aclose()

    async def test_the_last_piece_may_be_short(
        self, crossing_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(crossing_torrent, tmp_path / "dl")
        await storage.prepare()
        last = crossing_torrent.piece_count - 1
        data = piece_of(crossing_torrent, last)

        assert len(data) < crossing_torrent.piece_length
        await storage.write_piece(last, data)

        assert await storage.read_piece(last) == data
        await storage.aclose()

    async def test_blocks_are_read_out_of_a_piece(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        data = piece_of(sample_torrent, 2)
        await storage.write_piece(2, data)

        assert await storage.read_block(2, 0, 10) == data[:10]
        assert await storage.read_block(2, 100, 50) == data[100:150]
        await storage.aclose()

    async def test_reading_a_piece_that_is_not_there_fails(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare(preallocate=False)

        with pytest.raises(StorageError, match="incomplete on disk"):
            await storage.read_piece(0)

        with pytest.raises(StorageError, match="not on disk"):
            await storage.read_block(0, 0, 16)
        await storage.aclose()

    async def test_a_block_outside_its_piece_is_refused(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        await storage.write_piece(0, piece_of(sample_torrent, 0))

        with pytest.raises(ValueError, match="outside piece"):
            await storage.read_block(0, 1000, 16_000)
        await storage.aclose()

    async def test_writes_do_not_touch_neighbouring_pieces(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        await storage.write_piece(3, piece_of(sample_torrent, 3))

        around = await storage.read_piece(2) + await storage.read_piece(4)

        assert around == bytes(len(around))
        await storage.aclose()

    async def test_a_write_reports_on_the_event_bus(
        self,
        sample_torrent: Torrent,
        tmp_path: Path,
        piece_of: PieceData,
        bus: EventBus,
        events: list[Event],
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl", event_bus=bus)
        await storage.prepare()

        await storage.write_piece(1, piece_of(sample_torrent, 1))

        write_events = [event for event in events if event.type is EventType.DISK_WRITE]
        assert len(write_events) == 1
        assert write_events[0].data["index"] == 1
        assert "piece 1 written" in write_events[0].message
        await storage.aclose()


class TestVerification:
    async def test_a_corrupt_piece_is_rejected_before_it_reaches_disk(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        good = piece_of(sample_torrent, 4)
        corrupt = good[:100] + bytes([good[100] ^ 0xFF]) + good[101:]

        with pytest.raises(PieceHashMismatch) as caught:
            await storage.write_piece(4, corrupt)

        assert caught.value.index == 4
        assert caught.value.actual != caught.value.expected
        # Nothing was written, and the piece is not counted as done.
        assert await storage.read_piece(4) == bytes(len(good))
        assert storage.completed_pieces == ()
        await storage.aclose()

    async def test_verification_can_be_turned_off(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        config = StorageConfig(verify_before_write=False)
        storage = storage_for(sample_torrent, tmp_path / "dl", config=config)
        await storage.prepare()

        await storage.write_piece(0, b"\x00" * sample_torrent.piece_size(0))

        assert storage.completed_pieces == (0,)
        await storage.aclose()

    async def test_verification_can_be_overridden_per_write(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()

        with pytest.raises(PieceHashMismatch):
            await storage.write_piece(0, b"\x00" * sample_torrent.piece_size(0))
        await storage.write_piece(0, b"\x00" * sample_torrent.piece_size(0), verify=False)

        assert storage.completed_pieces == (0,)
        await storage.aclose()

    async def test_a_rejected_piece_is_reported_as_a_failure_event(
        self, sample_torrent: Torrent, tmp_path: Path, bus: EventBus, events: list[Event]
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl", event_bus=bus)
        await storage.prepare()

        with pytest.raises(PieceHashMismatch):
            await storage.write_piece(0, b"\x00" * sample_torrent.piece_size(0))

        failures = [event for event in events if event.type is EventType.PIECE_FAILED]
        assert len(failures) == 1
        assert failures[0].data["index"] == 0
        assert failures[0].level >= 30  # WARNING
        await storage.aclose()

    async def test_wrong_sized_data_is_refused(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()

        with pytest.raises(ValueError, match="must be 16384 bytes"):
            await storage.write_piece(0, piece_of(sample_torrent, 0)[:-1])
        await storage.aclose()

    async def test_an_impossible_piece_index_is_refused(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")

        with pytest.raises(IndexError):
            await storage.write_piece(sample_torrent.piece_count, b"x" * 16)
        await storage.aclose()

    async def test_a_piece_edited_outside_the_client_fails_verification(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        await storage.write_piece(2, piece_of(sample_torrent, 2))
        (storage.root / "payload.bin").write_bytes(b"\x00" * 8)

        assert await storage.verify_piece(2) is False
        await storage.aclose()

    async def test_a_good_piece_passes_verification(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        await storage.write_piece(2, piece_of(sample_torrent, 2))

        assert await storage.verify_piece(2) is True
        await storage.aclose()

    async def test_an_unwritten_piece_is_not_valid(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()

        assert await storage.verify_piece(0) is False
        await storage.aclose()


class TestResume:
    async def test_progress_is_remembered_across_a_restart(
        self, crossing_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        directory = tmp_path / "dl"
        state_directory = tmp_path / "state"
        first = storage_for(
            crossing_torrent,
            directory,
            config=StorageConfig(state_directory=state_directory),
        )
        await first.prepare()
        for index in (0, 1, 4):
            await first.write_piece(index, piece_of(crossing_torrent, index))
        await first.save_resume(uploaded=4096)
        await first.aclose()

        second = storage_for(
            crossing_torrent,
            directory,
            config=StorageConfig(state_directory=state_directory),
        )
        state = await second.load_resume()

        assert state is not None
        assert second.completed_pieces == (0, 1, 4)
        assert second.downloaded_bytes == first.downloaded_bytes
        assert state.uploaded == 4096
        assert set(second.missing_pieces) == {2, 3, 5}
        await second.aclose()

    async def test_resume_verification_drops_pieces_that_no_longer_match(
        self, crossing_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        directory = tmp_path / "dl"
        state_directory = tmp_path / "state"
        writer = storage_for(
            crossing_torrent,
            directory,
            config=StorageConfig(state_directory=state_directory),
        )
        await writer.prepare()
        for index in (0, 1, 2):
            await writer.write_piece(index, piece_of(crossing_torrent, index))
        await writer.save_resume()
        await writer.aclose()
        # Someone edits the data behind our back (or the disk lies). Only piece
        # 0 is touched, so pieces 1 and 2 must survive the re-check.
        flip_first_byte(directory / "bundle" / "part000.bin")

        resumer = storage_for(
            crossing_torrent,
            directory,
            config=StorageConfig(state_directory=state_directory),
        )
        await resumer.load_resume(verify=True)

        assert 0 not in resumer.completed_pieces
        assert resumer.completed_pieces == (1, 2)
        await resumer.aclose()

    async def test_verify_completed_reports_what_it_found(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        await storage.write_piece(0, piece_of(sample_torrent, 0))
        await storage.write_piece(1, piece_of(sample_torrent, 1))
        flip_first_byte(storage.root / "payload.bin")

        report = await storage.verify_completed()

        assert report.checked == 2
        assert report.valid == (1,)
        assert report.invalid == (0,)
        assert report.all_valid is False
        assert report.elapsed >= 0
        assert storage.completed_pieces == (1,)
        await storage.aclose()

    async def test_verifying_nothing_costs_nothing(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")

        report = await storage.verify_completed()

        assert report.checked == 0
        assert report.all_valid is True
        await storage.aclose()

    async def test_specific_pieces_can_be_checked(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()
        await storage.write_piece(6, piece_of(sample_torrent, 6))

        report = await storage.verify_completed([6, 7])

        assert report.checked == 2
        assert report.valid == (6,)
        assert report.invalid == (7,)
        await storage.aclose()

    async def test_loading_state_for_a_fresh_torrent_returns_none(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")

        assert await storage.load_resume() is None
        await storage.aclose()

    async def test_resume_events_are_published(
        self,
        sample_torrent: Torrent,
        tmp_path: Path,
        piece_of: PieceData,
        bus: EventBus,
        events: list[Event],
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl", event_bus=bus)
        await storage.prepare()
        await storage.write_piece(0, piece_of(sample_torrent, 0))

        await storage.save_resume()
        events.clear()
        await storage.load_resume()

        assert [event.type for event in events] == [EventType.RESUME_LOADED]
        await storage.aclose()

    async def test_pieces_that_are_not_on_disk_are_not_reported_as_resumed(
        self,
        sample_torrent: Torrent,
        tmp_path: Path,
        piece_of: PieceData,
        bus: EventBus,
        events: list[Event],
    ) -> None:
        """The claim in the file is not the fact on the disk.

        A resume file outlives the download directory it describes, and because
        the fixtures are deterministic the same info hash comes back run after
        run. ``load_resume(verify=True)`` used to re-verify the pieces, drop the
        failures from the completed set, and then return the state object it had
        loaded *before* verifying — so the engine reported "resumed 512 piece(s)
        from disk" while downloading all 512 from scratch.
        """
        writer = storage_for(sample_torrent, tmp_path / "dl")
        await writer.prepare()
        await writer.write_piece(0, piece_of(sample_torrent, 0))
        await writer.save_resume()
        await writer.aclose()

        # storage_for puts the state directory beside the download directory, so
        # both of these share tmp_path/state: the record survives, the bytes do
        # not. That is a deleted download folder, not a different torrent.
        reader = storage_for(sample_torrent, tmp_path / "elsewhere", event_bus=bus)
        await reader.prepare()

        state = await reader.load_resume(verify=True)

        assert state is not None, "the state file is still there and still matches"
        assert state.completed_pieces == ()
        assert reader.completed_pieces == ()
        published = [
            event.data["pieces"]
            for event in events
            if event.type is EventType.RESUME_LOADED
        ]
        assert published == [0], "the event carries what was verified, not what was claimed"
        await reader.aclose()

    async def test_a_resume_that_still_checks_out_is_reported_whole(
        self,
        sample_torrent: Torrent,
        tmp_path: Path,
        piece_of: PieceData,
    ) -> None:
        """The other half: verification must not throw away pieces that are there."""
        directory = tmp_path / "dl"
        writer = storage_for(sample_torrent, directory)
        await writer.prepare()
        await writer.write_piece(0, piece_of(sample_torrent, 0))
        await writer.save_resume()
        await writer.aclose()

        reader = storage_for(sample_torrent, directory)
        await reader.prepare()

        state = await reader.load_resume(verify=True)

        assert state is not None
        assert state.completed_pieces == (0,)
        assert reader.completed_pieces == (0,)
        await reader.aclose()

    async def test_state_from_a_different_geometry_is_discarded(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        directory = tmp_path / "dl"
        state_directory = tmp_path / "state"
        writer = storage_for(
            sample_torrent, directory, config=StorageConfig(state_directory=state_directory)
        )
        await writer.prepare()
        await writer.write_piece(0, piece_of(sample_torrent, 0))
        await writer.save_resume()
        await writer.aclose()

        document = (state_directory / f"{sample_torrent.hex_info_hash}.resume.json").read_text()
        edited = document.replace(
            f'"piece_length": {sample_torrent.piece_length}', '"piece_length": 32768'
        )
        (state_directory / f"{sample_torrent.hex_info_hash}.resume.json").write_text(edited)

        resumer = storage_for(
            sample_torrent, directory, config=StorageConfig(state_directory=state_directory)
        )
        assert await resumer.load_resume() is None
        assert resumer.completed_pieces == ()
        await resumer.aclose()


class TestLifecycle:
    async def test_closing_releases_the_owned_verifier(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")

        await storage.aclose()

        assert storage._verifier._executor is None

    async def test_an_injected_verifier_is_left_alone(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        verifier = PieceVerifier(workers=1)
        storage = storage_for(sample_torrent, tmp_path / "dl", verifier=verifier)

        await storage.aclose()

        assert await verifier.verify(b"data", b"\x00" * 20) is False
        await verifier.aclose()

    async def test_counters_describe_the_download(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()

        assert storage.progress == 0.0
        assert storage.complete is False
        assert len(storage.missing_pieces) == sample_torrent.piece_count

        for index in range(sample_torrent.piece_count):
            await storage.write_piece(index, piece_of(sample_torrent, index))

        assert storage.downloaded_bytes == sample_torrent.total_length
        assert storage.progress == 1.0
        assert storage.complete is True
        assert storage.missing_pieces == ()
        await storage.aclose()

    async def test_no_event_bus_is_fine(
        self, sample_torrent: Torrent, tmp_path: Path, piece_of: PieceData
    ) -> None:
        storage = storage_for(sample_torrent, tmp_path / "dl")
        await storage.prepare()

        await storage.write_piece(0, piece_of(sample_torrent, 0))

        assert storage.completed_pieces == (0,)
        await storage.aclose()

    async def test_files_and_geometry_are_exposed(
        self, multi_file_torrent: Torrent, tmp_path: Path
    ) -> None:
        storage = storage_for(multi_file_torrent, tmp_path / "dl")

        assert len(storage.files) == 4
        assert storage.piece_count == multi_file_torrent.piece_count
        assert storage.piece_length == multi_file_torrent.piece_length
        assert storage.total_length == multi_file_torrent.total_length
        assert storage.torrent is multi_file_torrent
        assert storage.resume_store.directory == tmp_path / "state"
        await storage.aclose()


class TestEdgeCases:
    def test_config_and_layout_are_exposed(self, sample_torrent: Torrent, tmp_path: Path) -> None:
        config = StorageConfig(preallocate_files=False)
        storage = StorageManager(sample_torrent, tmp_path / "dl", config=config)

        assert storage.config is config
        assert storage.layout.total_length == sample_torrent.total_length
        assert storage.layout.root == (tmp_path / "dl").resolve()
        asyncio_run = storage.aclose
        assert callable(asyncio_run)

    async def test_an_empty_torrent_is_complete_but_has_no_bytes(self, tmp_path: Path) -> None:
        # A torrent of nothing (all files zero-length): progress must not divide
        # by zero, and "complete" must not lie.
        empty = Torrent(
            name="empty",
            info_hash=bytes(range(20)),
            piece_length=16,
            piece_hashes=(),
            files=(FileEntry(PurePosixPath("empty.bin"), 0, 0),),
        )
        storage = StorageManager(empty, tmp_path / "dl")

        await storage.prepare()

        assert storage.progress == 1.0
        assert storage.complete is True
        assert storage.downloaded_bytes == 0
        assert await storage.on_disk_sizes() == (0,)
        await storage.aclose()


class TestDeleteFiles:
    """Removing a torrent's data: thorough about ours, blind to everything else."""

    async def test_it_deletes_the_files_and_prunes_empty_directories(
        self, multi_file_torrent: Torrent, payload: bytes, tmp_path: Path
    ) -> None:
        manager = StorageManager(multi_file_torrent, tmp_path, config=StorageConfig())
        await manager.prepare()
        for index in range(multi_file_torrent.piece_count):
            start = index * multi_file_torrent.piece_length
            await manager.write_piece(
                index, payload[start : start + multi_file_torrent.piece_length]
            )

        created = [tmp_path / mapped.relative_path for mapped in manager.files]
        assert all(path.exists() for path in created)

        removed = await manager.delete_files()

        assert removed == len(manager.files)
        assert not any(path.exists() for path in created)
        # The torrent's own directory is pruned; the download root is not.
        assert not (tmp_path / multi_file_torrent.name).exists()
        assert tmp_path.exists()
        await manager.aclose()

    async def test_it_refuses_to_touch_anything_outside_the_root(
        self, sample_torrent: Torrent, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A layout that points outside the download directory is not ours to delete."""
        manager = StorageManager(sample_torrent, tmp_path, config=StorageConfig())
        await manager.prepare()

        outside = tmp_path.parent / "not-ours.bin"
        outside.write_bytes(b"precious")
        manager._layout = SimpleNamespace(  # type: ignore[assignment]
            root=manager.root,
            files=(SimpleNamespace(path=outside),),
            total_length=len(b"precious"),
        )

        with caplog.at_level(logging.WARNING):
            removed = await manager.delete_files()

        assert removed == 0
        assert outside.exists(), "a file outside the root must survive"
        assert "refusing to delete" in caplog.text
        await manager.aclose()

    async def test_deleting_twice_counts_only_what_was_there(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        """Deletion counts results, not attempts: a missing file is not a deletion."""
        manager = StorageManager(sample_torrent, tmp_path, config=StorageConfig())
        await manager.prepare()

        assert await manager.delete_files() == len(manager.files)
        assert await manager.delete_files() == 0
        await manager.aclose()
