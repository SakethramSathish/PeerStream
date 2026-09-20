# Changelog

Everything notable about this client, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the versioning is
[semantic](https://semver.org/spec/v2.0.0.html), with the caveat that a 0.x
release promises nothing about the next one.

Milestone references (`M7`) point at
[`docs/implementation-plan.md`](docs/implementation-plan.md), which records what
each one was required to prove before the next could start.

## 0.1.0 - 2026-09-18

The first tagged release: a working BitTorrent client, built from the protocol
up, with a real-time observability UI. It downloads and seeds real torrents
against real peers, resolves magnets through the DHT, and publishes itself into
it.

### Added

**Protocol.**

- Hand-written bencode encoder/decoder with depth and size limits, rather than a
  dependency (M1).
- `.torrent` parsing, info-hash computation, and path-traversal rejection at the
  metadata boundary (M2).
- HTTP/HTTPS tracker announces and scrapes over `aiohttp`, with tier fallback,
  backoff, and both compact (IPv4/IPv6) and dictionary peer models (M3).
- UDP tracker protocol, BEP 15: connection ids, expiry, retry, and byte-level
  framing (M14).
- Peer wire protocol: every message encoded and decoded, self-validating ranges,
  partial reads over real TCP, several messages in one segment (M4).
- Peer connections and swarm management: handshakes, interest, choking,
  keep-alives, idle timeouts (M5).
- Download engine: block geometry, rarest-first / sequential / random piece
  selection, pipelined per-peer planning, endgame racing for requests that have
  gone unanswered, hash-before-write (M7).
- Upload engine: inbound handshakes with the info hash checked before answering,
  per-peer queues with round-robin serving, token-bucket rate limiting that
  schedules instead of blocking, tit-for-tat choke slots plus a rotating
  optimistic one (M8).
- Magnet links, BEP 3: hex and base32 info hashes, `x.pe` peers, `tr` trackers.
  v2 magnets (BEP 52) are refused with a message naming the version that cannot
  be fetched (M15).
- DHT, BEP 5: KRPC codec, a k-bucket routing table, `ping` / `find_node` /
  `get_peers` / `announce_peer` (M15).
- Metadata exchange, BEP 9 over the BEP 10 extension protocol: fetch an info
  dictionary from a peer and verify it against the magnet's hash (M15).
- Extension negotiation, BEP 10, with our ids and theirs kept apart by name
  (M17).
- Peer exchange, BEP 11: advertise only connected peers, at most one message per
  peer per minute, capped at 50 added and 50 dropped after a 200-entry first
  message, private torrents excluded (M17).
- DHT announcing: the client publishes itself, with a token per node, a
  900-second interval, and BEP 5's two-minute pre-shutdown silence (M17).

**Storage.**

- Files as windows onto one continuous byte stream, so a piece spanning two
  files is a range with two spans rather than a special case; preallocation with
  a truncate fallback; verify-before-write; corrupt-piece rejection with peer
  penalties; resume state (M6).
- Positional IO falls back to a seek on the call's own descriptor where
  `os.pwrite` does not exist, and the open flags degrade where `O_CLOEXEC` does
  not (M18).

**Statistics and events.**

- Rolling-window rate meters (1 s / 5 s / 30 s / session) that decay to zero when
  idle, bounded ring buffers, and a metrics snapshot with progress, peers, waste,
  queue depth, share ratio and an ETA that is `None` without a measured rate
  (M9).
- A typed async event bus, reduced into immutable snapshots for the UI (M9, M10).

**Interfaces.**

- CLI: `bittorrent info`, `announce`, and `download` with a measured progress
  line, seeding, and Ctrl-C resume (M0, M3, M10).
- PySide6 desktop UI: library, torrent detail with overview / peers / trackers /
  files / logs tabs, a DHT page, a command centre, and settings (M11, M12).
- Signature visuals: a swarm canvas, a piece matrix, and a rate graph, all driven
  by measured data on one window stated once in the design tokens (M13).
- Accessibility: contrast measured with the WCAG 2.1 relative-luminance
  arithmetic rather than judged, nine palette roles retuned by it, and an
  accessible name for every control (M16).

**Repository.**

- `LICENSE`: the MIT text `pyproject.toml` had been declaring since the first
  commit, now shipped beside the declaration and inside the wheel (M18).
- `CHANGELOG.md`, and the `v0.1.0` tag it describes (M18).
- Continuous integration: the three gates on every push, bootstrapped by
  `scripts/setup.sh` so CI cannot drift from the documented setup; a job that
  builds the wheel and installs it somewhere clean; and a job that runs the
  storage portability tests on Windows (M18).

**Tooling.**

- Local doubles that speak the real protocols over real sockets: a mock tracker
  (HTTP and UDP), a mock seeder, and a mock peer that negotiates BEP 10 and
  exchanges BEP 11 messages.
- End-to-end checks with measured numbers: `swarm_download`, `upload_check`,
  `storage_check`, `metrics_check`, `magnet_check`, `peer_probe`.
- `make_test_torrent` for fixtures and `screenshot` for headless renders of every
  page in both themes.

### Fixed

- `load_resume(verify=True)` re-verified the pieces it adopted, dropped the ones
  that failed, and then returned the state object it had loaded *before*
  verifying. The count taken from that object is the count the CLI and the UI
  show, so a resume file whose download directory had been deleted produced
  `resumed 512 piece(s) from disk` above a download that then fetched all 512
  from scratch. The returned state now carries the pieces that survived, and a
  discarded claim is logged (M18).
- The chart throttles treated an interval as a knife edge rather than a floor.
  Both compared a difference of two `time.monotonic()` readings against a fixed
  interval, and monotonic grows with uptime, so for roughly one base in sixty an
  on-time sample landed a rounding error inside the minimum gap and was dropped —
  leaving a five-minute window spanning five minutes and one second. The repaint
  throttle was worse and quieter: for about two bases in three, a frame requested
  exactly one 50 ms budget later was refused. Which machines were affected
  depended on how long they had been up (M18).
- `build_engine` never passed our listening port to the peer manager, so it could
  not recognise itself and risked dialling itself in a local swarm (M17).
- An announcing pass that skipped a torrent for want of a port then waited the
  full interval before trying again; skipped torrents are retried at 30 seconds
  (M17).
- Two one-line files were described in the documentation as modules
  (M17).
- Log and event batching in the UI bridge, found by the screenshots rather than
  by the tests (M14).

### Documentation

- [`docs/protocol.md`](docs/protocol.md) — what each BEP requires and what this
  client does about it.
- [`docs/architecture.md`](docs/architecture.md) — the module map and the event
  flow.
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — the milestone
  plan, including a closed-scope table naming the transports this client will not
  grow (µTP, MSE/PE, web seeding, LSD, UPnP/NAT-PMP, BitTorrent v2) and why.
- [`docs/testing.md`](docs/testing.md) and
  [`docs/development.md`](docs/development.md).

Tagged `v0.1.0`. There is no remote yet, so there are no compare links to put
here; when one is added, link each version header to the tag range it covers.
