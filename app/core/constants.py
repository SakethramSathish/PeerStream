"""Protocol and engine constants.

Values that are fixed by the BitTorrent specification live here so that no
magic numbers are scattered through the networking and storage code. Values
that are *choices* (timeouts, limits, sizes we impose) belong in
:mod:`app.core.config` instead — those are user-tunable.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------- handshake
PROTOCOL_STRING: Final[bytes] = b"BitTorrent protocol"
PROTOCOL_STRING_LENGTH: Final[int] = 19
HANDSHAKE_LENGTH: Final[int] = 68  # 1 + 19 + 8 + 20 + 20
RESERVED_BYTES_LENGTH: Final[int] = 8
INFO_HASH_SIZE: Final[int] = 20
PEER_ID_SIZE: Final[int] = 20

# ------------------------------------------------------------ peer messages
MSG_CHOKE: Final[int] = 0
MSG_UNCHOKE: Final[int] = 1
MSG_INTERESTED: Final[int] = 2
MSG_NOT_INTERESTED: Final[int] = 3
MSG_HAVE: Final[int] = 4
MSG_BITFIELD: Final[int] = 5
MSG_REQUEST: Final[int] = 6
MSG_PIECE: Final[int] = 7
MSG_CANCEL: Final[int] = 8
MSG_PORT: Final[int] = 9  # BEP 5: DHT port
MSG_EXTENDED: Final[int] = 20  # BEP 10: extension protocol

MESSAGE_HEADER_LENGTH: Final[int] = 4  # length prefix
MESSAGE_ID_LENGTH: Final[int] = 1

# Payload sizes for the fixed-size messages, used to validate peers.
REQUEST_PAYLOAD_LENGTH: Final[int] = 12  # index(4) + begin(4) + length(4)
HAVE_PAYLOAD_LENGTH: Final[int] = 4
PIECE_HEADER_LENGTH: Final[int] = 8  # index(4) + begin(4), then block data

# The largest frame we will accept from a peer. The protocol's own message
# length prefix allows 2^32-1 bytes; no legitimate message is that large, and
# honouring the declared length would let a peer make us allocate 4 GiB.
MAX_MESSAGE_LENGTH: Final[int] = 64 * 1024
# The largest block we will request or serve.
MAX_BLOCK_SIZE: Final[int] = 128 * 1024
DEFAULT_BLOCK_SIZE: Final[int] = 16 * 1024
# Piece size when a torrent does not fix one (the metainfo always does in
# practice); 256 KiB is what most modern clients choose.
DEFAULT_PIECE_LENGTH: Final[int] = 256 * 1024

# ------------------------------------------------------------------ network
DEFAULT_LISTEN_PORT: Final[int] = 6881

DEFAULT_DHT_ANNOUNCE_INTERVAL: Final[float] = 900.0
"""Seconds between DHT ``announce_peer`` passes. BEP 5 asks for fifteen minutes."""
# Port 0 means "not listening", so it is never a valid peer endpoint.
MIN_PORT: Final[int] = 1
MAX_PORT: Final[int] = 65535
DEFAULT_HANDSHAKE_TIMEOUT: Final[float] = 15.0
DEFAULT_REQUEST_TIMEOUT: Final[float] = 60.0
DEFAULT_CONNECTION_TIMEOUT: Final[float] = 10.0
# BEP 3 suggests a keep-alive roughly every two minutes; 90 s leaves room for
# one to be lost before a peer decides we went away.
DEFAULT_KEEPALIVE_INTERVAL: Final[float] = 90.0
# No bytes at all from a peer for this long means the connection is dead, even
# if the socket still looks open (half-open connections are common).
DEFAULT_IDLE_TIMEOUT: Final[float] = 150.0
# Consecutive connect/handshake failures before a candidate is dropped: home
# connections come and go, but retrying a dead address forever is noise.
DEFAULT_MAX_PEER_FAILURES: Final[int] = 3
DEFAULT_RECONNECT_DELAY: Final[float] = 30.0
# How often the peer manager tops its connection count back up.
DEFAULT_REFILL_INTERVAL: Final[float] = 10.0

# --------------------------------------------------------------- download
# How long a block request may go unanswered before the block is handed to
# someone else. Distinct from the connection's idle timeout: a peer can be
# perfectly alive (keep-alives flowing) and still never answer a request.
DEFAULT_BLOCK_TIMEOUT: Final[float] = 30.0
# Remaining blocks at which endgame starts racing the last pieces.
DEFAULT_ENDGAME_THRESHOLD: Final[int] = 20
DEFAULT_ENDGAME_DELAY: Final[float] = 1.0

# ----------------------------------------------------------------- upload
# How many peers we upload to at once. Four is the classic value: enough to
# keep a swarm healthy, few enough that each one gets a useful share.
DEFAULT_UPLOAD_SLOTS: Final[int] = 4
# How often the optimistic-unchoke slot moves to somebody else. Long enough
# for the new peer to prove itself, short enough to keep sampling the swarm.
DEFAULT_OPTIMISTIC_UNCHOKE_INTERVAL: Final[float] = 30.0
# How often the choking policy is re-evaluated.
DEFAULT_CHOKE_INTERVAL: Final[float] = 10.0
# A peer that chokes us and has sent us nothing for this long is snubbed:
# reciprocity has failed, so it drops down the ranking.
DEFAULT_SNUB_SECONDS: Final[float] = 60.0
# Requests one peer may have outstanding with us. Beyond this, a peer is
# queueing more than we could serve anyway, and the queue is pure exposure.
DEFAULT_MAX_REQUESTS_PER_PEER: Final[int] = 64
# Seconds an unanswered request stays in our queue before we drop it.
DEFAULT_UPLOAD_QUEUE_TIMEOUT: Final[float] = 30.0
"""How long a request must go unanswered before endgame mode races it.

Racing a block that was asked for a millisecond ago wastes bandwidth; racing
one that has been outstanding for a second is what saves a download from its
slowest peer at the end.
"""

# --------------------------------------------------------------- statistics
# Three windows: what is happening, what the last few seconds looked like, and
# what the trend is. The short one is the default because it is steady enough
# to read and short enough to still be true.
DEFAULT_INSTANT_WINDOW_SECONDS: Final[float] = 1.0
DEFAULT_STATS_WINDOW_SECONDS: Final[float] = 5.0
DEFAULT_LONG_WINDOW_SECONDS: Final[float] = 30.0
DEFAULT_HISTORY_SAMPLES: Final[int] = 300  # ~5 minutes at one sample/second

# ------------------------------------------------------------------ storage
DEFAULT_DOWNLOAD_DIRECTORY: Final[str] = "~/Downloads"
# Threads for SHA-1 verification. A 4 MiB piece hashes in roughly 10 ms, but at
# 50 peers that is half a second of event-loop stall per second of traffic, so
# hashing never runs on the loop.
DEFAULT_HASH_WORKERS: Final[int] = 2
# Resume-state file format. Bumped only if the layout becomes unreadable by an
# older client; unknown newer versions are rejected on load, never guessed at.
RESUME_VERSION: Final[int] = 1
RESUME_FILE_SUFFIX: Final[str] = ".resume.json"
# Suffix appended to a resume file that could not be parsed, so the bad file is
# kept for inspection instead of being silently overwritten.
RESUME_CORRUPT_SUFFIX: Final[str] = ".corrupt"

# --------------------------------------------------------------------- DHT
DHT_K: Final[int] = 8  # bucket size in Kademlia
DHT_NODE_ID_SIZE: Final[int] = 20
DHT_ALPHA: Final[int] = 3  # concurrent outstanding queries
DEFAULT_DHT_BOOTSTRAP_NODES: Final[tuple[tuple[str, int], ...]] = (
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
)
