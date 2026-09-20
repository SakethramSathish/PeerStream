"""Desktop entry point: ``python -m app.main``.

Everything lives in :mod:`app.ui.app`; this module exists so the command in the
README is the only thing a reader has to remember.
"""

from __future__ import annotations

import sys

from app.ui.app import run_ui

if __name__ == "__main__":
    raise SystemExit(run_ui(sys.argv))
