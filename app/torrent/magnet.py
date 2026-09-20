"""Magnet URI parsing and serialisation (BEP 9 / BEP 53 metadata exchange).

A magnet link is not a torrent. It is a *name* for one: an info-hash, a
display name, and some hints about where to look — trackers, peers, web seeds.
Everything else has to be fetched, which is why a magnet is the entry point for
the two protocols this milestone adds: DHT finds peers without a tracker
(BEP 5), and metadata exchange asks one of those peers for the ``info``
dictionary itself (BEP 9).

The parsing here is deliberately strict about the one field that cannot be
guessed and lenient about everything else:

* ``xt`` — the *exact topic* — is required, and is the torrent's identity. It
  may be 40 hex characters or 32 base32 characters; both are the same 20 bytes
  and both appear in the wild.
* ``dn``, ``tr``, ``ws``, ``x.pe``, ``so`` and friends are hints. A hint that
  does not parse is dropped rather than failing the whole link, because a
  magnet with one bad tracker is still a magnet.

Two things this module refuses to do:

* **Invent a name.** A magnet without ``dn`` has no name. The UI shows the
  info-hash until metadata arrives, rather than something like "magnet download".
* **Pretend to support BitTorrent v2.** ``xt=urn:btmh:`` (BEP 52) identifies a
  v2 torrent by a truncated SHA-256. We cannot download those, so a v2-only
  magnet raises :class:`UnsupportedMagnetError` with that reason instead of
  failing later with a confusing error, or worse, silently hashing the wrong
  thing.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, unquote, urlsplit

from app.core.constants import MAX_PORT, MIN_PORT
from app.torrent.errors import MagnetError, UnsupportedMagnetError
from app.torrent.info_hash import INFO_HASH_SIZE, format_info_hash, validate_info_hash

SCHEME: Final[str] = "magnet"
XT: Final[str] = "xt"  # exact topic: the torrent's identity
DISPLAY_NAME: Final[str] = "dn"
TRACKER: Final[str] = "tr"
WEB_SEED: Final[str] = "ws"
PEER: Final[str] = "x.pe"  # BEP 5: a peer address, host:port
SELECT_ONLY: Final[str] = "so"  # BEP 53: file indices to download
ACCEPTABLE_SOURCE: Final[str] = "as"
EXACT_SOURCE: Final[str] = "xs"
KEYWORD: Final[str] = "kt"

BTIH_PREFIX: Final[str] = "urn:btih:"  # v1: SHA-1 of the info dictionary
BTMH_PREFIX: Final[str] = "urn:btmh:"  # v2 (BEP 52): multihash of the info dict
HEX_LENGTH: Final[int] = INFO_HASH_SIZE * 2  # 40
BASE32_LENGTH: Final[int] = 32  # 20 bytes in base32, no padding


@dataclass(frozen=True, slots=True)
class MagnetUri:
    """A parsed magnet link.

    Attributes:
        info_hash: The 20-byte v1 info-hash — the only mandatory part.
        display_name: ``dn``, when the link carries one. Percent-decoded.
        trackers: ``tr`` URLs, in the order they appeared. Each becomes its own
            tier, since a magnet does not describe tiers.
        peers: ``x.pe`` addresses (BEP 5), for a swarm we can dial directly
            without waiting for DHT.
        web_seeds: ``ws`` URLs (BEP 17/19), handed to the download engine as
            read-only sources.
        select_only: ``so`` file indices (BEP 53), for "download just these".
        sources: ``xs`` and ``as`` URLs — alternative locations for the
            metadata itself.
        keywords: ``kt`` search terms, kept so a re-serialised link round-trips.
        raw: The original text, kept for logging and for "what did I paste".
    """

    info_hash: bytes
    display_name: str | None = None
    trackers: tuple[str, ...] = ()
    peers: tuple[tuple[str, int], ...] = ()
    web_seeds: tuple[str, ...] = ()
    select_only: tuple[int, ...] = ()
    sources: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    raw: str = ""

    def __post_init__(self) -> None:
        validate_info_hash(self.info_hash)

    # ------------------------------------------------------------------ reading

    @property
    def hex_info_hash(self) -> str:
        """The info-hash as lowercase hex."""
        return format_info_hash(self.info_hash)

    @property
    def urn(self) -> str:
        """The exact topic, as it appears in a link."""
        return f"{BTIH_PREFIX}{self.hex_info_hash}"

    @property
    def name(self) -> str:
        """What to call this torrent before the metadata arrives.

        A magnet without ``dn`` has no name, and inventing one is how a UI ends
        up showing "magnet download" for three different torrents. The
        info-hash is at least true.
        """
        return self.display_name or self.hex_info_hash

    @property
    def tiers(self) -> tuple[tuple[str, ...], ...]:
        """The trackers as tiers: one URL each, since a magnet has no tiers."""
        return tuple((url,) for url in self.trackers)

    # ----------------------------------------------------------------- writing

    def to_uri(self) -> str:
        """Re-serialise the link, canonically.

        The info-hash is written as hex rather than base32: both are legal, and
        hex is what a person pastes back into a search box expecting to find
        the same torrent.
        """
        parts = [f"{XT}={self.urn}"]
        if self.display_name:
            parts.append(f"{DISPLAY_NAME}={_quote(self.display_name)}")
        for url in self.trackers:
            parts.append(f"{TRACKER}={_quote(url)}")
        for host, port in self.peers:
            parts.append(f"{PEER}={_quote(f'{host}:{port}')}")
        for url in self.web_seeds:
            parts.append(f"{WEB_SEED}={_quote(url)}")
        for url in self.sources:
            parts.append(f"{EXACT_SOURCE}={_quote(url)}")
        if self.select_only:
            parts.append(f"{SELECT_ONLY}={','.join(str(index) for index in self.select_only)}")
        for keyword in self.keywords:
            parts.append(f"{KEYWORD}={_quote(keyword)}")
        return f"{SCHEME}:?{'&'.join(parts)}"

    def __str__(self) -> str:
        return self.to_uri()

    def with_trackers(self, trackers: Sequence[str]) -> MagnetUri:
        """A copy carrying these trackers as well as its own."""
        merged = list(self.trackers)
        for url in trackers:
            if url not in merged:
                merged.append(url)
        return MagnetUri(
            info_hash=self.info_hash,
            display_name=self.display_name,
            trackers=tuple(merged),
            peers=self.peers,
            web_seeds=self.web_seeds,
            select_only=self.select_only,
            sources=self.sources,
            keywords=self.keywords,
            raw=self.raw,
        )


def is_magnet(text: str) -> bool:
    """Whether this text looks like a magnet link.

    Cheap and forgiving: it exists so a dialog can decide which parser to call
    without the risk that a guessed parse raises.
    """
    return text.strip().lower().startswith(f"{SCHEME}:")


def parse_magnet(text: str) -> MagnetUri:
    """Parse a magnet URI.

    Args:
        text: The link, with or without surrounding whitespace.

    Returns:
        The parsed link.

    Raises:
        MagnetError: No ``xt``, or an ``xt`` that is not a usable v1 hash.
        UnsupportedMagnetError: The link names a v2 torrent (``urn:btmh:``),
            which this client cannot download.
    """
    source = text.strip()
    if not source:
        raise MagnetError("the magnet link is empty")

    parts = urlsplit(source)
    if parts.scheme.lower() != SCHEME:
        raise MagnetError(f"not a magnet link: {source[:40]!r} does not start with 'magnet:'")

    pairs = parse_qsl(parts.query, keep_blank_values=False, strict_parsing=False)
    fields: dict[str, list[str]] = {}
    for key, value in pairs:
        fields.setdefault(key.lower(), []).append(value)

    info_hash = _info_hash_from(fields.get(XT, []))
    return MagnetUri(
        info_hash=info_hash,
        display_name=_first_unquoted(fields.get(DISPLAY_NAME, [])),
        trackers=_urls(fields.get(TRACKER, [])),
        peers=_peers(fields.get(PEER, [])),
        web_seeds=_urls(fields.get(WEB_SEED, [])),
        select_only=_select_only(fields.get(SELECT_ONLY, [])),
        sources=_urls(fields.get(EXACT_SOURCE, []) + fields.get(ACCEPTABLE_SOURCE, [])),
        keywords=_keywords(fields.get(KEYWORD, [])),
        raw=source,
    )


def magnet_for(
    info_hash: bytes,
    *,
    display_name: str | None = None,
    trackers: Sequence[str] = (),
    peers: Sequence[tuple[str, int]] = (),
) -> MagnetUri:
    """Build a magnet link for a torrent we already have.

    Used to share a torrent, and by tests that need a link for a known hash.

    Args:
        info_hash: The torrent's info hash.
        display_name: Optional ``dn``.
        trackers: Optional ``tr`` URLs.
        peers: Optional ``x.pe`` addresses, for peers we already know are in
            the swarm — worth putting in the link, since it costs the receiver
            a DHT walk.
    """
    return MagnetUri(
        info_hash=validate_info_hash(info_hash),
        display_name=display_name,
        trackers=tuple(trackers),
        peers=tuple(peers),
        raw="",
    )


# ------------------------------------------------------------------- internals


def _info_hash_from(values: Sequence[str]) -> bytes:
    """The info-hash from one or more ``xt`` values.

    A link may carry several topics (a v1 and a v2 hash, for a hybrid torrent).
    We take the first v1 hash we can use, and only complain about v2 if that is
    all there is: a hybrid magnet is a torrent we can download.
    """
    if not values:
        raise MagnetError("the magnet link has no 'xt' (exact topic): it names no torrent")

    v2_seen = False
    for value in values:
        topic = value.strip().lower()
        if topic.startswith(BTIH_PREFIX):
            return _decode_btih(topic[len(BTIH_PREFIX) :])
        if topic.startswith(BTMH_PREFIX):
            v2_seen = True
    if v2_seen:
        raise UnsupportedMagnetError(
            "this magnet names a BitTorrent v2 torrent (urn:btmh:) and this client speaks v1 only"
        )
    raise MagnetError(f"the magnet link's 'xt' is not a BitTorrent info-hash: {values[0]!r}")


def _decode_btih(digest: str) -> bytes:
    """Decode the 20 bytes of an info-hash from hex or base32."""
    candidate = digest.strip()
    if len(candidate) == HEX_LENGTH:
        try:
            return validate_info_hash(bytes.fromhex(candidate))
        except ValueError as exc:
            raise MagnetError(f"'xt' is not {HEX_LENGTH} hex characters: {exc}") from exc
    if len(candidate) == BASE32_LENGTH:
        # Base32 in the wild is unpadded and sometimes lowercase.
        padded = candidate.upper() + "=" * (-len(candidate) % 8)
        try:
            return validate_info_hash(base64.b32decode(padded))
        except (binascii.Error, ValueError) as exc:
            raise MagnetError(f"'xt' is not valid base32: {exc}") from exc
    raise MagnetError(
        f"'xt' must be {HEX_LENGTH} hex or {BASE32_LENGTH} base32 characters, got {len(candidate)}"
    )


def _quote(value: str) -> str:
    """Percent-encode a value for a link, leaving readable characters alone."""
    from urllib.parse import quote

    return quote(value, safe=":/?&=~.-_")


def _first_unquoted(values: Sequence[str]) -> str | None:
    """Percent-decode the first value, or ``None`` when there is nothing."""
    if not values:
        return None
    decoded = unquote(values[0]).strip()
    return decoded or None


def _urls(values: Sequence[str]) -> tuple[str, ...]:
    """Percent-decoded URLs, in order, with duplicates removed.

    A hint that does not look like a URL is dropped rather than failing the
    link: one malformed tracker is not a reason to refuse the torrent.
    """
    urls: list[str] = []
    for value in values:
        url = unquote(value).strip()
        if not url or "://" not in url:
            continue
        if url not in urls:
            urls.append(url)
    return tuple(urls)


def _peers(values: Sequence[str]) -> tuple[tuple[str, int], ...]:
    """``x.pe`` addresses, ``host:port``, skipping the ones that make no sense."""
    peers: list[tuple[str, int]] = []
    for value in values:
        address = unquote(value).strip()
        if not address or ":" not in address:
            continue
        host, _, port_text = address.rpartition(":")
        host = host.strip().strip("[]")
        if not host or not port_text.isdigit():
            continue
        port = int(port_text)
        if not MIN_PORT <= port <= MAX_PORT:
            continue
        if (host, port) not in peers:
            peers.append((host, port))
    return tuple(peers)


def _select_only(values: Sequence[str]) -> tuple[int, ...]:
    """``so`` file indices. A non-numeric index is dropped, not guessed at."""
    indices: list[int] = []
    for value in values:
        for item in unquote(value).split(","):
            item = item.strip()
            if item.isdigit() and int(item) not in indices:
                indices.append(int(item))
    return tuple(indices)


def _keywords(values: Sequence[str]) -> tuple[str, ...]:
    """`kt` search terms, percent-decoded and in order."""
    keywords: list[str] = []
    for value in values:
        word = unquote(value).strip()
        if word and word not in keywords:
            keywords.append(word)
    return tuple(keywords)
