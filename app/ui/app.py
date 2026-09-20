"""QApplication bootstrap: theme, fonts, and the bridge to the engine.

This module is the desktop entry point's whole job: compose the application
(:class:`app.core.application.Application`), build a Qt application around it,
wire the :class:`~app.ui.bridge.EngineBridge`, and run until the user quits.

Two rules hold here:

* **The UI owns no engine code.** It holds a bridge that can submit coroutines
  and read snapshots, and nothing else.
* **Everything can be built without being started.** :func:`build_ui` returns
  the pieces; :func:`run_ui` starts them. That is what lets the tests build the
  real window offscreen and look at it.

Usage::

    python -m app.main                      # the desktop client
    QT_QPA_PLATFORM=offscreen python -m app.main   # headless, for screenshots
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app import __version__
from app.core.application import Application
from app.core.config import Config
from app.services import AppState, Session
from app.ui.bridge import EngineBridge
from app.ui.main_window import MainWindow
from app.ui.theme import icons, palette_for
from app.ui.theme.qss import build_stylesheet
from app.ui.theme.tokens import TOKENS

logger = logging.getLogger(__name__)

PROGRAM_NAME: str = "PeerStream"
ORGANISATION: str = "PeerStream"
DEFAULT_INTERVAL_MS: int = 200


@dataclass(slots=True)
class UiHandle:
    """Everything a running UI is made of.

    Attributes:
        qt: The QApplication.
        window: The main window.
        bridge: The Qt ↔ asyncio bridge.
        application: The composition root (config, bus, session, state).
    """

    qt: QApplication
    window: MainWindow
    bridge: EngineBridge
    application: Application

    @property
    def session(self) -> Session:
        return self.application.session  # type: ignore[return-value]

    @property
    def state(self) -> AppState:
        return self.bridge.state


def _qt_application(argv: Sequence[str]) -> QApplication:
    """The QApplication, created once per process."""
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    QApplication.setApplicationName(PROGRAM_NAME)
    QApplication.setOrganizationName(ORGANISATION)
    QApplication.setApplicationVersion(__version__)
    # Fusion is the only style that looks the same on every platform, and the
    # stylesheet is written against it.
    QApplication.setStyle("Fusion")
    with contextlib.suppress(Exception):  # Qt 6.8+ only, and not essential
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    app = QApplication(list(argv))
    
    # Load application icon from bundled PyInstaller resources or source directory
    if hasattr(sys, "_MEIPASS"):
        icon_path = os.path.join(sys._MEIPASS, "PeerStream.ico")
    else:
        # Relative to app/ui/app.py -> ../../packaging/PeerStream.ico
        icon_path = os.path.join(os.path.dirname(__file__), "..", "..", "packaging", "PeerStream.ico")
        
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
        
    return app


def install_theme(qt: QApplication, config: Config) -> tuple[str, object]:
    """Paint the application with the configured theme.

    Returns:
        ``(theme name, palette)`` so callers that paint by hand can use the
        same colours as the stylesheet.
    """
    palette = palette_for(config.ui.theme)
    icons.clear_cache()
    qt.setStyleSheet(build_stylesheet(palette, TOKENS))

    font = qt.font()
    font.setPointSize(TOKENS.type.body)
    font.setFamily(TOKENS.type.family.split(",")[0])
    qt.setFont(font)
    return palette.name, palette


def build_ui(
    argv: Sequence[str] = (),
    *,
    config_path: str | Path | None = None,
    load_config: bool = True,
    application: Application | None = None,
    interval_ms: int | None = None,
) -> UiHandle:
    """Compose the whole desktop client without starting it.

    Args:
        argv: Arguments for QApplication (usually ``sys.argv``).
        config_path: Explicit configuration file; the default path otherwise.
        load_config: When false, start from built-in defaults (tests).
        application: An already-composed application; composed when omitted.
        interval_ms: How often to read state; ``ui.update_interval_ms`` by
            default, but the interface reads more often than it samples
            statistics (200 ms feels live; 500 ms feels broken).

    Returns:
        A :class:`UiHandle` whose bridge has **not** been started.
    """
    core = application or Application.create(config_path=config_path, load_config=load_config)
    qt = _qt_application(argv)
    install_theme(qt, core.config)

    bridge = EngineBridge(
        core.session,  # type: ignore[arg-type]
        state=core.state,
        interval_ms=interval_ms
        or min(DEFAULT_INTERVAL_MS, max(16, core.config.ui.update_interval_ms)),
    )
    window = MainWindow(bridge, core.config)
    window.setWindowTitle(f"{PROGRAM_NAME} — {__version__}")

    # The settings screen can change the theme, and the stylesheet belongs to
    # the QApplication, so repainting is the composition root's job rather than
    # the window's. Writing the file back to disk is too: the window holds no
    # application, and saving must not block the GUI thread — so it is
    # submitted and forgotten, like every other engine action.
    window.settings_applied.connect(lambda config: install_theme(qt, config))
    window.set_config_saver(lambda _config: bridge.submit(core.save_config()))
    return UiHandle(qt=qt, window=window, bridge=bridge, application=core)


def run_ui(
    argv: Sequence[str] | None = None,
    *,
    config_path: str | Path | None = None,
    show: bool = True,
) -> int:
    """Build the client, start it, and run until the window closes.

    Args:
        argv: Command-line arguments; ``sys.argv`` when omitted.
        config_path: Explicit configuration file.
        show: Whether to show the window (off for smoke tests that only want
            the widget tree).

    Returns:
        A process exit code.
    """
    arguments = list(sys.argv if argv is None else argv)
    handle = build_ui(arguments, config_path=config_path)
    handle.bridge.start()
    # The DHT is started here, on the engine loop, before the first frame is
    # drawn: it needs a moment to fill its routing table, and a magnet link the
    # user pastes thirty seconds from now should find a network already there.
    handle.bridge.submit(handle.application.start())
    if show:
        handle.window.show()

    code = 0
    try:
        code = handle.qt.exec()
    except KeyboardInterrupt:  # pragma: no cover - depends on delivery timing
        logger.info("interrupted; shutting down")
        code = 130
    finally:
        handle.window.close()
        with contextlib.suppress(Exception):
            # The session was built on the engine loop; closing it there is the
            # only thread-safe way to stop its sockets.
            handle.bridge.submit(handle.application.stop()).result(timeout=10)
        handle.bridge.stop()
    return int(code)


__all__ = [
    "PROGRAM_NAME",
    "UiHandle",
    "build_ui",
    "install_theme",
    "run_ui",
]
