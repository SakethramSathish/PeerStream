"""Peer identifier generation and identification.

Every BitTorrent client needs a 20-byte ``peer_id``: it is sent in the
handshake, announced to trackers, and displayed by other peers. Two things
depend on getting it right:

**Uniqueness.** Some peers refuse multiple connections from the same id, so the
suffix is random per session.

**Convention.** The dominant convention (Azureus-style) encodes the client name
and version in the first bytes::

    -BY0100-aB3xY7qW9z12
     │ │ │    └── 12 random characters
     │ │ └────── 4-digit version (0.1.0 -> 0100)
     │ └─────────2-character client code
     └─────────── convention marker

Following the convention is what lets *other* clients show a meaningful name
next to our connections — and, in reverse, lets this client's peer table show
"qBittorrent 4.4.1" instead of a 20-byte blob. That identification is real data
derived from the handshake, not guesswork (PRD §13).
"""

from __future__ import annotations

import random
import re
import string
from typing import Final

from app import __version__
from app.core.constants import PEER_ID_SIZE

CLIENT_CODE: Final[str] = "BY"  # "Build Your own"; unregistered, so no collisions

CLIENT_NAME: Final[str] = "bittorrent-client"
"""What we call ourselves in a BEP 10 handshake's ``v`` field.

The same string the peer-id encodes, spelled out: a peer that asks what we are
gets an answer that matches the id it already has, rather than a marketing name
for a client that does not exist.
"""


def client_version_string(version: str | None = None) -> str:
    """``"bittorrent-client 0.1.0"`` — the ``v`` field we send (BEP 10)."""
    return f"{CLIENT_NAME} {version or __version__}"
_RANDOM_ALPHABET: Final[str] = string.ascii_letters + string.digits
_RANDOM_LENGTH: Final[int] = 12

# Azureus-style: -XX####-..........
_AZUREUS_PATTERN: Final[re.Pattern[bytes]] = re.compile(rb"^-([A-Za-z0-9]{2})(\d{4})-")
# Mainline (BitTorrent) style: M<x>-<y>-<z>-
_MAINLINE_PATTERN: Final[re.Pattern[bytes]] = re.compile(rb"^M(\d+)-(\d+)-(\d+)-")

CLIENT_NAMES: Final[dict[str, str]] = {
    "AG": "Ares",
    "AR": "Arctic",
    "AT": "Artemis",
    "AV": "Avicora",
    "AX": "BitPump",
    "AZ": "Azureus/Vuze",
    "BB": "BitBuddy",
    "BC": "BitComet",
    "BE": "BitTorrent SDK",
    "BF": "BitFlu",
    "BG": "BTGetit",
    "BL": "BitBlinder",
    "BP": "BitTorrent Pro",
    "BR": "BitRocket",
    "BS": "BTSlave",
    "BT": "BitTorrent",
    "BW": "BitWombat",
    "BX": "BittorrentX",
    "CD": "Enhanced CTorrent",
    "CT": "CTorrent",
    "DE": "Deluge",
    "DP": "Propagate Data Client",
    "EB": "EBit",
    "ES": "Electric Sheep",
    "FC": "FileCroc",
    "FD": "Free Download Manager",
    "FT": "FoxTorrent",
    "GR": "GetRight",
    "GS": "GSTorrent",
    "HL": "Halite",
    "HN": "Hydranode",
    "KG": "KGet",
    "KT": "KTorrent",
    "LC": "LeechCraft",
    "LH": "LH-ABC",
    "LP": "Lphant",
    "LT": "libtorrent",
    "LW": "LimeWire",
    "MO": "MonoTorrent",
    "MP": "MooPolice",
    "MR": "Miro",
    "MT": "MoonlightTorrent",
    "NX": "Net Transport",
    "PD": "Pando",
    "PE": "PeerProject",
    "PT": "PHPTracker",
    "QD": "QQDownload",
    "QT": "Qt 4 Torrent",
    "RT": "Retriever",
    "SB": "Swiftbit",
    "SD": "Thunder (Xunlei)",
    "SS": "SwarmScope",
    "ST": "SymTorrent",
    "SZ": "Shareaza",
    "TN": "TorrentDotNET",
    "TR": "Transmission",
    "TS": "Torrentstorm",
    "TT": "TuoTu",
    "UL": "uLeecher",
    "UM": "uTorrent for Mac",
    "UT": "uTorrent",
    "VG": "Vagaa",
    "WD": "WebTorrent Desktop",
    "WT": "BitLet",
    "WW": "WebTorrent",
    "WY": "FireTorrent",
    "XL": "Xunlei",
    "XT": "XanTorrent",
    "XX": "Xtorrent",
    "ZT": "ZipTorrent",
    "qB": "qBittorrent",
    "rT": "rTorrent",
    "tT": "tTorrent",
}


def _version_digits(version: str) -> str:
    """Render a dotted version as the 4-digit Azureus form.

    ``"0.1.0"`` becomes ``"0100"``; components above 9 are clamped to 9, since
    the field is a single digit wide.
    """
    parts = []
    for chunk in version.split(".")[:3]:
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(min(int(digits or "0"), 9))
    while len(parts) < 4:
        parts.append(0)
    return "".join(str(digit) for digit in parts[:4])


def generate_peer_id(*, version: str | None = None, rng: random.Random | None = None) -> bytes:
    """Generate a unique 20-byte peer id for this session.

    Args:
        version: Version string to encode (defaults to the package version).
        rng: Optional random generator, for deterministic tests.

    Returns:
        Exactly 20 bytes in Azureus ``-XX####-<random>`` form.
    """
    generator = rng or random
    suffix = "".join(generator.choice(_RANDOM_ALPHABET) for _ in range(_RANDOM_LENGTH))
    peer_id = f"-{CLIENT_CODE}{_version_digits(version or __version__)}-{suffix}"
    encoded = peer_id.encode("ascii")
    assert len(encoded) == PEER_ID_SIZE  # fixed by construction
    return encoded


def is_valid_peer_id(peer_id: bytes | bytearray | memoryview) -> bool:
    """True when ``peer_id`` is a plausible 20-byte identifier."""
    return len(bytes(peer_id)) == PEER_ID_SIZE


def identify_peer(peer_id: bytes | bytearray | memoryview) -> str:
    """Identify the client software from a peer id.

    Returns:
        A readable name such as ``"qBittorrent 4.4.1"``, ``"Unknown"`` when the
        id does not follow a known convention, or ``"Unknown (XX)"`` when the
        convention matches but the client code is not in the table.
    """
    data = bytes(peer_id)
    if not data:
        return "Unknown"

    match = _AZUREUS_PATTERN.match(data)
    if match:
        code = match.group(1).decode("ascii", errors="replace")
        digits = match.group(2).decode("ascii")
        version = _format_version(digits)
        name = CLIENT_NAMES.get(code, f"Unknown ({code})")
        return f"{name} {version}" if version else name

    mainline = _MAINLINE_PATTERN.match(data)
    if mainline:
        return "BitTorrent " + ".".join(part.decode() for part in mainline.groups())

    return "Unknown"


def _format_version(digits: str) -> str:
    """Format the 4-digit Azureus version field, dropping a trailing zero."""
    parts = [digits[0], digits[1], digits[2]]
    if digits[3] != "0":
        parts.append(digits[3])
    return ".".join(parts)


def user_agent() -> str:
    """The ``User-Agent`` string sent to HTTP trackers."""
    return f"bittorrent-client/{__version__}"
