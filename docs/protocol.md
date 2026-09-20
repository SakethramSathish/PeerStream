# BitTorrent protocol reference (as implemented)

A working reference for the parts of the protocol this client implements. It is
written from the implementation outward — where the specs leave room for
interpretation, this document records what this client does and why.

Primary references: BEP 3 (the base protocol), BEP 5 (the DHT), BEP 9 and
BEP 53 (magnet links and metadata exchange), BEP 10 (the extension protocol),
BEP 11 (peer exchange), BEP 12 (multitracker), BEP 15 (UDP trackers), BEP 27
(private torrents), BEP 52 (the BitTorrent v2 hash algorithm, not yet
implemented).

Sections marked **planned** belong to later milestones and are documented when
that milestone lands.

Some things will not arrive at all: µTP (BEP 29), message stream encryption,
web seeding (BEP 19), local service discovery (BEP 14), UPnP/NAT-PMP and the
BitTorrent v2 hash tree (BEP 52) are closed scope, with the reasons recorded in
the implementation plan. A protocol reference that quietly skips them reads as
an oversight, so this one says what it will never describe.

---

## 1. Bencode

Bencode is the serialisation used for `.torrent` files, tracker responses and
DHT packets. Four types, no whitespace, no comments:

| Type | Encoding | Example |
|---|---|---|
| Integer | `i<digits>e` | `i42e`, `i-7e`, `i0e` |
| Byte string | `<len>:<bytes>` | `5:hello`, `0:` |
| List | `l<items>e` | `li1e1:ae` |
| Dictionary | `d<key><value>...e` | `d1:ai1ee` |

Rules that matter in practice:

* **Dictionary keys are byte strings and must be sorted** (ascending byte
  order). BEP 3 mandates this for the `info` dictionary; violating it changes
  the `info_hash`.
* **Integers are canonical**: no leading zeros, no `-0`. `i042e` is invalid.
* Byte strings are binary, not text. Field *names* are ASCII; names and paths
  are arbitrary bytes (UTF-8 by convention, legacy encodings in the wild).

This client's implementation (`app/bencode/`) rejects non-canonical integers and
imposes limits on input size (64 MiB), nesting depth (100) and token digit
counts, so a hostile file cannot exhaust memory while being parsed.

---

## 2. Metainfo (`.torrent`) file structure

```
{
  "announce":        <url>,              # primary tracker
  "announce-list":   [[<url>, ...], ...] # tiers, tried in order (BEP 12)
  "creation date":   <integer>,          # POSIX timestamp
  "comment":         <string>,
  "created by":      <string>,
  "info": { ... }                        # the part that is hashed
}
```

### 2.1 Single-file `info`

```
{
  "name":         <string>,   # file name
  "length":       <integer>,  # total size in bytes
  "piece length": <integer>,  # bytes per piece
  "pieces":       <bytes>,    # 20-byte SHA-1 per piece, concatenated
  "private":      <0|1>       # optional, BEP 27
}
```

### 2.2 Multi-file `info`

`length` is replaced by `files`; `name` becomes the download directory:

```
{
  "name":         <string>,   # root directory name
  "piece length": <integer>,
  "pieces":       <bytes>,
  "files": [
    {"length": <integer>, "path": ["dir", "sub", "file.bin"]},
    ...
  ]
}
```

Exactly one of `length` and `files` is present. A torrent declaring both is
rejected rather than guessed at.

### 2.3 The torrent as one byte stream

Files are windows onto a single continuous byte stream. Concatenating every
file in `files` order reproduces the stream that `pieces` describes; a piece
therefore frequently straddles two files:

```
files:   [ A: 0..6 ][ B: 6..12 ]
pieces:  [ 0..4 ][ 4..8 ][ 8..12 ]
                  ^^^^^^ spans A and B
```

`Torrent.piece_size(i)`, `piece_offset(i)` and `files_in_piece(i)` expose this
geometry, and the storage layer (M6) uses it to scatter one piece across
several file handles — `FileLayout.plan(offset, data)` returns the per-file
chunks for any range, so a piece that straddles two files is not a special
case.

---

## 3. info_hash

```
info_hash = SHA1( bencode( info ) )
```

The hash covers the `info` dictionary *only*, which is why two torrents for the
same content with different trackers or comments share an identity.

**This client hashes the original bytes of the encoded `info` value**, not a
re-encoding of the parsed dictionary. The distinction is not academic: some
encoders emit dictionary keys out of order, and re-encoding normalises that
ordering, producing a hash no other peer in the swarm recognises. The client
would then announce, handshake, and silently download nothing.

* `info_hash_from_bencoded(raw_bytes)` — used when parsing a `.torrent` file.
* `compute_info_hash(info_mapping)` — canonical re-encoding, used only for
  metadata obtained without its original bytes — a magnet's info dictionary,
  fetched from a peer (BEP 9).

Consequences of the `info_hash` being the torrent's identity:

* tracker announces are keyed by it;
* the peer handshake carries it, and a mismatch means the connection is dropped;
* swarms are joined by it — never by file name.

---

## 4. Tracker protocols

* **HTTP/HTTPS (milestone M3, implemented).** `GET` announce with `info_hash`,
  `peer_id`, `port`, `uploaded`, `downloaded`, `left`, `event`, `compact`.
  Response is a bencoded dictionary with `interval`, `complete`, `incomplete`
  and `peers` (either a compact 6-byte-per-peer string or a list of
  dictionaries).

  Two details the implementation has to get right:

  - `info_hash` and `peer_id` are **arbitrary 20-byte values, not text**, so
    they are percent-encoded with `quote_from_bytes` and never decoded on the
    way out. The announce URL may already carry a query (passkey trackers do),
    so parameters are appended with `&` when a `?` is present.
  - `peers` has three shapes: 6-byte compact IPv4, 18-byte compact IPv6 under
    `peers6` (BEP 7), and the legacy list of `{ip, port, peer id}`
    dictionaries. All three are parsed; peers advertising port 0 are dropped.

  `complete`/`incomplete` are optional. When a tracker omits them the response
  records `None`, and the UI must render "not reported" rather than 0.
* **UDP (BEP 15, milestone M14: implemented).** `app/tracker/udp_tracker.py`
  sends the same frames over `asyncio.DatagramProtocol` — connect/announce/scrape
  with a 64-bit connection id and a 32-bit transaction id — because a UDP
  announce is ~1/10th the bytes of an HTTP one.

  A UDP announce is a conversation, and the client keeps its side of it:

  - **Connect first.** A 16-byte `connect` (magic `0x41727101980`, action 0)
    buys a connection id, which is cached for a minute and renegotiated when it
    expires, when a request goes unanswered, or when the tracker refuses it.
  - **Transaction ids are checked at the socket edge.** A reply whose id is not
    the one we just sent is dropped, never parsed: a late, duplicated or
    foreign datagram is otherwise indistinguishable from an answer.
  - **Retries are how failure is detected.** UDP reports nothing when a tracker
    is gone, and an ICMP "port unreachable" is a hint rather than an answer, so
    every request is retried with exponential backoff and then declared timed
    out.
  - **`event` is numbered the UDP way**: none 0, completed 1, started 2,
    stopped 3 — not the HTTP ordering.
  - **Scrapes are batched** at 74 info hashes, the most that fit in one packet.

  A UDP tracker always reports `complete` and `incomplete`, so
  `swarm_reported` is always true for these responses.

Only `http`, `https` and `udp` tracker URLs are accepted at parse time; others
are dropped with a warning rather than failing the torrent.

---

## 5. Peer wire protocol

**Implemented (milestone M4: framing, handshake, bitfields, session state;
milestone M5: connections, keep-alives, interest).** TCP, with this 68-byte
handshake first:

```
+--------+--------------------------------+
| 1 byte | pstrlen = 19                   |
| 19     | "BitTorrent protocol"          |
| 8      | reserved / extension flags     |
| 20     | info_hash                      |
| 20     | peer_id                        |
+--------+--------------------------------+
```

Both sides send it immediately; both verify the protocol string and the
`info_hash` before sending anything else.

Then a stream of length-prefixed messages:

```
[ 4-byte length ][ 1-byte id ][ payload ]
```

| Id | Message | Payload |
|---|---|---|
| — | keep-alive | *(length 0, no id)* |
| 0 | choke | — |
| 1 | unchoke | — |
| 2 | interested | — |
| 3 | not interested | — |
| 4 | have | piece index |
| 5 | bitfield | bitfield of owned pieces |
| 6 | request | index, begin, length |
| 7 | piece | index, begin, block data |
| 8 | cancel | index, begin, length |
| 9 | port | DHT port (BEP 5) |
| 20 | extended | extension id byte + bencoded body (BEP 10) |

Everything a peer sends is untrusted: message lengths are bounded, indices are
range-checked against `piece_count`, and `begin + length` must stay inside the
piece.

How the implementation handles the parts that break naive clients:

* **Keep-alive is a zero-length frame**, not an id byte. Reading it as an id
  desynchronises the stream, so the length prefix is checked first.
* **Length prefixes are refused before the payload is read.** The protocol
  allows 2^32-1 bytes; honouring that would let a peer make us allocate 4 GiB.
  The limit is 64 KiB by default and configurable.
* **Partial and batched frames are the normal case.** `StreamReader.readexactly`
  reads exactly the declared length, so a frame split across segments arrives
  whole and the bytes of the next message stay buffered — no manual reassembly.
* **Validation is split by what each layer knows.** A message cannot know the
  torrent's piece count, so `index` is only checked for being non-negative
  there; `PeerSession` performs the `piece_count` and `piece_length` checks.
* **Failure taxonomy matters for recovery.** A peer hanging up is normal and
  retried; a peer sending malformed frames is not.

**The extension protocol (BEP 10).** Message id 20 carries one byte naming
which extension is speaking, then that extension's bencoded payload. Id 0 is
the extension handshake itself, in which each side advertises `m` — a map of
extension name to the id *it* numbers that extension with.

**The ids belong to the receiver.** That is the rule the whole extension
protocol turns on, and the one most implementations get wrong once. A message
we send carries the id from the *peer's* `m`; a message we read carries an id
from *ours*. Both sides can encode and decode perfectly and still never
understand each other, which is why `app/peer/extension.py` keeps the two maps
apart by name (`our_id` / `their_id`) rather than storing one "the id". An id
of 0 in a peer's map means "not offered", because 0 is the handshake itself.

This client sets the reserved bit only when it has an extension to offer, and
offers two: `ut_metadata` when fetching metadata for a magnet (§8), and
`ut_pex` on any connection whose torrent is not private (§9). Advertising more
would be a promise to answer messages it has no answer for. A peer whose
handshake is unreadable costs us the extensions, not the connection — it may
still be perfectly good at pieces.

The handshake also carries `v` (client version), `p` (a port), `metadata_size`
and `reqq`. We send `v` as `bittorrent-client <version>`, which is the same
string our peer id encodes, and we read `p` because it is the only way to learn
a dialable port for a peer that connected to *us* (§9).

Connection behaviour (M5): after the handshake the client sends `interested`,
keeps the socket warm with a keep-alive every `keepalive_interval` (90 s) and
drops a peer that has been silent for `idle_timeout` (150 s) — the deadline is
on the *stream*, not on individual messages, so keep-alives keep a healthy but
data-less peer alive. A failed candidate is retried with exponential backoff
(`reconnect_delay · 2^(failures-1)`) and dropped after `max_peer_failures`.

---

## 6. Piece and block model

Pieces (typically 16 KiB–16 MiB; the torrent fixes the size) are the unit of
**integrity**: a piece is only written to disk once `SHA1(piece)` matches the
hash from the metainfo.

Blocks (16 KiB) are the unit of **transfer**: `request` messages ask for one
block, not a whole piece. This keeps a slow peer from monopolising a piece and
lets several peers contribute to one piece concurrently.

```
Piece (4 MiB)
 ├── block 0  (16 KiB)  verified as part of the piece
 ├── block 1  (16 KiB)
 ...
```

Piece states, as surfaced by the UI's piece matrix:

```
MISSING → REQUESTED → DOWNLOADING → DOWNLOADED → VERIFYING → VERIFIED
                                                      └────→ FAILED → MISSING
```

---

## 7. DHT / KRPC (BEP 5)

Kademlia over UDP, in `app/discovery/dht/`. Three pieces, in the order a lookup
touches them.

**The codec** (`krpc.py`) is bencode over datagrams, with the conventions that
are easy to get wrong:

* `nodes` is a **string** of 26-byte records (20-byte id, 4-byte IPv4, 2-byte
  port), not a list of them;
* `values` is a **list** of 6-byte addresses (4-byte IPv4, 2-byte port);
* every message carries a transaction id, `t`, which is the only thing that ties
  a reply to the question it answers. A datagram that arrives without one we
  asked for is dropped rather than guessed at.

**The routing table** (`routing.py`) is the usual k-buckets over XOR distance,
with two decisions worth naming. A newcomer loses to a node we have already
spoken to: a bucket of long-lived contacts is worth more than a stranger, and
preferring stability is what keeps the table useful. And only the bucket
covering *our own* id is ever split, because resolution near ourselves is the
resolution every lookup starts from — splitting distant buckets would spend
memory on parts of the space we route through once.

**The walk** (`node.py`) asks the `alpha` closest nodes, then the closest nodes
those name, until the closest `k` have all been asked. It answers the same four
questions from others, which is not politeness: a node that only asks is a
leech on everyone else's routing table. `announce_peer` needs a *token* issued
by the `get_peers` reply it is answering, minted from a rotating secret — the
protocol's only defence against announcing an address you do not control.

**Announcing is the other half, and it is a lookup first.** `get_peers` finds a
swarm; `announce_peer` puts us in one. Because a node only accepts a token it
issued itself, publishing a torrent costs a full iterative walk before a single
announce can be sent — which is why `dht_announcer.py` runs on BEP 5's fifteen
minutes rather than on the peer manager's five seconds, and why it announces one
torrent at a time. Three rules it holds to:

* **We publish our TCP listening port, with `implied_port` 0.** `implied_port`
  asks the node to record our UDP source port instead, which is the socket the
  DHT answers on and accepts no peer connections on. Publishing it would send
  every peer that finds us to a port that refuses them.
* **A torrent that is not listening is not published.** There is no address to
  hand out, so the announce is skipped and counted as skipped — the DHT panel
  shows the number that were actually accepted, not the number we asked for.
* **A private torrent is never published** (BEP 27). The flag means
  tracker-only, and the DHT is a tracker nobody controls.

**A skip is not a verdict.** A torrent with no listening port yet is *not yet*
publishable rather than unpublishable: its engine binds a socket a moment after
it is registered, and a torrent added paused may be resumed an hour later. So a
pass that skips a torrent for that reason shortens the next wait to thirty
seconds until something is published or unregistered, instead of leaving the
client invisible for fifteen minutes after every start. A private torrent's
refusal never changes, so it earns no retry.

BEP 5 has no goodbye. `unregister()` stops us refreshing an entry and the remote
node's own TTL (30 minutes here) expires it; `go_quiet()` gives the two minutes
of pre-shutdown silence the protocol asks for.

Two failure modes are ordinary rather than exceptional, and are handled as such:
silence (a node that does not answer costs one timeout and is retried, then
recorded as a failure) and nonsense (a malformed packet is dropped and never
fatal to the node).

## 8. Magnet links (BEP 9 / BEP 53)

A magnet names a torrent without describing it:

```
magnet:?xt=urn:btih:<40 hex or 32 base32>&dn=<name>&tr=<url>&x.pe=<host:port>
```

`xt` is the only part that must be there, and it is the only part the parser is
strict about — 40 hex characters or 32 base32, both the same 20 bytes. A
v2-only link (`urn:btmh:`, BEP 52) is refused with a message naming the version
we cannot fetch, rather than starting a search that cannot succeed; a hybrid
link uses its v1 hash. Hints that do not parse — a tracker that is not a URL, a
peer with no port — are dropped, because one bad hint is not a reason to refuse
a torrent.

Resolving one takes two searches in sequence:

1. **Find peers.** The link's `x.pe` addresses, its `tr` trackers, and the DHT,
   asked concurrently because they fail independently.
2. **Ask one of them for the info dictionary** (BEP 9, over the BEP 10
   extension protocol). The peer's extension handshake names the id *it* numbers
   `ut_metadata` with, so nothing about that id is assumed. Chunks are 16 KiB,
   and the chunk bytes ride **after** the bencoded dictionary in a `data`
   message — the detail every implementation gets wrong once.

Then the assembled bytes are hashed and compared with the magnet's info hash.
That check is the whole security model: the metadata came from a stranger, and
the hash is the only thing that makes it *the torrent we asked for* rather than
merely *a torrent*.

BEP 27's `private` flag is honoured as early as it can be, which is later than
one would like: it lives inside the info dictionary, so it cannot be read until
the metadata has arrived. A torrent that turns out to be private loses every
DHT-discovered peer and carries the flag forward, so nothing asks the DHT about
it again.

---

## 9. Peer exchange (BEP 11)

`ut_pex`, over BEP 10, in `app/discovery/pex.py`. Once a swarm has been
bootstrapped by a tracker or the DHT, PEX is what keeps it supplied: every peer
we are already talking to knows a few others, and swaps them about once a
minute. In a trackerless swarm it is often the only source still producing
addresses.

The wire format is compact records — six bytes per IPv4 contact, eighteen per
IPv6 — in a bencoded dictionary of `added`, `added.f`, `added6`, `added6.f`,
`dropped` and `dropped6`. The format is the easy part. The rules are what make
it safe, and each is enforced by `PexLedger`, the per-connection memory of who
has been told what:

* **Only peers we are connected to.** A contact is advertised once its handshake
  completes and retracted once it is gone. Advertising candidates we have not
  dialled would make every client a relay for addresses nobody verified, which
  is how a swarm gets used to aim connection attempts at a third party.
* **One message per peer per minute.** Batching is mandatory. The ledger refuses
  to build a message sooner, so the caller can ask as often as it likes.
* **Caps.** Fifty added and fifty dropped per message, two hundred for the first
  (a peer that joins late should not learn the swarm one contact a minute), with
  the remainder waiting rather than being discarded. No contact appears twice,
  and none appears in both lists — held by construction, and asserted from the
  outside by a seeded 400-step sequence.
* **Flags only for what we measured.** BEP 11 defines five bits; we set two.
  `0x02` (seed) when the peer's own bitfield says it holds every piece, and
  `0x10` (reachable) when we dialled it and it answered — a peer that dialled us
  proves nothing about whether we could dial it back. The other three describe
  encryption, uTP and holepunching: features this client does not implement, so
  it cannot have observed them and does not claim them.

An incoming connection's `address` is its **ephemeral source port**, which
nobody can dial. Unless the peer named a listening port in its BEP 10 handshake
(`p`), we do not advertise it at all: saying nothing beats saying something
useless.

Incoming messages are untrusted. Compact lists are length-checked (a truncated
list is refused whole, because believing its first six bytes would mean
inventing a peer), and `sanitize_incoming()` drops duplicate IPs — one host on
three ports is how a single address becomes three dial attempts — past a cap of
200 per message. A message we cannot parse costs nothing: BEP 11 permits
dropping a peer that egregiously violates the format, but a garbled `ut_pex`
says nothing about whether that peer can serve pieces.

Private torrents (BEP 27) do not participate at all: `ut_pex` is not offered, so
the extension bit is not even set, and a message that arrives anyway is ignored.
