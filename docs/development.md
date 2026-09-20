# Development

## Setup

```bash
./scripts/setup.sh                   # creates .venv, installs deps + headless Qt libs
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# or manually:
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ui]"
```

Python 3.12+ is required. The protocol stack itself has no third-party dependencies;
`aiohttp` (trackers) and `PySide6` (UI) are the only runtime requirements.

Developed and tested on Linux. The storage layer no longer assumes POSIX — it
falls back where `os.pwrite`, `os.pread` and `O_CLOEXEC` are absent, and CI runs
`tests/storage/test_portability.py` on Windows to prove it — but the rest of the
suite has not been run there, so Windows is not a supported platform yet.

## Layout

See `docs/architecture.md` for the layer rules and `docs/implementation-plan.md` for the
milestone list. The short version:

```
app/bencode     bencode codec
app/torrent     .torrent parsing, info-hash, magnet links
app/tracker     HTTP(S) + UDP announce/scrape
app/peer        handshake, message framing, bitfields, connection state
app/download    piece/block state, selection, scheduling, endgame
app/upload      block serving, choking policy
app/storage     file mapping, allocation, resume state, verification
app/statistics  rate meters, metrics snapshots, ring buffers
app/services    session/torrent services + engine (public API for UI and CLI)
app/ui          PySide6 presentation layer
cli/            headless front-end
tools/          test-torrent generator, mock tracker, mock seeder, storage and swarm harnesses
tests/          unit, integration and mock infrastructure
```

## Everyday commands

```bash
pytest                                   # whole suite
pytest tests/bencode -q                  # one subsystem
pytest -m "not network" -q               # offline only (default behaviour)
pytest --network -q                      # include live-tracker tests
pytest --cov=app --cov-report=term       # coverage

python tools/mock_tracker.py --port 8000  # local tracker for manual runs
python tools/peer_probe.py file.torrent --peers 10            # handshake with real peers
python tools/peer_probe.py file.torrent --swarm --peers 10     # connect through the peer manager
python tools/storage_check.py --size 8MiB --files 4 --out /tmp/sc   # store + verify a payload
python tools/storage_check.py --size 1MiB --corrupt 3 --out /tmp/sc # prove a bad piece is rejected
python tools/swarm_download.py --size 8MiB --seeds 3 --out /tmp/sd   # download from a local swarm
python tools/swarm_download.py --size 4MiB --seeds 3 --slow-seeds 1 --no-endgame  # the cost of no endgame
python tools/upload_check.py --size 8MiB --leechers 4 --slots 2  # seed to a local swarm
python tools/upload_check.py --hostile --leechers 2             # refused requests, by reason
python tools/upload_check.py --rate 100k                        # the upload cap, measured
python tools/metrics_check.py --size 8MiB --seeds 3              # download + seed while sampling
python tools/metrics_check.py --size 1MiB --interval 0.02         # denser samples, ETA convergence
python tools/metrics_check.py --size 1MiB --no-upload             # download only

ruff check . && ruff format .            # lint + format
mypy app cli tools                       # type check (strict, all source)

python -m cli.main info path/to/a.torrent          # inspect metadata
python -m cli.main download path/to/a.torrent --download-dir /tmp/dl --no-seed
python -m cli.main download path/to/a.torrent --download-dir /tmp/dl --seed-minutes 10

python -m app.main                       # desktop UI
```

## Local test swarm (no internet required)

```bash
# 1. Generate a deterministic payload + .torrent (the payload lands in --dir)
python tools/make_test_torrent.py --size 8MiB --out data/torrents/test.torrent \
       --dir /tmp --announce http://127.0.0.1:8000/announce

# 2. Start a local tracker and two seeders (each in its own shell)
python -m tools.mock_tracker --port 8000
python -m tools.mock_seeder --torrent data/torrents/test.torrent \
       --payload /tmp/test-payload.bin --port 6901
python -m tools.mock_seeder --torrent data/torrents/test.torrent \
       --payload /tmp/test-payload.bin --port 6902

# 3. Download with the real engine, headless
python -m cli.main download data/torrents/test.torrent --download-dir /tmp/dl --no-seed
```

The seeders are standalone processes speaking the real protocol: they announce
`left=0` to the tracker and re-announce on the tracker's interval, so a client
that discovers peers the ordinary way finds them. `--choke` and `--silent`
make one misbehave, for tests that need a peer that will not serve.

## UI work

```bash
python -m app.main                                        # normal run
QT_QPA_PLATFORM=offscreen python -m app.main              # headless smoke test

# Screenshots, against a real local swarm (tracker + seeders started for you)
QT_QPA_PLATFORM=offscreen python -m tools.screenshot --out docs/screenshots

# ...or with your own torrent, three seeders, eight seconds of traffic
QT_QPA_PLATFORM=offscreen python -m tools.screenshot \
    --torrent data/torrents/test.torrent --payload /tmp/test-payload.bin \
    --seeders 3 --seconds 8 --theme light
```

The screenshot tool starts a `MockTracker` and `MockPeer` seeders on its own
thread, adds the torrent to the real session, selects it, lets Qt's event loop
turn for `--seconds`, and writes one PNG per page — or per detail tab, with
`--pages detail:peers,detail:pieces`. It prints what was on screen beside every
file (progress, peers, rate). The numbers in the PNGs are measured: if a panel
shows 0 B/s, the transfer was doing 0 B/s.

`tests/ui/` builds the real window offscreen (the package's `conftest.py` sets
`QT_QPA_PLATFORM=offscreen` before PySide6 is imported), so the shell is tested
without a display:

```bash
pytest tests/ui -q                 # theme, formatters, widgets, charts, tabs, shell, screenshots
```

Rules when writing UI code:
* Widgets render `AppState` snapshots; they never call into the engine synchronously.
* The bridge is the only object that knows both worlds: submit a coroutine with
  `MainWindow._run()`, and refresh from the queued `work_finished` signal.
* Services are the only API surface above `app/services/`: `Session` → `TorrentService` → `Engine`, with `AppState` reducing the bus for the UI. `Application` composes all three (`app/core/application.py`).
* Repaint coalescing: high-frequency values (rates, piece deltas) are throttled to ~20 Hz.
* Every animation must be bound to a real state change (PRD §11, §36).
* Honour the reduced-motion guard in `app/ui/animations/transitions.py`.
* The detail page's slow views (swarm, pieces, files, trackers) are coroutines on
  the engine loop: `Session.peers_view()`, `piece_map()`, `files_view()`,
  `trackers_view()`. Never read engine state from Qt.
* A chart's mapping belongs in a pure function (`place_nodes`, `layout_cells`,
  `map_series`) so it can be tested without a window.
* Layout must not shift when numeric values change — use tabular figures and fixed-width slots.

## Accessibility work

```bash
pytest tests/ui/test_accessibility.py -q    # contrast maths, palette, control names
```

The palette is measured, not chosen. `app/ui/theme/contrast.py` implements the
WCAG arithmetic; the test walks every colour role against every surface it can
be painted on and fails on anything under threshold. So when you add a colour:

* add it to `palette.py` as a **role** (`text_faint`, `piece_missing`, …), not as
  a literal in a widget;
* run the test, and move its lightness until it clears — against *all four*
  surfaces, not the one you are looking at;
* if it is translucent, measure the composite: `contrast.composite(colour, background)`.

Controls are named, not decorated. `QLabel.setBuddy(field)` is how a label
becomes another control's name (and its mnemonic); `setAccessibleName` is what
an icon-only button needs. A tooltip is a description and does not make an
unnamed control nameable. The test walks every page and every detail tab and
asks Qt's accessibility layer what it would announce, so an unnamed control
fails the build rather than a design review.

## Packaging

```bash
bash scripts/setup.sh          # creates .venv and installs the package (editable)
pip install -e .               # ...or just the install
python -m pip wheel . --no-deps -w dist/     # build a wheel
pip install dist/bittorrent_client-0.1.0-py3-none-any.whl
bittorrent --help              # the console script, after installing
python -m cli.main --help      # the same thing, from a checkout
python -m app.main             # the desktop UI
```

The wheel ships `app*`, `cli*` and `tools*` as *found* packages — a fixed list
of the three top-level names ships the directories and none of their modules,
which installs a client that imports and immediately fails.
`tests/tools/test_packaging.py` reads the build config from `pyproject.toml`
and compares it against the packages on disk, imports each of them, resolves
every console-script entry point, runs `python -m cli.main --help`, and checks
that the licence `pyproject.toml` declares has text beside it — metadata repeats
the declaration whether or not a `LICENSE` file exists, so the inconsistency is
otherwise invisible until someone tries to comply with the licence they were
granted.

## Continuous integration

`.github/workflows/ci.yml` runs the three gates on every push and pull request,
plus two narrower jobs:

| Job | Runs | Why it is separate |
|---|---|---|
| `gates` | `scripts/setup.sh`, then `ruff check app tools tests cli`, `mypy app tools`, `pytest tests` with a 90% coverage floor | The whole suite, on the documented bootstrap, so CI cannot drift from what a contributor is told to run |
| `package` | `python -m build --wheel`, install into a clean venv, `bittorrent info` on a generated torrent | The editable install hides a broken wheel; only a clean install shows one |
| `windows-storage` | `pytest tests/storage/test_portability.py` on `windows-latest` | The fallbacks in `app/storage/files.py` exist for a platform the Linux runner only simulates |

Reproduce the gate job locally with:

```bash
bash scripts/setup.sh
.venv/bin/ruff check app tools tests cli
.venv/bin/mypy app tools
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests -q \
    --cov=app --cov=cli --cov=tools --cov-fail-under=90
```

Two deliberate absences. There is no `ruff format --check` step: the lint rules
and the formatter disagree about seven pre-existing files, and a gate that fails
on arrival teaches people to ignore gates. And the ten `network`-marked tests are
not run — they need `--network`, which is available from the workflow's dispatch
form for a maintainer who wants a live-swarm answer and accepts that a public
swarm is not a reproducible fixture.

## Commit discipline

One logical change per commit, imperative subject, prefixed:

```
feat: add bencode decoder
fix: reject oversized peer frames
refactor: extract choke policy from upload manager
test: cover rarest-first selection
docs: describe piece state machine
```

## Definition of done (per milestone)

1. Behaviour implemented, no placeholders.
2. Unit tests added/updated and passing.
3. `ruff` and `mypy` clean for touched modules.
4. Docs updated (this folder + docstrings).
5. Verified end-to-end against the local mock swarm where applicable.
