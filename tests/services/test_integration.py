"""End-to-end: a torrent becomes bytes on disk, twice.

This is the milestone's acceptance test, and it is deliberately made of the
real thing at every layer:

* a torrent built from a real 512 KiB payload, with real SHA-1 piece hashes;
* a mock **HTTP tracker on a real loopback socket**, which the client discovers
  through the torrent's own ``announce`` URL;
* three mock **seeders speaking the real wire protocol** over real TCP;
* the real :class:`~app.services.engine.Engine`, assembled by
  :func:`~app.services.engine.build_engine` and driven by a
  :class:`~app.services.session.Session`;
* the real storage layer, writing to a real temporary directory.

The check at the end is the only one that matters: the bytes on disk hash-match
the payload the torrent was built from. Then the session is torn down and
built again from the same directory, to prove a second run picks up where the
first left off instead of starting over.

Nothing here is mocked except the swarm, and the swarm is not a stub — it is
other people's half of the protocol, implemented honestly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.core.config import Config
from app.core.events import EventType
from app.services import AppState, Session, TorrentState
from app.torrent import Torrent, parse_torrent
from tools.make_test_torrent import build_torrent_bytes
from tools.mock_tracker import MockTracker

from tests.mocks.mock_peer import MockPeer

pytestmark = pytest.mark.integration

SEEDER_COUNT = 3
PIECE_LENGTH = 32 * 1024  # 16 pieces for the shared 512 KiB payload.
PAYLOAD_NAME = "payload.bin"

# Phase one of the resume test dials the swarm down to a crawl so the run can
# be interrupted mid-download on purpose: two requests in flight, each block
# taking a fifth of a second to come back.
STALLED_DELAY = 0.2
STALLED_PIPELINE = 2


async def start_seeders(
    payload: bytes, torrent: Torrent, count: int, *, delay: float = 0.0
) -> list[MockPeer]:
    """Start ``count`` seeders holding the whole payload."""
    seeders: list[MockPeer] = []
    for _ in range(count):
        seeder = MockPeer(
            payload,
            info_hash=torrent.info_hash,
            piece_length=torrent.piece_length,
            request_delay=delay,
        )
        await seeder.start()
        seeders.append(seeder)
    return seeders


async def _wait_for_a_piece(engine: object) -> None:
    """Wait until one piece has been verified by ``engine``."""
    download = engine.download  # type: ignore[attr-defined]
    while download.stats.pieces_verified == 0:
        await asyncio.sleep(0.02)


async def stop_seeders(seeders: list[MockPeer]) -> None:
    for seeder in seeders:
        await seeder.stop()


def torrent_announcing(payload: bytes, announce: str) -> Torrent:
    """A torrent whose tracker is the mock tracker's real URL."""
    return parse_torrent(
        build_torrent_bytes(
            payload, name=PAYLOAD_NAME, piece_length=PIECE_LENGTH, announce=announce
        )
    )


class TestEndToEndDownload:
    """The acceptance path: payload → torrent → tracker → seeders → disk."""

    async def test_a_torrent_downloads_verifies_and_assembles(
        self, payload: bytes, tmp_path: Path
    ) -> None:
        async with MockTracker(port=0) as tracker:
            torrent = torrent_announcing(payload, tracker.announce_url)
            seeders = await start_seeders(payload, torrent, SEEDER_COUNT)
            for seeder in seeders:
                tracker.add_peer(torrent.info_hash, seeder.host, seeder.port, left=0)

            download_directory = tmp_path / "downloads"
            async with Session(Config(), download_directory=download_directory) as session:
                engine = await session.add_torrent(torrent, start=False)
                assert engine.state == TorrentState.IDLE
                await engine.start()
                assert engine.state in {TorrentState.STARTING, TorrentState.DOWNLOADING}
                finished = await engine.wait_until_complete(timeout=60.0)

                assert finished, "the download did not finish inside a minute"
                assert engine.complete
                assert engine.state == TorrentState.SEEDING

                snapshot = engine.snapshot()
                assert snapshot.pieces_verified == torrent.piece_count
                assert snapshot.pieces_missing == 0
                assert snapshot.progress == pytest.approx(1.0)

            await stop_seeders(seeders)

        # The only check that cannot be argued with: the file on disk is the
        # payload, byte for byte, and therefore hashes to the torrent's pieces.
        written = (download_directory / PAYLOAD_NAME).read_bytes()
        assert len(written) == len(payload)
        assert written == payload

    async def test_the_tracker_was_asked_and_the_swarm_was_used(
        self, payload: bytes, tmp_path: Path
    ) -> None:
        async with MockTracker(port=0) as tracker:
            torrent = torrent_announcing(payload, tracker.announce_url)
            seeders = await start_seeders(payload, torrent, SEEDER_COUNT)
            for seeder in seeders:
                tracker.add_peer(torrent.info_hash, seeder.host, seeder.port, left=0)

            async with Session(Config(), download_directory=tmp_path / "d") as session:
                engine = await session.add_torrent(torrent)
                await engine.wait_until_complete(timeout=60.0)
                served = sum(seeder.requests_served for seeder in seeders)

            await stop_seeders(seeders)

        assert tracker.announce_count >= 1, "the client never announced"
        assert tracker.counts(torrent.info_hash)[0] == SEEDER_COUNT
        assert engine.peers.stats.discovered >= SEEDER_COUNT
        # Every piece came off the wire: served blocks cover the whole payload.
        assert served * (16 * 1024) >= len(payload)

    async def test_a_second_run_resumes_instead_of_starting_over(
        self, payload: bytes, tmp_path: Path
    ) -> None:
        """Interrupt a slow download, then finish it in a new session."""
        download_directory = tmp_path / "downloads"

        async with MockTracker(port=0) as tracker:
            torrent = torrent_announcing(payload, tracker.announce_url)
            seeders = await start_seeders(payload, torrent, SEEDER_COUNT, delay=STALLED_DELAY)
            for seeder in seeders:
                tracker.add_peer(torrent.info_hash, seeder.host, seeder.port, left=0)

            # --- run one: deliberately torn down before it can finish --------
            stalled = Config().with_overrides(
                download={"max_outstanding_requests": STALLED_PIPELINE}
            )
            async with Session(stalled, download_directory=download_directory) as session:
                engine = await session.add_torrent(torrent)
                # Stop the moment the swarm has delivered anything at all: a
                # torn-down session is what resume has to survive, and tying it
                # to a wall-clock guess is how tests become flaky.
                await asyncio.wait_for(_wait_for_a_piece(engine), timeout=30.0)
                await session.stop_all()
                partial = engine.download.stats.pieces_verified
                assert 0 < partial < torrent.piece_count, (
                    "the first run should be interrupted mid-download"
                )

            # --- run two: a brand-new session, same disk, open throttle ------
            for seeder in seeders:
                seeder.request_delay = 0.0
            async with Session(Config(), download_directory=download_directory) as session:
                resumed_engine = await session.add_torrent(torrent)
                adopted = resumed_engine.resumed.pieces
                finished = await resumed_engine.wait_until_complete(timeout=60.0)
                seeded_from = resumed_engine.snapshot().download.total
                assert finished
                assert resumed_engine.complete

            await stop_seeders(seeders)

        assert adopted >= partial, "the second run forgot what the first one saved"
        assert adopted == partial
        # Resuming means *not* re-fetching what is already on disk.
        assert seeded_from < len(payload)
        assert (download_directory / PAYLOAD_NAME).read_bytes() == payload

    async def test_a_finished_torrent_seeds_what_it_has(
        self, payload: bytes, tmp_path: Path
    ) -> None:
        async with MockTracker(port=0) as tracker:
            torrent = torrent_announcing(payload, tracker.announce_url)
            seeders = await start_seeders(payload, torrent, 1)
            for seeder in seeders:
                tracker.add_peer(torrent.info_hash, seeder.host, seeder.port, left=0)

            async with Session(Config(), download_directory=tmp_path / "d") as session:
                engine = await session.add_torrent(torrent)
                await engine.wait_until_complete(timeout=60.0)
                claimed = engine.upload.have.count
                # Seeding is a state of a *running* engine; ask while it runs.
                assert engine.state == TorrentState.SEEDING

            await stop_seeders(seeders)

        assert claimed == torrent.piece_count

    async def test_app_state_watches_the_whole_run(self, payload: bytes, tmp_path: Path) -> None:
        """AppState reduces real events and reports real measurements."""
        async with MockTracker(port=0) as tracker:
            torrent = torrent_announcing(payload, tracker.announce_url)
            seeders = await start_seeders(payload, torrent, SEEDER_COUNT)
            for seeder in seeders:
                tracker.add_peer(torrent.info_hash, seeder.host, seeder.port, left=0)

            state = AppState()
            async with Session(Config(), download_directory=tmp_path / "d") as session:
                state.attach(session)
                engine = await session.add_torrent(torrent)
                await engine.wait_until_complete(timeout=60.0)
                snapshot = state.snapshot()
                rendered = state.render()

            await stop_seeders(seeders)

        assert snapshot.totals.torrents == 1
        assert snapshot.totals.active == 1
        assert snapshot.torrents[0].state == TorrentState.SEEDING
        assert state.count(EventType.TORRENT_ADDED) == 1
        assert state.count(EventType.PIECE_VERIFIED) == torrent.piece_count
        # The timeline is real: pieces were verified, peers connected, blocks
        # arrived, and every one of them is in the ring.
        assert snapshot.events, "no events were reduced"
        assert len(snapshot.events) > torrent.piece_count
        assert "torrents: 1 (1 active)" in rendered
