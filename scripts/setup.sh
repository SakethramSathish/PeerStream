#!/usr/bin/env bash
#
# Bootstrap a development environment for the BitTorrent client.
#
#   ./scripts/setup.sh              # venv + runtime/dev/UI dependencies
#   ./scripts/setup.sh --no-ui      # skip PySide6 (engine/CLI work only)
#
# The script is idempotent: re-running it refreshes an existing .venv.
# Python 3.12+ is required.
#
# The optional apt-get step installs the X11/Qt runtime libraries needed to
# *render* the PySide6 UI in a headless environment
# (QT_QPA_PLATFORM=offscreen, used by tools/screenshot.py and the UI tests).
# It is skipped automatically when apt-get or passwordless sudo is unavailable.

set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
VENV=".venv"
INSTALL_UI=1
for arg in "$@"; do
    case "$arg" in
        --no-ui) INSTALL_UI=0 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

echo "==> Python"
"$PYTHON" --version
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    echo "error: Python 3.12+ is required (set PYTHON=python3.12 to override)" >&2
    exit 1
fi

echo "==> Creating virtual environment in $VENV"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip

echo "==> Installing project dependencies"
if [ "$INSTALL_UI" -eq 1 ]; then
    "$VENV/bin/pip" install -q -e ".[dev,ui]"
else
    "$VENV/bin/pip" install -q -e ".[dev]"
fi

# ---------------------------------------------------------------- headless Qt
QT_LIBS=(
    libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0
    libxcb-xinerama0 libxcb-xkb1 libxcb-util1 libx11-xcb1 libdbus-1-3
    libfontconfig1 libfreetype6
)

if [ "$INSTALL_UI" -eq 1 ] && command -v apt-get >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then
        echo "==> Installing Qt runtime libraries for offscreen rendering"
        sudo apt-get update -qq
        # shellcheck disable=SC2068
        sudo apt-get install -y -qq ${QT_LIBS[@]}
    else
        echo "==> Skipping Qt system libraries (no passwordless sudo)"
        echo "    If the UI fails to start with 'libxkbcommon.so.0: cannot open',"
        echo "    install: ${QT_LIBS[*]}"
    fi
fi

echo "==> Verifying"
"$VENV/bin/python" - <<'PY'
import importlib
for module in ("pytest", "aiohttp"):
    importlib.import_module(module)
    print(f"  {module:8s} ok")
try:
    from PySide6 import QtWidgets
    print("  PySide6  ok")
except ImportError as exc:
    print(f"  PySide6  skipped ({exc})")
PY

cat <<'TXT'

Done. Activate the environment with:

    source .venv/bin/activate

Then:

    pytest -q                  run the test suite
    python -m cli.main --help  headless client
    python -m app.main         desktop UI
TXT
