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

from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import QSystemTrayIcon, QMenu

from app.core.config import Config
from app.ui.bridge import EngineBridge
from app.ui.web_bridge import WebBridge

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    # Keep signals for app.py compatibility
    settings_applied = Signal(object)

    def __init__(self, bridge: EngineBridge, config: Config) -> None:
        super().__init__()
        self._bridge = bridge
        self._config = config
        self._save_config: Callable[[Config], object] | None = None
        self._force_close = False
        
        self.resize(1360, 860)
        self.setMinimumSize(1024, 640)
        self.setWindowTitle("PeerStream")

        # Setup System Tray
        self.tray_icon = QSystemTrayIcon(self)
        # Use a built-in icon or a fallback
        self.tray_icon.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon))
        self.tray_icon.setToolTip("PeerStream")
        
        tray_menu = QMenu()
        restore_action = QAction("Restore", self)
        restore_action.triggered.connect(self.showNormal)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.force_close)
        
        tray_menu.addAction(restore_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)

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

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.showNormal()
            self.activateWindow()

    def closeEvent(self, event):
        if not self._force_close:
            event.ignore()
            # Let the JS decide whether to exit completely or minimize to tray
            self._view.page().runJavaScript("if(window.openExit) window.openExit(); else window.bridge.exit_app();")
        else:
            event.accept()

    def force_close(self):
        self._force_close = True
        self.close()

    def set_config_saver(self, saver: Callable[[Config], object]) -> None:
        """Called by app.py to wire config saving."""
        self._save_config = saver

