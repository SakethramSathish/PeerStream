# Testing

## Principles

1. **Hermetic by default.** No test requires the public internet. Public-swarm tests are marked
   `@pytest.mark.network` and are excluded by default.
2. **Real sockets, controlled peers.** Where a test exercises the wire protocol, it uses real
   TCP/UDP sockets against in-repo mock peers — not mocks of our own parser. Mocking our own
   framing logic would only prove the mock is consistent with itself.
3. **Deterministic payloads.** `tools/make_test_torrent.py` generates data from a seeded PRNG so
   piece hashes are stable across machines.
4. **Assertions on bytes, not on progress bars.** Download tests compare `sha1(file_bytes)` with
   the source payload hash.

## Layout

```
tests/
├── bencode/       codec round-trips, malformed input, limits
├── core/          config, event bus, logging, peer-id, the composition root
├── torrent/       parsing, info-hash vectors, path traversal, magnet URIs
├── tracker/       HTTP/UDP announce against local doubles, retries
├── peer/          handshake, framing, bitfield, state, connections, peer manager
├── discovery/     KRPC, routing table, iterative lookups, magnet resolution
│                  (test_dht_live.py is @pytest.mark.network and off by default)
├── storage/       file mapping, allocation, verification, resume, security
├── download/      blocks, pieces, selection, scheduling, endgame, the manager,
│                  six real-socket integration tests and the CLI tool
├── upload/        choke policy, request validation, pacing, the upload manager
├── statistics/    rate windows, ring buffers, ETA
├── services/      engine wiring, session, app state (100 % covered)
├── cli/           the command line, including a real loopback download
├── ui/            view models, headless widget tests, screenshots,
│                  accessibility (contrast maths, palette, control names)
├── tools/         the verification tools, and packaging
├── mocks/         in-process tracker, peer and swarm fixtures
└── fixtures/      shared builders for payloads and metainfo

Two directories hold the loopback integration tests — `download/` (torrent →
tracker → peer → piece → verify → disk) and `upload/` (leecher → choke →
pacing → disk) — because they need a swarm, not a fixture.
```

## Fixtures (`tests/conftest.py`)

| Fixture | Provides |
|---|---|
| `event_loop` | per-test asyncio loop (pytest-asyncio, `asyncio_mode = auto`) |
| `tmp_download_dir` | isolated download root |
| `sample_torrent` | single-file torrent over a deterministic 4 MiB payload |
| `multi_file_torrent` | torrent whose pieces cross file boundaries on purpose |
| `mock_tracker` | running HTTP tracker on an ephemeral port (its own thread and event loop, so it also serves synchronous callers such as the CLI) |
| `tracker_server` | factory for tests that need more than one tracker |
| `mock_peer` / `mock_swarm` | real handshake-capable seeder(s) serving real pieces (`tests/mocks/mock_peer.py`: bitfield, wrong info hash, refusal, choking, `serve_requests` off, `send_have`) |
| `MockLeecher` | a real-socket leecher that connects to our listener, sends `interested`, requests blocks and keeps what it received (`tests/mocks/mock_leecher.py`: wrong info hash, silent, `valid` compares received blocks with the payload) |
| `engine_factory` | wired Engine with fake clock and throttled disk |

## Unit coverage map

| Subsystem | Must cover |
|---|---|
| Bencode | ints, strings, lists, dicts, nesting, empty containers, non-canonical keys, truncated input, depth limit, UTF-8 keys |
| Torrent | single/multi-file, `announce-list` tiers, `length` vs `files`, piece-hash count/size, info-hash vector, absolute/`..`/NUL paths rejected |
| Info hash | known-answer vector; hash is over the *re-bencoded* info dict bytes |
| Tracker | query encoding of raw 20-byte fields, compact IPv4/IPv6 and dictionary peers, failure reasons, missing/invalid `interval`, oversized bodies, peer-count caps, tier fallback, backoff |
| Messages | every message id, keep-alive, round-trip, oversized length prefix, unknown id, partial buffer, two messages in one read, 4-byte framing |
| Connections | handshake with the right/wrong info hash, refused, hang-up mid-handshake, silent peer, interested → unchoke, `have` → bitfield, keep-alive sent on schedule, idle peer dropped, oversized length prefix, idempotent close, callbacks on block/disconnect |
| Peer manager | dedup, self-connection skip, provenance, slot cap, concurrent dialling, failed candidate backoff, reaping on disconnect, `maintain()` refill, broadcast skipping dead sockets |
| Framing | partial reads across TCP segments, several messages in one write, oversized length refused before the payload is read, disconnect mid-frame, read deadlines |
| Bitfield | set/test/count, spare bits masked, malformed length, availability aggregation and rarest ordering |
| Bitfield | set/test/count, spare bits, availability aggregation, malformed length |
| Selection | sequential order, rarest-first ordering with ties, skip owned, skip unavailable, endgame duplication |
| Verification | correct hash accepted, corrupted rejected → `FAILED` → retried, last-piece short size |
| Storage | piece spanning two files, write at offsets, preallocation size, resume round-trip, corrupt resume file recovery |
| Verification | known SHA-1 vectors, one-bit change caught, hashing runs in a worker thread with bounded concurrency, batch order preserved |
| Statistics | idle decay to zero, window rollover and pruning, bounded buffer never grows (and reading a series does not create one), ETA with zero rate, ETA using the instant window until the longer one fills, byte counting from events, progress from verified pieces, a share ratio only when something was downloaded, a sampling loop that survives a failing source and stops when told |
| Choking | optimistic unchoke rotation, reciprocation to best uploaders, interest transitions, snubbed peers dropped, a departed peer's slot released at once |
| Upload | request validation by reason (bad index/offset/length, missing piece, not interested, choked, queue full, duplicate), round-robin serving, token-bucket pacing that schedules instead of blocking, cancel and disconnect, `have` only for verified pieces |
| Listener | inbound handshake accepted and adopted, wrong info hash refused, silent peer not adopted, slot cap refuses the extra peer, `stop()` cannot deadlock |
| Engine | wiring: block → download, request → upload, disconnect → both, verified piece → upload's `have`, cancel passed through; states derived from counters (idle → starting → downloading → seeding, pause/resume, stop then start again, a failed prepare reported as `ERROR`); `build_engine()` adopting progress from disk; a tracker answer fed back into the swarm; the tracker closed on `aclose()` |
| Session | add/get/remove (with and without deleting data), duplicate refused, hash case ignored, `stop_all`/`start_all`, totals summed across torrents, one failing torrent not stranding the others, closing twice |
| AppState | reduction order, a bounded event ring, per-type counts, listeners (including one that raises), detach, per-torrent filtering, snapshots taken from measurements at read time |
| CLI `download` | argument defaults, missing/broken torrents, tracker tiers from `--tracker`, the progress line, JSON summary, seeding window, Ctrl-C returning 130 with progress saved, and a real end-to-end run against a loopback swarm |
| DHT | KRPC encode/decode round-trips and the four query types, malformed and oversized packets, transaction matching; routing table ordering, bucket splitting, `K` eviction and questionable-node probing; iterative lookups narrowing to a target, token minting and validation; a four-node cluster bootstrapped over loopback |
| Magnet | stage order (trackers, then DHT, then metadata), stopping once sized, deduplication across sources, the metadata handshake against a mock peer, and the two-peer resolution that ends with a verified info hash |
| Interface | view models reducing snapshots, headless widget construction, the screenshot tool rendering a real swarm, and the two accessibility groups below |
| Accessibility | contrast arithmetic against known anchors (21:1, 1:1, symmetry, `#777777` on white at 4.48:1); every palette role measured against every surface it can sit on; every control on every page — and every detail tab, because one never shown has no children — given a name by Qt's accessibility layer |
| Extension protocol | BEP 10 handshake round-trips, an id of 0 meaning "not offered", an id past one byte dropped, an unknown extension kept rather than rejecting the handshake, a body over 64 KiB refused; and over real TCP: the reserved bit set only when we have something to offer, our version named, a garbled handshake costing the extensions but not the connection, an id we never advertised ignored, and a `ut_pex` message leaving with the *peer's* id (ours 1, theirs 5) |
| Peer exchange | compact encode/decode for IPv4 and IPv6, a truncated list refused whole, flags built only from measurements (seed, reachable — never encryption, uTP or holepunching), `sanitize_incoming` dropping ourselves and one host on three ports, the ledger's caps (200 first message, 50 after), one-message-a-minute batching, a flapping peer elided, a peer never mentioned never retracted, and a seeded 400-step sequence asserting no message ever names a contact in both lists or twice |
| DHT announcing | every way an announce honestly does not happen (no node, unbound node, nothing registered, no listening port, private torrent, a lookup that fails without ending the pass), the port read at announce time rather than at registration, a count that follows the measurement when a torrent stops listening, `go_quiet`, the loop repeating and being poked early, and a real four-node cluster where a stranger finds our TCP port |
| Packaging | the build config matched against the packages on disk, each package imported, every console-script entry point resolved to a callable, and `python -m cli.main --help` answering |

## Network tests

Tests that touch the public internet are marked `@pytest.mark.network` and
skipped unless pytest is run with `--network`. The default suite talks to
loopback only, so it stays green offline and in CI:

```bash
pytest                 # loopback only
pytest --network       # additionally, real trackers
```

Every test has a 120 s budget (`--timeout`, from `pytest-timeout`). This suite
opens sockets, and the worst thing a networking test can do is wait forever
without saying why: a timeout fails the test *and* prints a stack dump of every
thread, so a wedged peer or a stuck checkpoint shows where it was waiting.

`tests/tracker/test_public_tracker.py` announces, scrapes and provokes a
refusal against a live tracker. Unreachability degrades to a skip, never a
failure: no route to the tracker is not evidence of a broken client.

## Integration gate

The engine's definition of done is `tests/download/test_integration.py`:

```
payload → torrent → local tracker → 3 seeders → Engine → verified bytes on disk
```

Variants: single file, multi file, resume after hard stop, one seeder dying mid-transfer,
a seeder sending corrupt data (must be detected, penalised and re-fetched from another peer).

Three more suites run the same shape end to end against real sockets —
`tests/services/test_integration.py` (torrent → session → disk → resume in a
fresh session), `tests/upload/test_integration.py` (leecher walks away with
verified bytes), and `tests/statistics/test_integration.py` (counters and ETA
measured during a real transfer).

## Security regression tests

Kept in `tests/peer/test_security.py` and `tests/storage/test_security.py`:

* length prefix > 64 KiB → connection dropped
* piece index ≥ piece_count → dropped
* `begin + length` crossing piece boundary → dropped
* block length > configured cap → dropped
* request for a piece the peer never announced → dropped
* handshake with wrong info-hash or bad pstr → dropped
* file entry with `../` or absolute path → torrent rejected at parse time
* resume JSON with bad version/schema → quarantined, session starts clean

## Running

```bash
pytest -q                        # default: hermetic
pytest -m network -q             # public-swarm smoke tests (opt-in)
pytest -m "not slow" -q          # fast feedback
pytest tests/peer -q -k framing  # narrow
```
