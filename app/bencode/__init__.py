"""Dependency-free bencode codec.

Bencode is the wire format of BitTorrent: ``.torrent`` files, HTTP/UDP tracker
responses and KRPC (DHT) packets are all bencoded.  This package implements the
codec in full — no third-party dependency — so that:

* the parser can enforce canonical form and hard resource limits at the exact
  boundary where untrusted bytes enter the process (see :mod:`.decoder`);
* the encoder can guarantee byte-stable output, which the ``info_hash`` depends
  on (see :mod:`.encoder`).

Quick start::

    from app.bencode import decode, encode

    torrent = decode(open("ubuntu.torrent", "rb").read())
    assert encode(torrent["info"])  # canonical bytes for hashing

Exports:
    decode, decode_prefix, Decoder — decoding entry points
    encode, encoded_size, Encoder  — encoding entry points
    BencodeValue                   — recursive type alias
    BencodeError and subclasses    — error types
"""

from __future__ import annotations

from app.bencode.decoder import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_LENGTH,
    BencodeValue,
    Decoder,
    decode,
    decode_prefix,
)
from app.bencode.encoder import Encoder, encode, encoded_size
from app.bencode.errors import BencodeDecodeError, BencodeEncodeError, BencodeError

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_LENGTH",
    "BencodeDecodeError",
    "BencodeEncodeError",
    "BencodeError",
    "BencodeValue",
    "Decoder",
    "Encoder",
    "decode",
    "decode_prefix",
    "encode",
    "encoded_size",
]
