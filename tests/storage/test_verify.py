"""Tests for SHA-1 piece verification (FR-09).

The interesting properties here are not "sha1 works" — that is hashlib's job —
but that verification happens off the event loop, with a bounded number of
threads, and that a mismatch is reported as data rather than raised as noise.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.storage.verify import PieceVerifier, hash_matches, hash_piece

# RFC 3174 / every SHA-1 implementation agrees on these.
EMPTY_SHA1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
ABC_SHA1 = "a9993e364706816aba3e25717850c26c9cd0d89d"


class TestHashing:
    def test_known_answer_vectors(self) -> None:
        assert hash_piece(b"").hex() == EMPTY_SHA1
        assert hash_piece(b"abc").hex() == ABC_SHA1

    def test_matches_hashlib_on_a_realistic_piece(self) -> None:
        data = bytes(range(256)) * 64  # 16 KiB, the block size

        assert hash_piece(data) == hashlib.sha1(data).digest()

    def test_hash_matches_accepts_good_data(self) -> None:
        data = b"a piece"

        assert hash_matches(data, hash_piece(data)) is True

    def test_a_single_changed_byte_is_caught(self) -> None:
        good = b"a piece of data"
        expected = hash_piece(good)
        bad = good[:-1] + bytes([good[-1] ^ 0x01])

        assert hash_matches(bad, expected) is False

    def test_a_truncated_piece_does_not_match(self) -> None:
        data = b"a piece of data"

        assert hash_matches(data[:-1], hash_piece(data)) is False

    def test_an_empty_piece_only_matches_the_empty_hash(self) -> None:
        assert hash_matches(b"", bytes.fromhex(EMPTY_SHA1)) is True
        assert hash_matches(b"", bytes.fromhex(ABC_SHA1)) is False


class TestPieceVerifier:
    async def test_good_and_bad_pieces(self) -> None:
        verifier = PieceVerifier(workers=2)
        data = b"x" * 1024
        expected = hash_piece(data)

        assert await verifier.verify(data, expected) is True
        assert await verifier.verify(b"y" * 1024, expected) is False

        await verifier.aclose()

    async def test_workers_must_be_at_least_one(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            PieceVerifier(workers=0)

    async def test_hashing_runs_in_a_worker_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The event loop must not be the thing computing SHA-1."""
        import app.storage.verify as module

        seen: list[str] = []
        real = module.hash_matches

        def spy(data: object, expected: object) -> bool:
            seen.append(threading.current_thread().name)
            return real(data, expected)  # type: ignore[arg-type]

        monkeypatch.setattr(module, "hash_matches", spy)
        verifier = PieceVerifier(workers=1)

        await verifier.verify(b"piece", b"\x00" * 20)

        assert seen and seen[0] != threading.current_thread().name
        assert "sha1" in seen[0] or "ThreadPool" in seen[0]
        await verifier.aclose()

    async def test_the_loop_keeps_running_while_a_piece_is_hashed(self) -> None:
        verifier = PieceVerifier(workers=1)
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        task = asyncio.create_task(tick())
        try:
            await verifier.verify(b"z" * (4 << 20), b"\x00" * 20)
        finally:
            task.cancel()
        await verifier.aclose()

        assert ticks > 0, "the loop stalled while hashing"

    async def test_verify_many_keeps_the_input_order(self) -> None:
        verifier = PieceVerifier(workers=4)
        good = hash_piece(b"good")
        items = [(0, b"good", good), (1, b"bad", good), (2, b"good", good)]

        results = await verifier.verify_many(items)

        assert results == [(0, True), (1, False), (2, True)]
        await verifier.aclose()

    async def test_verify_many_is_bounded_by_the_worker_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.storage.verify as module

        lock = threading.Lock()
        in_flight = 0
        peak = 0
        real = module.hash_matches

        def spy(data: object, expected: object) -> bool:
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                return real(data, expected)  # type: ignore[arg-type]
            finally:
                with lock:
                    in_flight -= 1

        monkeypatch.setattr(module, "hash_matches", spy)
        verifier = PieceVerifier(workers=2)
        items = [(index, b"data", b"\x00" * 20) for index in range(12)]

        await verifier.verify_many(items)

        assert peak <= 2, f"{peak} hashes ran at once with 2 workers"
        await verifier.aclose()

    async def test_an_empty_batch_costs_nothing(self) -> None:
        verifier = PieceVerifier()

        assert await verifier.verify_many([]) == []

        await verifier.aclose()

    async def test_an_injected_executor_is_not_shut_down(self) -> None:
        executor = ThreadPoolExecutor(max_workers=1)
        verifier = PieceVerifier(executor=executor)

        await verifier.verify(b"data", b"\x00" * 20)
        await verifier.aclose()

        assert executor.submit(lambda: 42).result(timeout=5) == 42
        executor.shutdown(wait=True)

    async def test_aclose_is_idempotent(self) -> None:
        verifier = PieceVerifier(workers=1)
        await verifier.verify(b"data", b"\x00" * 20)

        await verifier.aclose()
        await verifier.aclose()

    async def test_verifying_after_close_still_works(self) -> None:
        verifier = PieceVerifier(workers=1)
        data = b"data"
        expected = hash_piece(data)
        await verifier.aclose()

        assert await verifier.verify(data, expected) is True

        await verifier.aclose()
