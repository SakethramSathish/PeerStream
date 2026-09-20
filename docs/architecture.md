# Architecture

## Layers

```
┌───────────────────────────────────────────────────────────────┐
│ Presentation        app/ui/**  (PySide6, MVVM)                │
│   views → viewmodels → models → widgets/charts                │
├───────────────────────────────────────────────────────────────┤
│ Application         app/services/**                           │
│   Session · TorrentService · AppState · Engine                │
├───────────────────────────────────────────────────────────────┤
│ Domain              torrent · download · upload · peer · dht   │
│   Torrent · Piece · Block · PeerState · ChokePolicy           │
├───────────────────────────────────────────────────────────────┤
│ Infrastructure      bencode · tracker · storage · statistics  │
│   TCP/UDP/HTTP · disk IO · resume state · rate meters         │
└───────────────────────────────────────────────────────────────┘
```

Rules:
* **UI never touches a socket, a tracker, or the disk.** It calls `Session`/`TorrentService`
  and observes `AppState` + `EventBus`.
* **Infrastructure never imports `app.ui`.**
* **Domain never imports `app.services`.** Data flows up, commands flow down.
* Every subsystem takes its dependencies as constructor arguments (no module-level singletons,
  no service locators) so tests can substitute doubles.

## Runtime object graph (one torrent)

```
TorrentService
 └── Engine                      (owns the tasks below; emits events)
      ├── TrackerManager         announce loops, tier fallback, health
      │     ├── HTTPTracker
      │     └── UDPTracker
      ├── PeerManager            candidate vetting, dedupe, connection budget
      │     └── PeerConnection ×N   handshake → bitfield → message loop
      ├── DownloadManager
      │     ├── PieceSelector    sequential | rarest-first | random
      │     ├── RequestScheduler pipelining, timeouts, endgame
      │     └── Piece / Block    state machines
      ├── UploadManager + ChokePolicy
      ├── StorageManager         byte-stream ⇄ files, preallocation
      │     └── ResumeStore      atomic JSON state
      └── MetricsCollector       rate meters + ring buffers
```

## Foundation services (`app/core`, milestone M0)

| Module | Responsibility |
|---|---|
| `config.py` | Typed dataclass configuration tree; JSON-backed, unknown keys rejected, corrupt files fall back to defaults |
| `events.py` | `Event`, `EventType`, `EventCategory` — the vocabulary every subsystem publishes in |
| `event_bus.py` | Async fan-out with per-handler error isolation and non-blocking emission |
| `logging_setup.py` | Console/rotating-file/JSON handlers plus a handler that forwards records into the event bus |
| `peer_id.py` | Azureus-style peer-id generation and peer client identification |
| `application.py` | Composition root: loads config, wires logging to the bus, owns lifecycle |

### Tracker layer (`app/tracker`, milestone M3)

| Module | Responsibility |
|---|---|
| `errors.py` | `TrackerError` family, split so transient (connection/timeout) and permanent (unsupported) failures can be caught separately |
| `base.py` | `AnnounceRequest`/`AnnounceResponse`/`PeerAddress` models, query encoding, `Tracker` interface, `TrackerStatus` health record |
| `http_tracker.py` | HTTP/HTTPS announce and scrape over `aiohttp`; pure-function response parsing that validates everything the tracker sends |
| `manager.py` | Tiers, first-success-wins announce, interval clamping, exponential backoff, periodic announcing, per-tracker health for the UI |

The peer manager (M5) consumes `PeerAddress` values only — it never learns
which tracker, DHT node or PEX exchange produced them, though it records the
provenance string for the swarm view.

### Peer protocol layer (`app/peer`, milestone M4)

| Module | Responsibility |
|---|---|
| `errors.py` | Failure taxonomy: disconnect and timeout are recoverable, protocol and message errors mark the peer down |
| `handshake.py` | The 68-byte handshake: encode, decode, and validate protocol string, info hash and extension flags |
| `messages.py` | Immutable message objects that validate on construction; frame encode/decode |
| `protocol.py` | `PeerStream`: length-prefixed framing over `asyncio` streams, deadlines, byte counters |
| `bitfield.py` | Peer piece availability, plus `PieceAvailability` counts for rarest-first selection |
| `state.py` | `PeerSession`: choke/interest state, bitfield, counters, and message → event mapping |
| `extension.py` | BEP 10: the id-0 handshake, and the two id maps that must not be confused — `our_id()` for messages arriving, `their_id()` for messages leaving. `ExtensionState` is per connection and knows whether the peer has answered yet |

### Peer connection layer (`app/peer`, milestone M5)

| Module | Responsibility |
|---|---|
| `connection.py` | `PeerConnection`: one socket, one peer — connect, handshake, read loop, keep-alives, idle timeout, teardown. `SwarmContext` carries the per-torrent facts (info hash, piece count, piece length) every connection needs |
| `discovery/peer_manager.py` | `PeerManager`: candidate pool, slot cap, concurrent connect attempts, per-peer backoff, reaping dead sockets, broadcast, `maintain()` loop. `PeerCandidate` remembers provenance, failures and retry times. `exchange_peers()` runs the BEP 11 side of that loop: one ledger per connection, contacts taken from the connections that exist right now |
| `discovery/listener.py` | `PeerListener`: the inbound side — binds a port, checks the info hash before answering, applies the slot cap, and adopts accepted sockets through `PeerManager.adopt()` |

Design decisions that matter:

* **The read deadline is `idle_timeout`, not a per-message timeout.** A peer
  that sends nothing for 150 s is gone; a peer that sends keep-alives every 90 s
  is alive even if it has no data for us.
* **`fill()` dials concurrently.** Serial dialling at a 10 s timeout would take
  minutes to work through 50 tracker peers.
* **Backoff is exponential and capped:** `reconnect_delay · 2^(failures-1)`,
  candidates dropped after `max_peer_failures`. A peer that completed a
  handshake and then hung up gets its failure count cleared but is still held
  off for `reconnect_delay` — we do not redial someone who just said goodbye.
* **Piece policy is not here.** The connection layer reports blocks and
  disconnects through callbacks (`on_block`, `on_disconnect`); the download
  engine (M7) decides what to request.

The layering is deliberate: `messages.py` knows nothing about torrents (so a
`have` for piece 9,999 is not inherently invalid), and `state.py` performs the
per-torrent range checks because only it knows the piece count. Socket failures
never escape as `OSError`; they arrive as typed errors the connection layer can
react to.

### Discovery layer (`app/discovery`, milestones M15 and M17)

How a torrent learns where its peers are when the tracker list is empty, the
trackers are dead, or there is no `.torrent` at all — only a magnet link. M15
built the asking half; M17 built the answering half, because a client that can
find a trackerless swarm but cannot be found by one is half a participant.

| Module | Responsibility |
|---|---|
| `dht/krpc.py` | BEP 5 KRPC: the four queries (`ping`, `find_node`, `get_peers`, `announce_peer`) and their replies, bencoded into UDP, matched by transaction id |
| `dht/routing.py` | The routing table: 160 k-buckets of `K` live nodes each, ordered closest-first to a target, with questionable-node probing and a 15-minute refresh |
| `dht/protocol.py` | One socket, many concurrent conversations: retries with backoff, a bound on outstanding transactions, and a strict 1500-byte packet ceiling |
| `dht/node.py` | `DhtNode` — bootstrap against the public routers, iterative lookups (`α` queries in parallel, bounded rounds), peer storage with a 30-minute TTL, and the tokens an `announce_peer` must carry |
| `dht_announcer.py` | The other direction of BEP 5: `announce_peer` on a fifteen-minute loop, per torrent, publishing the TCP port we actually listen on. Skips what it cannot honestly publish — a torrent that is not listening, a private one |
| `magnet_resolver.py` | BEP 9 end to end, cheapest source first: trackers named in the magnet, then a DHT `get_peers` walk, then metadata handshakes over whichever peers answered. It stops as soon as it has a size and a piece count. |
| `pex.py` | BEP 11 peer exchange: the compact `ut_pex` codec, the flags we can actually measure, and `PexLedger` — the per-peer memory of who has been told what, which is what makes "one message a minute, no contact in both lists" true |

A magnet resolves in **stages**, and each stage is worth trying on its own
because any one of them may be the only one that works: the trackers in the
`tr` parameter first (one HTTP request, no DHT needed), then the DHT, then
`ut_metadata` over whatever peers either produced. The resolver returns the
sources it actually used, and the UI says which they were rather than claiming
"tracker" by default.

Discovery runs in **two directions**, and only one of them is obvious. Asking a
tracker or walking the DHT finds peers; `announce_peer` and `ut_pex` are how the
swarm finds us back. Both are wired through the same `PeerManager`, which is why
every address it receives carries a `source` string rather than a shape specific
to one protocol — the peers tab's "found via" column reads that string, so
provenance in the UI is the same fact the protocol recorded.

Two measurements came out of building it against the live network:

* **A peer answers `ut_metadata` with the id *we* advertised, not the id it
  advertised.** qBittorrent 5.1/5.2 say `ut_metadata=2` in their handshake and
  then reply `Extended(id=1, …)` — id 1 being ours. Matching only the peer's
  handshake id makes every fetch time out against a peer that answered, and a
  mock peer is too polite to reveal it; the request is now matched against both.
* **A peer may ask *us* for metadata in the middle of our own fetch.** Serving
  a `reject` for a torrent we do not have keeps the connection readable.

### Download layer (`app/download`, milestone M7)

| Module | Responsibility |
|---|---|
| `block.py` | `Block` / `BlockKey` / `BlockState`: the unit of transfer. `plan_blocks()` splits a piece, `block_at()` maps an offset back to its block |
| `piece.py` | `Piece`: one piece's buffer, per-block state, requesters and sources. `MISSING → DOWNLOADING → VERIFYING → VERIFIED`, plus `FAILED → reset()` |
| `selector.py` | `PieceSelector`: the order pieces are started in — rarest-first (default), sequential, or random-first-piece |
| `endgame.py` | `EndgameTracker`: who is fetching which block, and whether a second peer may have it too |
| `scheduler.py` | `Scheduler`: decides *what* to request, *from whom*, and *when to give up* — pipeline depth, in-progress pieces first, availability, expiry, cancels |
| `manager.py` | `DownloadManager`: the engine. Wires peers → scheduler → storage, verifies, retries, emits events, keeps honest statistics |

Design decisions that matter:

* **The scheduler plans, the manager acts.** `Scheduler.plan()` returns
  `Assignment`s; it never touches a socket. `DownloadManager` sends them, which
  is why the scheduler is testable without a network and the manager is
  testable without a scheduler.
* **Endgame watches *unclaimed* blocks, not missing ones.** What matters is not
  how much is left but how much *unassigned* work is left: once peers are about
  to run out of things to do, racing beats queueing. The count is maintained
  incrementally because it is consulted for every planned request.
* **Endgame races stale requests only.** A block is only requested from a
  second peer after the first request has gone unanswered for
  `download.endgame_delay` (1 s). On a healthy swarm that costs nothing; with
  one slow seeder it finished the same 4 MiB download 2.4× sooner for 4% waste.
* **Interest is the engine's job.** A peer only unchokes a client that has said
  `interested`, so the manager states (and withdraws) interest as the wanted
  set changes — and caches the decision per peer, keyed on how many pieces that
  peer holds and how many we have verified.
* **Finishing a piece cancels its races.** The moment a piece is complete,
  every remaining request for it is cancelled; when a piece fails its hash, it
  is reset, the peers who supplied it are penalised, and nothing was written.

### Upload layer (`app/upload`, milestone M8)

| Module | Responsibility |
|---|---|
| `rate.py` | `TokenBucket`: an immutable token bucket. `take()` returns the wait a send would owe, so the caller can schedule it instead of sleeping |
| `choke.py` | `ChokePolicy`: pure tit-for-tat — slots ranked by bytes received, plus one optimistic slot that rotates to whoever has had it least |
| `manager.py` | `UploadManager`: request validation, per-peer queues, round-robin serving, rate pacing, choke decisions on the wire, `have` broadcast, upload statistics |

Design decisions that matter:

* **Every refusal has a reason.** A request is checked for a valid index, a
  valid offset, a legal length, whether we hold the piece, whether the peer is
  interested, whether it is unchoked, and how deep its queue already is — and
  each refusal is counted by reason. "How many peers are asking for pieces we
  do not have" is a question worth being able to answer.
* **The pacer schedules, it never sleeps in line.** A block the rate limit
  cannot pay for yet is handed to a background task with a wake-up time; the
  serving pass moves on to the next peer immediately. A slow cap costs
  throughput, not fairness, and never blocks the event loop.
* **Choking is decided from measurements, not promises.** Merit is bytes a peer
  has actually sent us; peers that have sent nothing for `snub_seconds` drop
  out, and a peer that leaves loses its slot immediately so the optimistic
  rotation can offer it to somebody who is still there.
* **The wire stays honest about what we have.** `have` is only broadcast for a
  piece whose SHA-1 verified, and only to peers that are connected at that
  moment — a piece nobody knows about is a piece nobody asks for.

### Statistics layer (`app/statistics`, milestone M9)

| Module | Responsibility |
|---|---|
| `speed.py` | `RateMeter` (one rolling window) and `SpeedMeter` (several at once, plus the session average). `take()`-style accounting: the bucket answers "how long would this cost?", and the caller schedules instead of sleeping |
| `history.py` | `History` and `HistoryBook`: bounded series of timestamped `Sample`s. Reading a series never creates one |
| `metrics.py` | `MetricsCollector` and `MetricsSnapshot`: read the download and upload engines, count bytes from events, publish `STATS_SAMPLE`, and hand the UI one honest object |

Design decisions that matter:

* **Rates are measured from events, not polled from counters.**
  `PIECE_BLOCK_RECEIVED` and `PIECE_UPLOADED` already carry the length of every
  block; subscribing to them means the statistics layer adds no coupling to the
  engines it watches.
* **A window is a promise about the past.** `RateMeter(5.0)` answers "what was
  the throughput of the last five seconds?" — which also means it reads zero
  five seconds after the last byte, and that is the point.
* **An ETA is only as honest as its rate.** `eta_seconds` is `None` when no
  window has a rate, and it prefers the instant window until the longer one has
  actually filled: dividing by a window that is mostly empty under-reports, and
  an under-reported rate is an ETA that lies slowly.
* **Nothing is estimated for a subsystem that is not there.** Sources are
  duck-typed and optional; a missing peer manager reads as zero peers, not as a
  guess.


## Application services (M10)

The four modules in `app/services/` are the seam between "a client" and "a pile
of subsystems". Everything above them is allowed to *ask* and to *read*; nothing
above them is allowed to touch a socket.

| Module | Owns | Contract |
|---|---|---|
| `engine.py` | one torrent's subsystems | The only place that knows the wiring. `Engine` injects storage, peers, download, upload, metrics, tracker and listener; `build_engine()` is a coroutine because the download engine decides what is missing when it is *constructed*, so progress on disk must be adopted first. |
| `session.py` | the set of torrents | `Session` owns config, bus and torrents; `add_torrent()`, `get()`, `remove()`, `start_all()`, `stop_all()`, `totals()`. Closing it stops everything. |
| `torrent_service.py` | one torrent, for a UI | `TorrentService` wraps an engine in start/pause/resume/remove that are safe to repeat, and serves an immutable `TorrentView`. |
| `app_state.py` | what the UI draws | `AppState.reduce(event)` folds the bus into an event ring and per-type counts; `snapshot()` reads rates and progress *at call time*, because a cached rate is a stale rate. |

Two rules the layer keeps:

**Callbacks belong to whoever needs them most.** The engine owns
`PeerManager.on_disconnect` because *both* engines must hear it — the
downloader re-plans its requests, the uploader drops the peer's queue. It also
subscribes to `PIECE_VERIFIED` and tells the upload engine, because a peer
cannot ask for a piece nobody announced.

**State is derived, never stored.** `TorrentState` is computed on every read:
`ERROR` → `PAUSED` → (`STOPPED` if it ever ran, else `IDLE`) → `SEEDING` if
complete → `STARTING` if not one byte has moved → `DOWNLOADING`. A state that
is stored is a state that can be wrong; this one cannot drift from the counters
it describes.

```
Application ──▶ Session ──▶ TorrentService ──▶ Engine ──▶ {peers, storage, trackers}
     │                                                          │
     └──────────────────▶ EventBus ◀─────────────────────────────┘
                             │
                             ▼
                        AppState ──▶ UI (M11–M13)
```

## Interface (M11–M13)

The interface is a desktop shell in PySide6, arranged MVVM, and it obeys one
rule above all others: **no widget holds a socket, a coroutine, or an engine.**
Everything it knows comes through `app/ui/bridge.py`, and everything it wants
done goes out through the same object.

```
MainWindow ──reads──▶ ViewModel ──reduces──▶ AppSnapshot (from AppState)
    │                                              ▲
    │ submits coroutines                            │ snapshot() every 200 ms
    ▼                                               │
EngineBridge ──▶ EngineLoop (asyncio thread) ──▶ Session ──▶ Engine
    ▲                                                  │
    └────── events_pending (queued Qt signal, one per drain) ◀──┘ EventBus
```

| Module | Owns |
|---|---|
| `bridge.py` | The Qt ↔ asyncio seam: `EngineLoop` (a daemon thread with its own loop), `StatePump` (reads `AppState` on a timer), `EventFeed` (accumulates bus events and crosses them as one batch per drain, with at most one queued signal outstanding), and `EngineBridge`, which is the only object the UI holds. |
| `app.py` | Composition root: `build_ui()` builds a window without starting it, `run_ui()` starts and runs it, `install_theme()` paints it. |
| `main_window.py` | Navigation, the header, the toast overlay, and every user action. |
| `viewmodels/` | `SessionViewModel` reduces a snapshot to the shapes the shell draws and remembers rate history in bounded buffers. `TorrentViewModel` does the same for one torrent and owns its swarm and piece view models; `PeersViewModel` differences per-peer counters into rates; `PiecesViewModel` tallies the five piece states; `LogsViewModel` holds a bounded, filtered timeline. |
| `models/` | Qt item models: peers, trackers, files, the piece legend, and the detail page's field table. They render what a view model decided — no filtering, no arithmetic of their own. |
| `views/` | One widget per screen. They read a view model and emit requests; they decide nothing. `torrent_detail_view.py` hosts the six detail tabs. |
| `charts/` | The three signature visuals, each with its mapping as a pure function (`place_nodes`, `layout_cells`, `map_series`) so it can be tested without a window. |
| `animations/` | `pulse.py` — glows that are *stimulated* by bytes moving and then decay; `transitions.py` — fades and the reduced-motion guard that makes them instant. |
| `widgets/` | Reusable parts: progress bars, stat cards, sparklines, health meters, rows, toasts, panels, the honest empty state. |
| `theme/` | `tokens.py` (numbers), `palette.py` (colour roles), `contrast.py` (the arithmetic those roles are measured with), `qss.py` (the generated stylesheet), `icons.py` (16 icons drawn in code, cached). |

**Measurements are pulled, events are pushed.** Rates and progress are read at
the moment they are drawn — a rate remembered from a second ago is not a rate —
so the pump asks `AppState` for a snapshot every 200 ms. Discrete events are the
opposite: they are pushed as they happen, because a log line should not wait for
a timer. Only a handful of event types become toasts (a failed tracker, a
completed torrent); a toast for every block received would be noise.

**Actions are submitted, never awaited.** `MainWindow._run(work)` hands a
*coroutine factory* to the engine loop and returns immediately. When the future
settles, the result crosses back as the queued `work_finished` signal — not as a
`QTimer.singleShot` from the engine thread, which has no Qt event loop to fire
it, and not as a blocking `.result()`, which would stop the window painting.

**The detail page pulls its slow views.** The 200 ms snapshot carries rates and
progress, but not the swarm or the piece map — those are read on the engine loop
by `Session.peers_view()` / `piece_map()` / `files_view()` / `trackers_view()`,
submitted as coroutines and folded in when they settle. Peers and pieces are
re-read every second; files and trackers every fifth time, because they change
far more slowly. The timer runs only while the detail page is on screen, and a
read already in flight is never re-issued.

**Every visual variable is measured.** In the swarm canvas, node size is how much
of the torrent the peer holds, colour is what it is (seed, leech, handshaking,
known-but-unreached), and line width is the rate actually measured over the last
interval; a peer's angle is a hash of its address, so nothing slides around when
the set changes. In the piece matrix, a cell is one piece, in one of the five
states, and pieces that *no* connected peer holds get a red outline — the most
useful thing it tells you. In the rate graph the y axis is scaled to the largest
sample in the buffer, rounded up to a clean binary unit, and that number is
printed; before two samples exist it says it is waiting rather than drawing a
flat line at zero, which would read as "downloading at 0 B/s".

**Animation is bound to data and switchable.** A node glows because bytes moved
(`pulse.py`), and decays when they stop; nothing pulses on a timer. With
`reduced_motion` set, glows and fades are dropped rather than shortened — a
140 ms fade is not an accessibility feature, it is a fade.

**Charts are bounded.** `SeriesBuffer` keeps 180 samples and drops the oldest;
`Throttle` refuses repaints inside the 20 fps budget. A fast swarm cannot turn
the interface into a busy loop, and a five-minute graph cannot quietly become a
fifty-minute one.

**The theme is data, not a second stylesheet.** Widgets ask for *roles*
(`palette.text_muted`, `state_colour("verified")`), never literals, so a light
theme is a second `Palette` instance. Icons are drawn with `QPainter` and
cached, so there are no binary assets to go missing.

**What is not built yet says so.** Until M15, DHT and magnet links were rendered
by `empty_state.not_implemented(..., milestone=…)`, which printed the milestone
that owned the feature; the pattern stays for anything genuinely unbuilt. No
screen shows invented numbers to make a screenshot look finished — and one feature is deliberately absent rather than faked: the peers
table has no *country* column, because resolving one needs a GeoIP database this
client does not ship, and a column of dashes is not information.

**The DHT screen reads the routing table, nothing else.** It shows nodes that
answered, when, and how often they have failed since. Where a panel would
otherwise print zeroes — a DHT that is switched off, and one that is listening
but has not reached a soul — it prints two different sentences, because the
first is a setting and the second is a network fact.

Screenshots are taken against a real swarm, not a fixture:
`python -m tools.screenshot` starts its own tracker and seeders, runs the client
against them over loopback, and only then writes `docs/screenshots/*.png`.

## Accessibility (M16)

Three claims, and each one has a test that can fail.

**1. Contrast is arithmetic, not taste.** `app/ui/theme/contrast.py` implements
WCAG 2.1 relative luminance and contrast ratio, and
`tests/ui/test_accessibility.py` measures every colour role against *every*
surface it can be painted on: text roles must clear 4.5:1, large text 3:1, and
the fills that carry meaning — piece states, peer states, progress, health —
3:1 (SC 1.4.11). `composite()` flattens a colour with an alpha channel over its
background first, so a 40 % overlay is judged as the colour the eye receives
rather than the one that was written down.

The palette is the *output* of these measurements, not the input: when first
measured, nine roles failed, and the dark theme's `piece_missing` came in at
1.23:1 — a cell you could not see against the card behind it. Each replacement
was reached by stepping lightness until it cleared the threshold on all four
surfaces, not just the one the failing screenshot happened to show.

**2. Every control has a name.** The test walks the real window — six pages,
and all six detail tabs, because a tab that has never been shown has never been
laid out and has no children to inspect — and asks Qt's own accessibility layer
what it would announce for each control. Asking Qt rather than reading strings
off the widgets matters: that layer knows a `QLabel` is another control's name
through `setBuddy`, and it knows an icon-only button has nothing to say. So
icon-only buttons carry an `accessibleName`, and a label beside a field is made
its buddy — which is also what gives the label its keyboard mnemonic. A tooltip
does not count as a name; it is a *description*, and letting an unnamed control
borrow one would make the check pass on a technicality.

**3. What is not interactive is not a tab stop.** The rate graph and the swarm
canvas are `NoFocus`: they are pictures, and giving them focus would put two
dead stops in the tab order. The piece matrix is not a picture — it is
keyboard-driven (arrows, `Home`/`End`, `Enter`/`Space` to select and read a
piece) — so it takes focus, and says in its accessible description that it
does. Motion honours `reduced_motion` and `QT_NO_ANIMATIONS`, and a fade that
is suppressed is instant rather than shortened.

Honest limits: this is WCAG 2.1 AA **contrast** and **naming**, measured in
code. It has never been exercised with a real screen reader (NVDA, VoiceOver,
Orca), and focus *order*, reflow at 200 % zoom, and the platform's own
high-contrast and large-text settings have not been audited. The light theme
passed the same measurements but has had far fewer hours of use.

## Event flow

```
Engine ──emit(TypedEvent)──▶ EventBus ──▶ AppState.reduce()
                                    │
                                    └──▶ EventFeed: queued Qt signal, one per drain
                                              └──▶ ViewModel.add_many(batch) ──▶ Widget.repaint()
```

`EventBus` is async and fan-out; a slow subscriber must not stall the engine, so subscribers run as
independent tasks and exceptions are logged, never propagated into engine code paths.
The Qt bridge is the *only* place that knows about both worlds.

Events cross into Qt **in batches**, not one at a time. A busy transfer emits a few hundred a second
(every block received, every piece verified), and one queued signal each meant one timeline refresh
each: measured on a 512 MiB transfer, 9,768 events cost 26.6 s of GUI-thread time in a 47 s run —
a window that had stopped painting to catch up on log lines. The feed now accumulates arrivals and
delivers them as one tuple per drain, and the timeline does one update for the whole batch; the same
run costs 0.17 s. A batch of failures raises one toast, not ten.

## Concurrency

| Concern | Mechanism |
|---|---|
| Networking | `asyncio` streams (`open_connection`, `start_server`) |
| UDP trackers / DHT | `asyncio.DatagramProtocol` |
| HTTP(S) announce | `aiohttp` |
| Disk I/O | `asyncio.to_thread` around `pwrite`/`pread`, so several pieces can be in flight without blocking the loop |
| SHA-1 verification | bounded `ThreadPoolExecutor` (`hash_workers`), CPU off the loop |
| UI updates | cross-thread Qt signals, queued; UI repaints at ≤ 20 Hz regardless of event rate |

## State persistence

`data/state/<info_hash>.json` holds completed-piece bitmap (hex), downloaded/uploaded counters,
file layout hash, download path, piece length and a format version. Writes are atomic
(temp file + `os.replace`) and debounced (default every 30 s and on every state transition
that matters: pause, stop, completion, piece verified).

## Configuration

JSON at `~/.config/bittorrent-client/config.json` (override with `BITTORRENT_CLIENT_CONFIG`),
mirroring the structure of TRD §40. Typed dataclasses in `app/core/config.py`; unknown keys are
rejected on load, missing keys fall back to defaults, and a corrupt file falls back to defaults
with a warning instead of crashing.

## Honest-state policy

Any subsystem that is not implemented must render an explicit "not enabled / not implemented"
state rather than synthetic numbers (PRD §13). `app/ui/widgets/empty_state.py` is the single
place this is expressed.
