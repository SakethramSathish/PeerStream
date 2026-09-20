"""Tests for the peer probe tool (offline behaviour)."""

from __future__ import annotations

import pytest
from app.core.config import NetworkConfig
from app.peer.connection import SwarmContext
from app.peer.handshake import Handshake
from tools.peer_probe import PeerReport, ProbeSummary, probe_peer, probe_peers, probe_swarm, render

from tests.mocks.mock_peer import MockPeer

INFO_HASH = bytes(range(20))
PIECE_LENGTH = 16 * 1024


def make_context(piece_count: int = 2) -> SwarmContext:
    return SwarmContext(info_hash=INFO_HASH, piece_count=piece_count, piece_length=PIECE_LENGTH)


class TestUnreachablePeers:
    async def test_a_refused_connection_is_reported_not_raised(self) -> None:
        report = await probe_peer("127.0.0.1", 1, INFO_HASH, connect_timeout=1.0)
        assert not report.connected
        assert report.outcome in {"refused", "timeout"}
        assert report.error

    async def test_peers_are_probed_concurrently(self) -> None:
        summary = await probe_peers(
            [("127.0.0.1", 1), ("127.0.0.1", 2)], INFO_HASH, connect_timeout=1.0
        )
        assert len(summary.reports) == 2
        assert summary.connected == []

    async def test_an_empty_peer_list_is_fine(self) -> None:
        assert (await probe_peers([], INFO_HASH)).reports == []


class TestSummary:
    def test_aggregates_outcomes(self) -> None:
        summary = ProbeSummary(
            reports=[
                PeerReport("a:1", "connected", client="qBittorrent 5.1.0", unchoked=True),
                PeerReport("b:1", "connected", client="qBittorrent 5.1.0"),
                PeerReport("c:1", "connected", client="Transmission 4.0"),
                PeerReport("d:1", "timeout", error="no data"),
            ]
        )

        assert len(summary.connected) == 3
        assert len(summary.unchoked) == 1
        assert summary.clients == {"qBittorrent 5.1.0": 2, "Transmission 4.0": 1}

    def test_empty_summary(self) -> None:
        summary = ProbeSummary()
        assert summary.connected == []
        assert summary.clients == {}


class TestRendering:
    def test_prints_successes_and_failures(self, capsys: pytest.CaptureFixture[str]) -> None:
        summary = ProbeSummary(
            reports=[
                PeerReport(
                    "10.0.0.5:6881",
                    "connected",
                    client="qBittorrent 5.1.0",
                    handshake_ms=42.5,
                    messages=("Bitfield(378 bytes)", "Unchoke"),
                    pieces_held=3020,
                ),
                PeerReport("10.0.0.6:6882", "timeout", error="no data for 4.0s"),
            ]
        )
        render(summary, title="Probe")

        output = capsys.readouterr().out
        assert "Probe" in output
        assert "10.0.0.5:6881" in output
        assert "qBittorrent 5.1.0" in output
        assert "3020 pieces" in output
        assert "42 ms" in output  # 42.5 rounds to 42
        assert "Unchoke" in output
        assert "10.0.0.6:6882" in output
        assert "timeout" in output
        assert "1/2 completed a handshake" in output

    def test_shows_whether_the_peer_unchoked_us(self, capsys: pytest.CaptureFixture[str]) -> None:
        summary = ProbeSummary(
            reports=[
                PeerReport("10.0.0.5:6881", "connected", unchoked=True),
                PeerReport("10.0.0.6:6882", "connected", unchoked=False),
                PeerReport("10.0.0.7:6883", "connected"),
            ]
        )
        render(summary, title="Swarm")

        lines = capsys.readouterr().out.splitlines()
        body = [line for line in lines if ":" in line]
        assert "unchoked" in body[0]
        assert "choked" in body[1]
        assert not body[2].rstrip().endswith("choked")

    def test_reports_a_silent_peer(self, capsys: pytest.CaptureFixture[str]) -> None:
        summary = ProbeSummary(reports=[PeerReport("10.0.0.5:6881", "connected", client="Unknown")])
        render(summary, title="Probe")
        assert "(silent)" in capsys.readouterr().out


def test_handshake_helpers_are_reusable() -> None:
    """The probe builds the same handshake object the client will send."""
    from app.peer.handshake import outgoing_handshake

    handshake = outgoing_handshake(INFO_HASH, b"p" * 20)
    assert isinstance(handshake, Handshake)
    assert len(handshake.encode()) == 68


class TestSwarmMode:
    """``--swarm`` drives the real peer manager against a local seeder."""

    async def test_connects_to_a_real_seeder_and_asks_for_data(self) -> None:
        context = make_context()
        seeder = MockPeer(
            b"q" * (PIECE_LENGTH * 2),
            info_hash=context.info_hash,
            piece_length=PIECE_LENGTH,
        )
        await seeder.start()
        try:
            summary = await probe_swarm(
                context,
                [(seeder.host, seeder.port)],
                limit=2,
                settle=0.3,
                config=NetworkConfig(idle_timeout=30.0),
            )
        finally:
            await seeder.stop()

        assert len(summary.connected) == 1
        report = summary.connected[0]
        assert report.pieces_held == 2
        assert report.unchoked is True
        assert report.client
        assert "qBittorrent" not in report.client  # it is our own mock, not qBittorrent

    async def test_unreachable_peers_are_reported_too(self) -> None:
        summary = await probe_swarm(make_context(), [("127.0.0.1", 1)], limit=2, settle=0.0)

        assert summary.connected == []
        assert len(summary.reports) == 1
        assert summary.reports[0].outcome == "unreachable"
        assert summary.reports[0].error

    async def test_the_slot_limit_caps_concurrent_connections(self) -> None:
        context = make_context()
        seeders = [
            MockPeer(
                b"q" * (PIECE_LENGTH * 2), info_hash=context.info_hash, piece_length=PIECE_LENGTH
            )
            for _ in range(3)
        ]
        for seeder in seeders:
            await seeder.start()
        try:
            summary = await probe_swarm(
                context,
                [(seeder.host, seeder.port) for seeder in seeders],
                limit=2,
                settle=0.3,
                config=NetworkConfig(idle_timeout=30.0),
            )
        finally:
            for seeder in seeders:
                await seeder.stop()

        assert len(summary.connected) == 2
