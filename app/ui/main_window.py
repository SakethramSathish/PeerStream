"""Main window: embedding QWebEngineView for the HTML/CSS frontend."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from collections.abc import Callable

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebChannel import QWebChannel

from app.core.config import Config
from app.ui.bridge import EngineBridge
from app.ui.web_bridge import WebBridge

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    """The application window containing the WebEngineView.

    Args:
        bridge: The Qt ↔ asyncio bridge.
        config: The configuration.
    """

    # Keep signals for app.py compatibility
    settings_applied = Signal(object)

    def __init__(self, bridge: EngineBridge, config: Config) -> None:
        super().__init__()
        self._bridge = bridge
        self._config = config
        self._save_config: Callable[[Config], object] | None = None
        
        self.resize(1360, 860)
        self.setMinimumSize(1024, 640)
        self.setWindowTitle("PeerStream")

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Create WebBridge and QWebChannel
        self._web_bridge = WebBridge(bridge, self)
        self._channel = QWebChannel(self)
        self._channel.registerObject("bridge", self._web_bridge)

        # Create QWebEngineView
        self._view = QWebEngineView(self)
        self._view.page().setWebChannel(self._channel)
        
        # Configure WebEngine settings
        settings = self._view.page().settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

        # Load index.html
        base_dir = getattr(sys, "_MEIPASS", Path(__file__).parent.parent.parent)
        index_path = Path(base_dir) / "app" / "ui" / "web" / "index.html"
        self._view.load(QUrl.fromLocalFile(str(index_path)))

        layout.addWidget(self._view)

    def set_config_saver(self, saver: Callable[[Config], object]) -> None:
        """Called by app.py to wire config saving."""
        self._save_config = saver
