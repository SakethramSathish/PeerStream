# Implementation Plan (internal working document)

> Source of truth: `Build_Your_Own_BitTorrent_PRD_TRD.md` (PRD §1–16, TRD §17–52)
> plus the "Master Implementation Prompt" supplied by the project owner.
> Where the two disagree, the master prompt wins (it is the later, more specific instruction).

---

## 1. Assessment of the specification

### Strengths
* The TRD's layered architecture (§18) and repository layout (§19) are sound and are adopted nearly verbatim.
* The three signature UI features (§36, §37, §38) are well-defined and genuinely differentiating.
* Testing requirements (§42) explicitly ask for mock infrastructure — this makes the project
  verifiable in a sandbox with no public swarm access. Critical, and treated as a first-class deliverable.
* The recommended build order (§50) is dependency-correct and is followed.

### Gaps / ambiguities found, and how they are resolved

| # | Gap or conflict | Resolution (assumption, stated explicitly) |
|---|---|---|
| A1 | TRD §17 lists `bencodepy`; master prompt §6/§10 requires a from-scratch bencode parser | **Own bencode implementation** in `app/bencode/`, zero dependencies. The point of the project is protocol understanding; a library would remove a whole learning unit. It also removes a supply-chain dependency. |
| A2 | TRD §17 lists no async HTTP client; HTTP trackers are required (FR-02) | **aiohttp** for HTTP/HTTPS tracker I/O (async-first, no event-loop blocking). UDP trackers and DHT use raw `asyncio.DatagramProtocol`. |
| A3 | TRD §19 puts `main.py` under `app/`; the UI package is `app/ui` | Kept as specified, but `app/core/application.py` owns composition so `app/main.py` is a 3-line entrypoint and `cli/main.py` can drive the same engine headlessly. |
| A4 | TRD §19 has no `services/` layer, yet §18 requires "Application Services" | Added `app/services/` (SessionService, TorrentService, AppState, Engine). This is the single API surface for UI and CLI; it is what keeps sockets out of widgets. |
| A5 | Endgame mode (§27) is described but not placed in the file tree | `app/download/endgame.py` + `app/download/scheduler.py` (request pipelining is a prerequisite for endgame and for §43 performance targets). |
| A6 | Statistics (§34) needs bounded history, which no file owns | `app/statistics/history.py` — ring buffers, not unbounded lists (memory target §43). |
| A7 | Config format (§40) is YAML, but no YAML dependency is listed | JSON-backed config (`~/.config/bittorrent-client/config.json`) with the same key structure as §40. No PyYAML dependency. Documented as a deviation. |
| A8 | PEX (§48) has no home in the tree | `app/discovery/pex.py`, used only after DHT lands (V2). |
| A9 | Peer "country" column (§10.6) would require a GeoIP database | Not implemented; the column is omitted rather than faked (§13 real-data rule). Peer client name is derived from the real peer-id (Azureus-style *-XXXX-* convention), which *is* real data. |
| A10 | §12 accessibility asks for reduced motion | `app/ui/animations/transitions.py` exposes a global motion guard honouring `QT_NO_ANIMATIONS` / a config flag. |
| A11 | MVP (§47) lists a CLI but the tree has none | `cli/main.py` — also the fastest way to prove the engine works before the UI exists. |
| A13 | Info-hash must match what other clients compute even for non-canonical files | Added `Decoder.decode_mapping_with_spans()`; the parser hashes the original `info` byte slice rather than a re-encoding (see `docs/protocol.md` §3). |
| A14 | Path sanitisation belongs at the metadata boundary, not only in storage | New module `app/torrent/path_safety.py`, reused by the storage layer in M6. |
| A12 | Resume (§30) shows JSON but does not say where | `data/state/<info_hash>.json`, written atomically (tmp + `os.replace`), validated on load, quarantine on corruption (§44). |

### Closed scope: transports this client will not grow

A client can spend its whole life absorbing more BEPs, and most of them are
about features whose absence a user feels only as a footnote. These are closed
deliberately, recorded here so that "not implemented" reads as a decision
rather than as something nobody got round to. Each is named where a user would
look for it — the DHT page, the peers tab, the settings — as absent, and no
subsystem pretends otherwise.

| Omission | BEP | Why it stays out |
|---|---|---|
| µTP, the LEDBAT congestion control | 29 | It replaces the transport: every socket in the codebase, every timeout, every rate limit is written against TCP. Bolting a userspace congestion controller onto that would be a second client, and the honest version of "we support µTP" is a rewrite, not a patch. |
| Message Stream Encryption (MSE/PE) | — | Encryption here would be theatre without an authenticated handshake, and real MSE exists to defeat traffic shaping, which is a deployment problem this client does not have. PEX's `0x01` bit and the `e` handshake field stay unset because we have nothing true to put in them. |
| Web seeding (`url-list`, `httpseeds`) | 19 | It is an HTTP range client beside the BitTorrent one — a second download engine with its own resume, verification and fairness story. Nothing in the piece pipeline is wrong without it; a swarm simply has one fewer source. |
| Local Service Discovery | 14 | Multicast on a LAN. Useful on a home network, and the one feature here that would make the client a nuisance on a shared one: every laptop on a café network announcing itself into everybody else's swarm. |
| UPnP / NAT-PMP port mapping | — | It asks the router to open a hole, which is a security decision that belongs to whoever owns the router, not to a download client deciding for them. Behind NAT this client downloads fine and accepts inbound connections only from peers it dialled first; the DHT page says `published` for exactly that reason. |
| BitTorrent v2 hashes | 52 | A different piece-hash tree (SHA-256, BEP 52) touches bencode, verification, storage and the magnet parser at once. v2 magnets are refused with a message naming the version we cannot fetch, which is the honest half of supporting them; the other half is a milestone of its own. |
| The peer "country" column | — | It needs a GeoIP database, i.e. a third-party dataset that would be stale the day it shipped. The column is omitted; the peer-id client name beside it is derived from bytes the peer actually sent (§13). |

### Hard rules carried through every phase
1. **Real data only.** No subsystem may report a value it did not measure. Unimplemented → `EmptyState` widget with an honest label (§13).
2. **No blocking calls in the event loop.** Disk I/O goes through a thread executor; hashing goes through a bounded worker (it is CPU-bound and would stall peers).
3. **Untrusted input.** Every peer-supplied length, index and offset is validated before use; piece data is never written before SHA-1 verification.
4. **Tests ship with the phase that introduces the behaviour**, not later.

---

## 2. Milestone breakdown

Each milestone = one logical commit group = one review unit.
Entry criteria: previous milestone's tests pass. Exit criteria: new tests pass + docs updated.

| M | Milestone | Deliverables | Tests |
|---|---|---|---|
| **M0** | Foundation ✅ | `core/config.py`, `core/event_bus.py`, `core/events.py`, `core/logging_setup.py`, `core/peer_id.py`, `core/application.py`, `cli/main.py` (`info`) | config validation/round-trip, bus fan-out and error isolation, log forwarding, peer-id convention, CLI info command |
| **M1** | Bencode ✅ | `decoder.py`, `encoder.py`, `errors.py` | round-trips, malformed input, depth/size limits, fuzz-lite |
| **M2** | Torrent metadata ✅ | `metadata.py`, `parser.py`, `info_hash.py`, `path_safety.py`, `errors.py`, `tools/make_test_torrent.py` | parse fixtures, info-hash vectors, path-traversal rejection |
| **M3** | HTTP tracker ✅ | `base.py`, `http_tracker.py`, `manager.py`, `errors.py`, `tools/mock_tracker.py`, `cli/main.py` (`announce`) | mock HTTP tracker on real sockets, compact IPv4/IPv6 + dictionary peer models, percent-encoded byte fields, tier fallback, timeouts, backoff, response limits, `pytest --network` run against a live public tracker |
| **M4** | Peer wire protocol ✅ | `errors.py`, `handshake.py`, `messages.py`, `protocol.py`, `bitfield.py`, `state.py`, `tools/peer_probe.py` | encode/decode vectors for every message, self-validating ranges, partial reads over real TCP, several messages in one segment, oversized length prefixes, malformed bitfields, state transitions, availability aggregation, `pytest --network` handshakes with real peers on the internet |
| **M5** | Peer connections ✅ | `peer/connection.py`, `peer/discovery/peer_manager.py`, `tests/mocks/mock_peer.py`, `tools/peer_probe.py --swarm` | real TCP against `MockPeer` on real sockets: handshake (correct/wrong info hash/refused/hang-up/silent), interested → unchoke, `have` → bitfield, keep-alives, idle timeout, oversized length prefix, peer hang-up, slot caps, concurrent dialling, candidate backoff and reaping, deduplication, self-connection skip, `--network` probe of a live public swarm |
| **M6** | Storage ✅ | `storage/files.py`, `storage/manager.py`, `storage/verify.py`, `storage/resume.py`, `tools/storage_check.py` | pieces spanning two files, padding files, preallocation (and not shrinking existing data), `pwrite` at offsets, verify-before-write rejecting a flipped bit, resume round-trip, corrupt resume quarantine, path escape/symlink/duplicate-path rejection, hashing off the event loop with bounded workers, end-to-end store-and-verify of a real 64 MiB payload |
| **M7** | Download engine ✅ | `download/block.py`, `download/piece.py`, `download/selector.py`, `download/endgame.py`, `download/scheduler.py`, `download/manager.py`, `tools/swarm_download.py` | block geometry and piece state machine, rarest-first/sequential/random selection, pipelined per-peer planning, interest management, request expiry, endgame racing (only for requests that have gone unanswered), hash-before-write, corrupt-piece rejection with peer penalties, resume-aware start, six real-socket integration tests (whole torrent byte-for-byte, pieces straddling four files, seeder dying mid-download, resume, a lying seeder that never poisons the disk), end-to-end 8 MiB swarm download at 12.6 MiB/s |
| **M8** | Upload + choking ✅ | `upload/manager.py`, `upload/choke.py`, `upload/rate.py`, `peer/discovery/listener.py`, `tools/upload_check.py` | inbound handshakes (info hash checked before we answer, wrong torrent and silent peers refused, slot caps), request validation by reason (bad index/offset/length, missing piece, not interested, choked, queue full, duplicate), per-peer queues with round-robin serving, token-bucket rate limiting that schedules instead of blocking, tit-for-tat slots ranked by bytes received plus a rotating optimistic slot, snubbed peers dropped, `have` broadcast on verified pieces, cancel and disconnect handling, seven real-socket integration tests (leecher walks away with the bytes, choked peer gets nothing, hostile requests refused, wrong info hash refused at the door, queue limit), ten listener tests (adoption, concurrency, capacity, no deadlock on stop), end-to-end 4 MiB seed to three leechers with every served block verified against the payload |
| **M9** | Statistics + events ✅ | `statistics/speed.py`, `statistics/history.py`, `statistics/metrics.py`, `tools/metrics_check.py` | rolling-window rate meters (1 s / 5 s / 30 s / session) that decay to zero when idle, byte counting from `PIECE_BLOCK_RECEIVED` and `PIECE_UPLOADED` on the event bus, bounded ring buffers that never grow past their capacity, `MetricsSnapshot` with progress, peers, waste, queue depth, share ratio and an ETA that is `None` without a measured rate, `STATS_SAMPLE` events for the UI, 133 statistics tests including seven real-socket integration tests (bytes counted by two independent counters, ETA falling as the torrent fills, an idle meter decaying to zero, seeding back to real leechers), end-to-end 8 MiB download at 12.2 MiB/s with every sample verified |
| **M10** | Engine + services + CLI ✅ | `services/engine.py`, `services/session.py`, `services/torrent_service.py`, `services/app_state.py`, `cli/main.py` (`download`), `core/application.py` | `Engine` wires storage/peers/download/upload/metrics/tracker/listener and owns the callbacks that belong to more than one engine; `build_engine()` adopts resume state before the download engine is built; `Session` owns torrents, `TorrentService` wraps one with an idempotent start/pause/resume/remove, `AppState` reduces the bus into snapshots; CLI `download` with a measured progress line, seeding and Ctrl-C resume. **Full local integration test**: mock tracker + 3 mock seeders → download → verify → assemble → byte-for-byte hash check → tear the session down → resume in a fresh one. 100 % coverage of `app/services` (609 stmts); two real bugs found (10 s dial delay, unreachable `STARTING`) |
| **M11** | UI shell ✅ | `app/ui/app.py`, `main_window.py`, design system (`theme/`), navigation | headless smoke test (offscreen platform) |
| **M12** | Library + detail tabs ✅ | models, view models, overview/peers/trackers/files/logs tabs | view-model tests with fake services |
| **M13** | Signature visuals ✅ | `charts/swarm_canvas.py`, `charts/piece_matrix.py`, `charts/rate_graph.py`, `widgets/peer_inspector.py` | data-mapping tests (no fake animation) — built alongside M12, since the tabs are where they live, then polished: one five-minute window stated once in the design tokens and read by every chart, gaps drawn as gaps, real per-piece sizes, and click-to-inspect |
| **M14** | UDP tracker ✅ | `udp_tracker.py` (BEP 15), `factory.py`, `tools/mock_udp_tracker.py`, and a fix the screenshots exposed: `bridge.py` / `logs_vm.py` batching | 52 tests: byte-level framing, parsing and error handling, plus a real conversation with a local UDP tracker double (handshake, connection-id reuse and expiry, transaction-id filtering, retries, timeouts, scrape batching) |
| **M15** | Magnet + DHT ✅ | `torrent/magnet.py`, `discovery/dht/{krpc,routing,protocol,node}.py` (BEP 5), `discovery/magnet_resolver.py`, `peer/metadata_exchange.py` (BEP 9/10), `ui/views/dht_view.py` | magnet parsing (hex + base32, v2 refused with a reason), KRPC codec on recorded bytes incl. 12 malformed packets, routing-table tests (bucket split, questionable-contact replacement, `closest()` ordering), a real 4-node DHT cluster on loopback UDP (bootstrap, announce→find, silent and nonsense peers, token refusal), 38 metadata-exchange tests against mock peers that serve, reject, short-change and lie, 15 resolver tests with the sources injected, and `tests/services/test_magnet_session.py`: magnet → metadata from one peer → payload from another → byte-for-byte file on disk. `pytest --network` against the live DHT: bootstrap reached the public routers, a `get_peers` walk contacted 31 nodes and returned 354 peers, and the Debian netinst magnet resolved to 791 674 880 bytes in 3020 pieces, verified against its info hash |
| **M16** | Polish ✅ | `ui/theme/contrast.py` (the WCAG 2.1 arithmetic), nine palette roles retuned by measurement, accessible names and `setBuddy` wiring across `ui/views/` and `charts/piece_matrix.py`, `tests/ui/test_accessibility.py`, `tests/tools/test_packaging.py`, `pyproject.toml` (packages *found*, not listed) | 66 accessibility tests: contrast checked against known anchors (21:1, 1:1, symmetry, `#777777` on white at 4.48:1), every text role measured against all four surfaces at 4.5:1, nine fill roles at the 3:1 non-text threshold, and a walk of all six pages and all six detail tabs asking Qt's accessibility layer what it would announce for every control — a tab never shown has no children to inspect. Nine roles failed the first measurement; the worst was the dark theme's *missing piece* cell at 1.23:1, drawn in a colour indistinguishable from the card behind it, and a tooltip was ruled not to count as a name. 27 packaging tests: the build config compared with the packages on disk (the old fixed list shipped `app/` and none of its subpackages, so the wheel imported and immediately failed), every package imported, every console-script entry point resolved to a callable, and `python -m cli.main --help` answering; the built wheel installs and `bittorrent info` reads a real torrent. Both themes re-photographed against a local swarm and a four-node DHT. Docs: a discovery-layer section, an accessibility section, the layout and the stale integration path in `docs/testing.md`, the pre-M15 wording in `empty_state.py`. **2295 passed, 10 skipped** |

| **M17** | Discovery round-out ✅ | `discovery/dht_announcer.py`, `discovery/pex.py` (BEP 11), `peer/extension.py` (BEP 10), extension negotiation in `peer/connection.py`, `exchange_peers()` in `peer/discovery/peer_manager.py` | The client publishes itself. `announce_peer` existed since M15 and nothing called it, so a trackerless download could find peers and could never be found; `DhtAnnouncer` runs it per torrent on BEP 5's fifteen minutes, publishing the TCP port we actually listen on with `implied_port=0` (the implied port is our DHT socket, which accepts no peer connections), skipping a torrent that is not listening and refusing a private one (BEP 27). 35 announcer tests, including one against a real four-node loopback cluster where a node that never met us finds the address we published — and fails when the implied-port flag is flipped, which is the proof the assertion means something. BEP 10 moved into its own module because the ids belong to the *receiver*: a message we send carries the peer's id, one we read carries ours, and a client can encode and decode perfectly while never being understood. 30 extension tests plus 13 over real TCP with the two sides deliberately numbered 1 and 5. PEX: 47 tests — compact codec for both families, the two flags we can measure (seed from the peer's bitfield, reachable because we dialled it), the ledger's caps and one-message-a-minute batching, a seeded 400-step sequence asserting no contact is ever in both lists or advertised twice, and 14 manager tests over real sockets. Two bugs found while wiring: `build_engine` never passed `our_port` to the peer manager, so `_is_self` could not fire and a local swarm could hand us our own listener to dial; and `app/discovery/__init__.py`'s `PeerManager` re-export made the package unimportable from the peer layer the moment PEX needed BEP 10. The DHT page grew a fifth card, `published`, counting torrents a node actually accepted an announce for |
| **M18** | Repository hygiene ✅ | `LICENSE`, `CHANGELOG.md`, `.github/workflows/ci.yml`, `app/storage/files.py` (`_OPEN_WRITE`/`_OPEN_READ`, `_write_at`, `_read_at`), `tests/storage/test_portability.py`, `pyproject.toml` (classifiers), `tests/tools/test_packaging.py` | The repository stopped contradicting itself. `pyproject.toml` declared `license = { text = "MIT" }`, setuptools repeated that into the wheel's metadata, and there was no licence text anywhere in the tree: anyone who installed it was granted a licence whose terms did not ship. `LICENSE` is that text, and `tests/tools/test_packaging.py` now fails if it goes missing, if it stops naming the declared licence, or if it still holds the template's `<year>` and `<copyright holders>` blanks. `CHANGELOG.md` records 0.1.0 against the milestones that produced it and `v0.1.0` is tagged. The three gates became CI rather than a habit: `gates` bootstraps with `scripts/setup.sh`, so CI cannot drift from the setup a contributor is told to run, then runs ruff, mypy and the suite under a 90% coverage floor (the suite sits at 93%, so the floor is a regression net and not a ratchet); `package` builds the wheel and installs it somewhere clean, because an editable install hides a broken wheel; `windows-storage` runs the portability tests on the platform they were written for. Two absences are deliberate and recorded in `docs/development.md`: no `ruff format --check`, because the formatter and the lint rules disagree about seven pre-existing files and a gate that fails on arrival teaches people to ignore gates; and the ten `network` tests stay behind `--network`, reachable from the workflow's dispatch form, because a public swarm is not a reproducible fixture. The storage layer stopped assuming POSIX: `os.pwrite`, `os.pread` and `os.O_CLOEXEC` were referenced unconditionally, so `write_chunk` would have raised `AttributeError` on Windows before a single byte reached the disk — while the `posix_fallocate` call twenty lines above them was already `hasattr`-guarded, which made the omission look like an oversight rather than a decision. The flags now compose through `getattr` and the positional IO falls back to a seek on the call's own descriptor, which is safe for the reason `pwrite` was chosen: every `write_chunk` owns its fd, so the position it moves is nobody else's. 13 tests run that fallback by deleting the attributes from `os` — as close to Windows as a Linux runner gets — including one that re-imports the module in a subprocess with all four already gone, which is the check that fails if a bare `os.O_CLOEXEC` ever returns, since it would raise at import time before any patch could apply, and one that holds the no-interleaving property with eight threads over eight regions. The milestone's last gate run then failed a chart test that had passed twenty minutes earlier, and the cause was a bug rather than a flaky test: `SeriesBuffer` and `Throttle` both compared a difference of two `time.monotonic()` readings against a fixed interval, and monotonic grows with uptime, so for one base in sixty an on-time sample landed a rounding error inside the minimum gap and was dropped — a five-minute window spanning five minutes and one second — while the 50 ms frame budget refused an on-time repaint for two bases in three. Both now take `INTERVAL_TOLERANCE_SECONDS` of slack; three tests pin bases that were verified to break, and 1500 random bases now hold the promised 299-second window where before about twenty-five of them did not . Running the documented quick start afterwards exposed a third: `load_resume(verify=True)` verified the pieces it adopted and then returned the state it had loaded *before* verifying, so a resume file whose download directory was gone printed `resumed 512 piece(s) from disk` above a download that fetched all 512 from scratch — the number was the file's claim, not a measurement, which rule 1 below forbids |

---

## 3. Cross-cutting design decisions

**Concurrency model.** One `asyncio` event loop. Long-lived tasks per torrent:
`tracker_task`, `peer_acceptor`, `peer_connections (N)`, `scheduler`, `stats_task`, `state_persist_task`.
CPU-bound SHA-1 verification runs in a `ThreadPoolExecutor` with a bounded queue (a 4 MiB piece hash
is ~10 ms; at 50 peers that would otherwise cost 500 ms of loop stall per second).

**Event flow.** Engine → `EventBus` (async, type-annotated) → `AppState` reducer → Qt signal (queued,
marshalled to the GUI thread) → view model → widget repaint. The UI never holds engine objects.
The Qt↔asyncio bridge is a single `QAbstractEventDispatcher`-friendly pump: engine callbacks post
through `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`.

**Backpressure.** Download never grows unbounded: the scheduler keeps at most
`max_outstanding_requests_per_peer` (default 16) in flight; incomplete pieces are held in a bounded
in-memory assembly cache and flushed to disk on verification; un-verified data is *never* written to
the final file (only to a scratch region when a piece must survive a restart — off by default).

**Piece state machine** (TRD §21): `MISSING → REQUESTED → DOWNLOADING → DOWNLOADED → VERIFYING → VERIFIED`,
with `FAILED` reachable from `VERIFYING` and returning to `MISSING` via a retry with peer-penalty.

**Security posture** (§41): max frame 64 KiB + block size cap (default 32 KiB, hard limit 128 KiB),
piece index bounds-checked against `piece_count`, `begin + length <= piece_size`,
file paths sanitised (reject absolute, `..`, NUL, reserved names), resume JSON schema-validated.

**Observability** (§45): every subsystem emits typed events; the counters listed in §45 are gathered by
`statistics/metrics.py` into one immutable snapshot per tick so the UI renders a consistent frame.

---

## 4. Verification strategy (no public swarm required)

```
tools/make_test_torrent.py   → deterministic payload + .torrent
tools/mock_tracker.py        → real HTTP tracker on 127.0.0.1
tools/mock_seeder.py         → real BitTorrent seeder (handshake + bitfield + piece)
tests/mocks/*                → in-process equivalents for pytest
```

Milestone M10's integration test is the "definition of done" gate for the engine:
create payload → create torrent → start tracker → start 3 seeders → download through the real
engine → assert bytes on disk hash-match the source payload → resume from a torn-down session.

UI is verified headlessly: `QT_QPA_PLATFORM=offscreen` render → PNG → visual review (M11–M13).
