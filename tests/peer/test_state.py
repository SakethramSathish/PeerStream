"""Unit tests for per-peer session state."""

from __future__ import annotations

import time

import pytest
from app.core.events import EventType
from app.peer.bitfield import Bitfield
from app.peer.errors import MessageError, ProtocolError
from app.peer.messages import (
    Bitfield as BitfieldMessage,
)
from app.peer.messages import (
    Cancel,
    Choke,
    Have,
    Interested,
    KeepAlive,
    NotInterested,
    Piece,
    Port,
    Request,
    Unchoke,
)
from app.peer.state import ConnectionState, PeerSession

PIECE_COUNT = 8
PIECE_LENGTH = 1024


@pytest.fixture
def session() -> PeerSession:
    return PeerSession(piece_count=PIECE_COUNT)


class TestDefaults:
    def test_starting_state_is_pessimistic(self, session: PeerSession) -> None:
        """Nothing flows until both sides say so — that is the protocol."""
        assert session.am_choking is True
        assert session.peer_choking is True
        assert session.am_interested is False
        assert session.peer_interested is False
        assert session.state is ConnectionState.CONNECTING
        assert not session.can_request

    def test_empty_bitfield(self, session: PeerSession) -> None:
        assert session.bitfield.count == 0

    def test_rejects_non_positive_piece_count(self) -> None:
        with pytest.raises(ValueError, match="piece_count must be positive"):
            PeerSession(piece_count=0)


class TestHandshake:
    def test_records_peer_identity(self, session: PeerSession) -> None:
        session.note_handshake(b"p" * 20, "qBittorrent 4.4.1")
        assert session.peer_id == b"p" * 20
        assert session.client == "qBittorrent 4.4.1"
        assert session.state is ConnectionState.CONNECTED

    def test_marking_closed(self, session: PeerSession) -> None:
        session.mark_closed()
        assert session.closed
        assert session.state is ConnectionState.CLOSED


class TestChokingAndInterest:
    def test_choke(self, session: PeerSession) -> None:
        assert session.apply(Choke()) is EventType.PEER_CHOKED
        assert session.peer_choking is True

    def test_unchoke(self, session: PeerSession) -> None:
        session.peer_choking = True
        assert session.apply(Unchoke()) is EventType.PEER_UNCHOKED
        assert session.peer_choking is False

    def test_interested(self, session: PeerSession) -> None:
        assert session.apply(Interested()) is EventType.PEER_INTERESTED
        assert session.peer_interested is True

    def test_not_interested(self, session: PeerSession) -> None:
        session.peer_interested = True
        assert session.apply(NotInterested()) is None
        assert session.peer_interested is False

    def test_can_request_only_when_interested_and_unchoked(self, session: PeerSession) -> None:
        session.apply(Unchoke())
        assert not session.can_request  # we have not said we are interested
        session.am_interested = True
        assert session.can_request
        session.apply(Choke())
        assert not session.can_request


class TestAvailability:
    def test_have_updates_the_bitfield(self, session: PeerSession) -> None:
        assert session.apply(Have(index=3)) is None
        assert session.bitfield.has(3)
        assert session.bitfield.count == 1

    def test_have_is_idempotent(self, session: PeerSession) -> None:
        session.apply(Have(index=3))
        session.apply(Have(index=3))
        assert session.bitfield.count == 1

    # A negative index never reaches the session: the message rejects it first.
    @pytest.mark.parametrize("index", [PIECE_COUNT, 999])
    def test_have_out_of_range_is_rejected(self, session: PeerSession, index: int) -> None:
        with pytest.raises(ProtocolError, match=r"piece index .* is outside"):
            session.apply(Have(index=index))

    def test_bitfield_message(self, session: PeerSession) -> None:
        data = Bitfield.from_indices([0, 2, 5], PIECE_COUNT).to_bytes()
        assert session.apply(BitfieldMessage(data=data)) is EventType.PEER_BITFIELD
        assert session.bitfield.indices() == [0, 2, 5]

    def test_second_bitfield_is_rejected(self, session: PeerSession) -> None:
        data = Bitfield(PIECE_COUNT).to_bytes()
        session.apply(BitfieldMessage(data=data))
        with pytest.raises(ProtocolError, match="second bitfield"):
            session.apply(BitfieldMessage(data=data))

    def test_short_bitfield_is_rejected(self) -> None:
        session = PeerSession(piece_count=16)
        with pytest.raises(MessageError, match="too short for 16 pieces"):
            session.apply(BitfieldMessage(data=b"\xff"))


class TestBlocks:
    def test_piece_records_the_download(self, session: PeerSession) -> None:
        before = session.downloaded
        assert session.apply(Piece(index=0, begin=0, data=b"x" * 100)) is (
            EventType.PIECE_BLOCK_RECEIVED
        )
        assert session.downloaded == before + 100

    def test_block_within_bounds(self, session: PeerSession) -> None:
        message = Piece(index=0, begin=512, data=b"x" * 512)
        assert session.apply(message, piece_length=PIECE_LENGTH) is EventType.PIECE_BLOCK_RECEIVED

    def test_block_running_past_the_piece(self, session: PeerSession) -> None:
        message = Piece(index=0, begin=1000, data=b"x" * 100)
        with pytest.raises(MessageError, match="runs past the end"):
            session.apply(message, piece_length=PIECE_LENGTH)

    def test_offset_past_the_end(self, session: PeerSession) -> None:
        message = Piece(index=0, begin=PIECE_LENGTH + 1, data=b"x")
        with pytest.raises(MessageError, match="starts past the end"):
            session.apply(message, piece_length=PIECE_LENGTH)

    def test_piece_index_out_of_range(self, session: PeerSession) -> None:
        message = Piece(index=PIECE_COUNT, begin=0, data=b"x")
        with pytest.raises(ProtocolError, match="is outside"):
            session.apply(message, piece_length=PIECE_LENGTH)

    def test_bounds_are_skipped_when_piece_length_is_unknown(self, session: PeerSession) -> None:
        """Before the torrent is known we can only check the index."""
        assert session.apply(Piece(index=1, begin=0, data=b"x")) is (EventType.PIECE_BLOCK_RECEIVED)


class TestUploadSideMessages:
    @pytest.mark.parametrize(
        "message",
        [
            Request(index=0, begin=0, length=1024),
            Cancel(index=0, begin=0, length=1024),
            Port(port=6881),
            KeepAlive(),
        ],
    )
    def test_are_recorded_without_state_change(self, session: PeerSession, message: object) -> None:
        assert session.apply(message) is None  # type: ignore[arg-type]


class TestQueries:
    def test_wanted_from(self, session: PeerSession) -> None:
        session.apply(
            BitfieldMessage(data=Bitfield.from_indices([1, 2, 3], PIECE_COUNT).to_bytes())
        )
        ours = Bitfield.from_indices([0, 1], PIECE_COUNT)
        assert session.wanted_from(ours) == [2, 3]

    def test_is_interesting(self, session: PeerSession) -> None:
        session.apply(BitfieldMessage(data=Bitfield.from_indices([1], PIECE_COUNT).to_bytes()))
        assert session.is_interesting(Bitfield(PIECE_COUNT))
        assert not session.is_interesting(Bitfield.from_indices([1], PIECE_COUNT))

    def test_progress(self, session: PeerSession) -> None:
        session.apply(BitfieldMessage(data=Bitfield.from_indices([0, 1], PIECE_COUNT).to_bytes()))
        assert session.progress == 0.25
        assert not session.is_seed

    def test_is_seed(self, session: PeerSession) -> None:
        session.apply(BitfieldMessage(data=Bitfield.full(PIECE_COUNT).to_bytes()))
        assert session.is_seed
        assert session.progress == 1.0


class TestActivity:
    def test_counters(self, session: PeerSession) -> None:
        session.record_download(100)
        session.record_upload(50)
        assert session.downloaded == 100
        assert session.uploaded == 50

    def test_touch_resets_idle_time(self, session: PeerSession) -> None:
        session.last_activity = time.monotonic() - 5
        assert session.idle_for >= 4
        session.touch()
        assert session.idle_for < 1

    def test_applying_a_message_touches_the_session(self, session: PeerSession) -> None:
        session.last_activity = time.monotonic() - 5
        session.apply(KeepAlive())
        assert session.idle_for < 1

    def test_string_form(self, session: PeerSession) -> None:
        session.note_handshake(b"p" * 20, "Transmission 3.0")
        text = str(session)
        assert "Transmission 3.0" in text
        assert "0/8" in text
        assert "connected" in text
