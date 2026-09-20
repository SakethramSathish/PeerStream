"""Tests for the standalone seeder.

The interesting test here is the last one: a real seeder process (well, the
coroutine a real seeder process runs) announces itself to a real tracker, a
real client discovers it there, and the bytes that arrive are compared against
the payload the torrent was built from. That is the local swarm the docs
describe, exercised in one test.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from app.services import Session, TorrentState
from app.torrent import Torrent, parse_torrent
from tools.make_test_torrent import build_torrent_bytes
from tools.mock_seeder import build_parser, load_payload, main, run_seeder
from tools.mock_tracker import MockTracker

pytestmark = pytest.mark.integration

PIECE_LENGTH = 32 * 1024


def seeder_args(torrent_path: Path, payload_path: Path, tracker: MockTracker) -> object:
    return build_parser().parse_args(
        [
            "--torrent",
            str(torrent_path),
            "--payload",
            str(payload_path),
            "--port",
            "0",
            "--tracker",
            tracker.announce_url,
            "--announce-interval",
            "3600",
        ]
    )


class TestPayload:
    def test_it_reads_exactly_what_the_torrent_describes(
        self, sample_torrent: Torrent, tmp_path: Path
    ) -> None:
        path = tmp_path / "payload.bin"
        path.write_bytes(b"x" * (sample_torrent.total_length + 64))

        assert len(load_payload(path, sample_torrent)) == sample_torrent.total_length

    def test_a_short_payload_is_refused(self, sample_torrent: Torrent, tmp_path: Path) -> None:
        """A seeder cannot serve what it does not have; saying so is not fatal."""
        path = tmp_path / "short.bin"
        path.write_bytes(b"x" * (sample_torrent.total_length - 1))

        with pytest.raises(ValueError, match="cannot serve what it does not have"):
            load_payload(path, sample_torrent)


class TestArguments:
    def test_defaults_are_sane(self, tmp_path: Path) -> None:
        args = build_parser().parse_args(["--torrent", "a.torrent", "--payload", "p.bin"])

        assert args.port == 0
        assert args.host == "127.0.0.1"
        assert args.tracker is None
        assert args.timeout == 15.0
        assert not args.choke and not args.silent

    def test_a_broken_torrent_is_reported(self, tmp_path: Path, capsys: object) -> None:
        broken = tmp_path / "broken.torrent"
        broken.write_bytes(b"nope")

        assert main(["--torrent", str(broken), "--payload", str(tmp_path / "p.bin")]) == 1


class TestServing:
    async def test_a_seeder_announces_and_serves_a_real_client(
        self, payload: bytes, tmp_path: Path
    ) -> None:
        torrent_path = tmp_path / "served.torrent"
        payload_path = tmp_path / "payload.bin"
        payload_path.write_bytes(payload)

        async with MockTracker(port=0) as tracker:
            # The client finds the seeder the ordinary way: through the
            # tracker named in the metainfo.
            torrent_bytes = build_torrent_bytes(
                payload,
                name="served.bin",
                piece_length=PIECE_LENGTH,
                announce=tracker.announce_url,
            )
            torrent_path.write_bytes(torrent_bytes)
            torrent = parse_torrent(torrent_bytes)

            arguments = seeder_args(torrent_path, payload_path, tracker)
            seeder_task = asyncio.create_task(run_seeder(arguments))  # type: ignore[arg-type]

            # The seeder announces on start-up; that is how we learn its port.
            for _ in range(100):
                if tracker.peers_for(torrent.info_hash):
                    break
                await asyncio.sleep(0.02)
            assert tracker.peers_for(torrent.info_hash), "the seeder never announced"

            try:
                directory = tmp_path / "downloads"
                async with Session(download_directory=directory) as session:
                    engine = await session.add_torrent(torrent, start=True)
                    finished = await engine.wait_until_complete(timeout=60.0)
                    served = engine.peers.stats.discovered
                    # While it runs, a finished torrent is a seeding one.
                    assert engine.state == TorrentState.SEEDING
            finally:
                seeder_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await seeder_task

        assert finished, "the client did not finish"
        assert served >= 1
        assert (directory / "served.bin").read_bytes() == payload
