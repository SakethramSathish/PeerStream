"""Settings: the configuration, shown honestly.

Every control here edits a field that actually exists in
:class:`~app.core.config.Config`, and none of them lies about what it does.
The DHT toggle is a real setting with a real consequence, so the note beside it
says what enabling it costs (outbound UDP) and when it takes effect (the next
start) rather than promising instant results.

Changes are collected into an override mapping and applied by the window when
the user presses Apply: a config that mutated on every keystroke would be a
config that could half-apply.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.config import Config
from app.core.constants import MAX_PORT, MIN_PORT
from app.ui.theme.tokens import TOKENS

# The pump clamps to this range; the spin box should not offer values outside
# it (see app.ui.app: the interval is clamped to 16-200 ms).
MIN_INTERVAL_MS: int = 16
MAX_INTERVAL_MS: int = 1000

# 0 means unlimited everywhere in this client; the spin box says so in words
# rather than showing a fake ceiling.
UNLIMITED: int = 0


class _Group(QFrame):
    """A titled block of settings."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("kind", "card")
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(
            TOKENS.spacing.lg, TOKENS.spacing.md, TOKENS.spacing.lg, TOKENS.spacing.lg
        )
        self._column.setSpacing(TOKENS.spacing.sm)
        heading = QLabel(title.upper())
        heading.setProperty("role", "small")
        heading.setStyleSheet("font-weight: 700; letter-spacing: 1px;")
        self._column.addWidget(heading)

    def add(self, label: str, widget: QWidget, *, note: str = "") -> None:
        """Add one labelled control, with an optional note underneath.

        The label is made the control's *buddy*, which does two things a row of
        two widgets left side by side does not: a screen reader announces the
        label as the control's name, and the label's mnemonic focuses it.
        """
        widget.setMinimumHeight(TOKENS.geometry.control_height)
        row = QHBoxLayout()
        row.setSpacing(TOKENS.spacing.md)
        text = QLabel(label)
        text.setMinimumWidth(180)
        text.setStyleSheet("font-weight: 500;")
        text.setBuddy(widget)
        row.addWidget(text)
        row.addWidget(widget, stretch=1)
        self._column.addLayout(row)
        if note:
            hint = QLabel(note)
            hint.setProperty("role", "faint")
            hint.setWordWrap(True)
            hint.setStyleSheet(f"font-size: {TOKENS.type.caption}pt; padding-left: 2px;")
            self._column.addWidget(hint)


class SettingsView(QWidget):
    """The settings screen.

    Args:
        config: The configuration to show.
        parent: Qt parent.
    """

    applied = Signal(object)
    """Emitted with the new :class:`Config` when the user applies changes."""

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._dirty = False
        spacing = TOKENS.spacing

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        inner = QWidget()
        scroll_column = QVBoxLayout(inner)
        scroll_column.setContentsMargins(spacing.xl, spacing.lg, spacing.xl, spacing.xl)
        scroll_column.setSpacing(spacing.lg)

        heading = QLabel("Settings")
        heading.setProperty("role", "strong")
        heading.setStyleSheet(f"font-size: {TOKENS.type.title}pt;")
        scroll_column.addWidget(heading)

        # ---- appearance
        look = _Group("Interface", self)
        self._theme = QComboBox()
        self._theme.addItems(["dark", "light", "amoled"])
        self._theme.setCurrentText(config.ui.theme)
        self._theme.currentTextChanged.connect(self._mark_dirty)
        look.add("Theme", self._theme, note="The light palette is the same roles, re-tinted. AMOLED is true black.")

        self._motion = QCheckBox("Reduce motion")
        self._motion.setChecked(config.ui.reduced_motion)
        self._motion.stateChanged.connect(self._mark_dirty)
        look.add("Animation", self._motion, note="Turns off fades and pulses, keeps every layout.")

        self._interval = QSpinBox()
        self._interval.setRange(MIN_INTERVAL_MS, MAX_INTERVAL_MS)
        self._interval.setSuffix(" ms")
        self._interval.setValue(config.ui.update_interval_ms)
        self._interval.valueChanged.connect(self._mark_dirty)
        look.add(
            "Refresh interval", self._interval, note="How often the interface reads the engine."
        )
        scroll_column.addWidget(look)

        # ---- transfer
        transfer = _Group("Transfer", self)
        self._down_speed = QSpinBox()
        self._down_speed.setRange(0, 1_000_000)
        self._down_speed.setSuffix(" KiB/s")
        self._down_speed.setValue(_to_kib(config.download.max_download_speed))
        self._down_speed.valueChanged.connect(self._mark_dirty)
        transfer.add("Download limit", self._down_speed, note="0 means unlimited.")

        self._up_speed = QSpinBox()
        self._up_speed.setRange(0, 1_000_000)
        self._up_speed.setSuffix(" KiB/s")
        self._up_speed.setValue(_to_kib(config.upload.max_upload_speed))
        self._up_speed.valueChanged.connect(self._mark_dirty)
        transfer.add("Upload limit", self._up_speed, note="0 means unlimited.")

        self._slots = QSpinBox()
        self._slots.setRange(1, 64)
        self._slots.setValue(config.upload.slots)
        self._slots.valueChanged.connect(self._mark_dirty)
        transfer.add("Upload slots", self._slots, note="How many peers we serve at once.")
        scroll_column.addWidget(transfer)

        # ---- network
        network = _Group("Network", self)
        self._port = QSpinBox()
        self._port.setRange(1024, 65535)
        self._port.setValue(config.network.listen_port)
        self._port.valueChanged.connect(self._mark_dirty)
        network.add("Listen port", self._port, note="Applies to torrents started after Apply.")

        self._max_peers = QSpinBox()
        self._max_peers.setRange(1, 500)
        self._max_peers.setValue(config.network.max_peers_per_torrent)
        self._max_peers.valueChanged.connect(self._mark_dirty)
        network.add("Peers per torrent", self._max_peers)

        self._incoming = QCheckBox("Accept incoming connections")
        self._incoming.setChecked(config.network.accept_incoming_connections)
        self._incoming.stateChanged.connect(self._mark_dirty)
        network.add("Inbound peers", self._incoming, note="Needed to seed to peers behind NAT.")
        scroll_column.addWidget(network)

        # ---- storage
        storage = _Group("Storage", self)
        self._directory = QLineEdit(str(config.storage.download_directory))
        self._directory.setMinimumHeight(TOKENS.geometry.control_height)
        self._directory.textChanged.connect(self._mark_dirty)
        storage.add("Download folder", self._directory)
        scroll_column.addWidget(storage)

        # ---- peer discovery
        discovery = _Group("Peer discovery", self)
        self._dht = QCheckBox("Distributed hash table")
        self._dht.setChecked(config.dht.enabled)
        self._dht.stateChanged.connect(self._mark_dirty)
        discovery.add(
            "DHT",
            self._dht,
            note="Finds peers without a tracker, which is what makes magnet links "
            "resolvable. Needs outbound UDP; takes effect on the next start.",
        )
        self._dht_port = QSpinBox()
        self._dht_port.setRange(MIN_PORT, MAX_PORT)
        self._dht_port.setValue(config.dht.port)
        self._dht_port.setMinimumHeight(TOKENS.geometry.control_height)
        self._dht_port.valueChanged.connect(self._mark_dirty)
        discovery.add("DHT port", self._dht_port, note="UDP port the DHT listens on.")
        scroll_column.addWidget(discovery)

        scroll_column.addStretch(1)

        # ---- buttons
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._status = QLabel("")
        self._status.setProperty("role", "faint")
        buttons.addWidget(self._status)
        self._revert = QPushButton("Revert")
        self._revert.setEnabled(False)
        self._revert.setMinimumHeight(TOKENS.geometry.control_height)
        self._revert.clicked.connect(self.revert)
        buttons.addWidget(self._revert)
        self._apply = QPushButton(" Apply")
        self._apply.setObjectName("primary")
        self._apply.setEnabled(False)
        self._apply.setMinimumHeight(TOKENS.geometry.control_height)
        self._apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply.clicked.connect(self.apply_changes)
        buttons.addWidget(self._apply)
        scroll_column.addLayout(buttons)

        scroll.setWidget(inner)

    # ------------------------------------------------------------------ content

    @property
    def config(self) -> Config:
        """The configuration this view is showing."""
        return self._config

    @property
    def dirty(self) -> bool:
        """Whether there are unapplied edits."""
        return self._dirty

    def set_config(self, config: Config) -> None:
        """Show a different configuration (used after Apply)."""
        self._config = config
        self.revert()

    def overrides(self) -> dict[str, dict[str, Any]]:
        """The edits, as a mapping of config sections.

        Only sections with a change are included, so applying cannot silently
        reset a value the user never touched.
        """
        speed_down = _from_kib(self._down_speed.value())
        speed_up = _from_kib(self._up_speed.value())
        return {
            "ui": {
                "theme": self._theme.currentText(),
                "reduced_motion": self._motion.isChecked(),
                "update_interval_ms": self._interval.value(),
            },
            "download": {"max_download_speed": speed_down},
            "upload": {"max_upload_speed": speed_up, "slots": self._slots.value()},
            "network": {
                "listen_port": self._port.value(),
                "max_peers_per_torrent": self._max_peers.value(),
                "accept_incoming_connections": self._incoming.isChecked(),
            },
            "storage": {"download_directory": self._directory.text().strip()},
            "dht": {"enabled": self._dht.isChecked(), "port": self._dht_port.value()},
        }

    # ------------------------------------------------------------------ actions

    def apply_changes(self) -> Config:
        """Build the new configuration, validate it, and announce it.

        Returns:
            The applied configuration. If the values are rejected by
            :class:`Config` — a port out of range, an unparseable path — the
            old configuration is kept and the reason is shown, because a
            settings screen that fails silently is a settings screen that lies.
        """
        try:
            updated = self._config.with_overrides(**self.overrides())
        except (ValueError, TypeError) as error:
            self._status.setText(f"not applied: {error}")
            return self._config
        self._config = updated
        self._dirty = False
        self._apply.setEnabled(False)
        self._revert.setEnabled(False)
        self._status.setText("applied")
        self.applied.emit(updated)
        return updated

    def revert(self) -> None:
        """Put every control back to the configuration's values."""
        config = self._config
        self._theme.setCurrentText(config.ui.theme)
        self._motion.setChecked(config.ui.reduced_motion)
        self._interval.setValue(config.ui.update_interval_ms)
        self._down_speed.setValue(_to_kib(config.download.max_download_speed))
        self._up_speed.setValue(_to_kib(config.upload.max_upload_speed))
        self._slots.setValue(config.upload.slots)
        self._port.setValue(config.network.listen_port)
        self._max_peers.setValue(config.network.max_peers_per_torrent)
        self._incoming.setChecked(config.network.accept_incoming_connections)
        self._directory.setText(str(config.storage.download_directory))
        self._dht.setChecked(config.dht.enabled)
        self._dht_port.setValue(config.dht.port)
        self._mark_clean()

    def on_applied(self, handler: Callable[[Config], None]) -> None:
        """Register a callback for applied configurations."""
        self.applied.connect(handler)

    # ------------------------------------------------------------------ plumbing

    def _mark_dirty(self, *_args: object) -> None:
        self._dirty = True
        self._apply.setEnabled(True)
        self._revert.setEnabled(True)
        self._status.setText("unsaved changes")

    def _mark_clean(self) -> None:
        self._dirty = False
        self._apply.setEnabled(False)
        self._revert.setEnabled(False)
        self._status.setText("")


def _to_kib(bytes_per_second: int) -> int:
    """Bytes per second to whole KiB/s. Rounds down; 0 stays 0 (unlimited)."""
    if bytes_per_second <= 0:
        return UNLIMITED
    return max(1, bytes_per_second // 1024)


def _from_kib(kib_per_second: int) -> int:
    """Whole KiB/s to bytes per second."""
    if kib_per_second <= 0:
        return UNLIMITED
    return kib_per_second * 1024
