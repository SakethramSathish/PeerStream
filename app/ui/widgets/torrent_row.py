"""One row in the library: what a torrent is doing, at a glance.

A row answers six questions in the order a user actually asks them: what is it,
is it going, how far along is it, how fast, how long will it take, and who is
serving it. Everything shown comes from a :class:`~app.services.torrent_service.TorrentView`,
which is built by measurement — so a row cannot claim a rate nobody measured.

The row never starts or stops anything itself. It emits a request with the
torrent's info hash, and the window hands that to the session on the engine
loop. A widget that drives the engine directly is a widget that can block on
the engine.
"""

from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.services.engine import TorrentState
from app.services.torrent_service import TorrentView
from app.ui.format import human_bytes, human_duration, human_percent, human_rate
from app.ui.theme import icons
from app.ui.theme.tokens import TOKENS
from app.ui.widgets.progress_bar import ProgressBar

#: Which colour role each state paints with. Roles, not colours: the light
#: theme redefines the palette and the rows follow without being edited.
STATE_ROLES: dict[str, str] = {
    TorrentState.IDLE.value: "faint",
    TorrentState.STARTING.value: "info",
    TorrentState.DOWNLOADING.value: "accent",
    TorrentState.SEEDING.value: "success",
    TorrentState.PAUSED.value: "warning",
    TorrentState.STOPPED.value: "muted",
    TorrentState.ERROR.value: "danger",
}

#: The bar's colour follows the same table.
BAR_ROLES: dict[str, str] = {
    TorrentState.IDLE.value: "faint",
    TorrentState.STARTING.value: "info",
    TorrentState.DOWNLOADING.value: "accent",
    TorrentState.SEEDING.value: "success",
    TorrentState.PAUSED.value: "warning",
    TorrentState.STOPPED.value: "muted",
    TorrentState.ERROR.value: "danger",
}


class TorrentRow(QFrame):
    """A single torrent in the list.

    Args:
        view: The torrent to show.
        parent: Qt parent.
    """

    pause_toggled = Signal(str)
    """Emitted with the info hash when the user asks to pause or resume."""

    remove_requested = Signal(str)
    """Emitted with the info hash when the user asks to remove."""

    selected = Signal(str)
    """Emitted with the info hash when the user clicks the row."""

    open_location = Signal(str)
    """Emitted with the info hash when the user asks to open the download folder."""

    def __init__(self, view: TorrentView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("row")
        self._view: TorrentView | None = None
        self._info_hash: str = ""
        self.setFixedHeight(TOKENS.geometry.row_height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        spacing = TOKENS.spacing

        row = QHBoxLayout(self)
        row.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        row.setSpacing(spacing.lg)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(spacing.xs)

        self._name = QLabel(view.name)
        self._name.setProperty("role", "strong")
        left.addWidget(self._name)

        self._meta = QLabel("")
        self._meta.setProperty("role", "faint")
        self._meta.setStyleSheet(f"font-size: {TOKENS.type.caption}pt;")
        left.addWidget(self._meta)
        row.addLayout(left, stretch=3)

        self._bar = ProgressBar(self, height=TOKENS.spacing.sm + 2)
        bar_column = QVBoxLayout()
        bar_column.setContentsMargins(0, 0, 0, 0)
        bar_column.setSpacing(spacing.xs)
        bar_column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar_column.addWidget(self._bar)
        self._progress = QLabel("")
        self._progress.setProperty("role", "faint")
        self._progress.setStyleSheet(f"font-size: {TOKENS.type.caption}pt;")
        bar_column.addWidget(self._progress)
        row.addLayout(bar_column, stretch=2)

        self._speed = QLabel("--")
        self._speed.setFixedWidth(96)
        self._speed.setProperty("role", "data")
        row.addWidget(self._speed)

        self._eta = QLabel("--")
        self._eta.setFixedWidth(84)
        self._eta.setProperty("role", "muted")
        row.addWidget(self._eta)

        self._state = QLabel("")
        self._state.setFixedWidth(104)
        self._state.setProperty("kind", "pill")
        row.addWidget(self._state)

        self._toggle = QPushButton()
        self._toggle.setProperty("kind", "ghost")
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.setIcon(icons.icon("pause", size=TOKENS.geometry.icon))
        self._toggle.clicked.connect(lambda: self.pause_toggled.emit(self.info_hash))
        row.addWidget(self._toggle)

        self._remove = QPushButton()
        self._remove.setProperty("kind", "ghost")
        self._remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remove.setIcon(icons.icon("trash", size=TOKENS.geometry.icon))
        self._remove.clicked.connect(lambda: self.remove_requested.emit(self.info_hash))
        row.addWidget(self._remove)

        self._open_folder = QPushButton()
        self._open_folder.setProperty("kind", "ghost")
        self._open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_folder.setIcon(icons.icon("folder", size=TOKENS.geometry.icon))
        self._open_folder.setToolTip("Open file location")
        self._open_folder.clicked.connect(self._on_open_folder)
        row.addWidget(self._open_folder)

        self.update_view(view)

    # ------------------------------------------------------------------ content

    @property
    def info_hash(self) -> str:
        """The torrent's info hash, as hex."""
        return self._info_hash

    @property
    def view(self) -> TorrentView | None:
        """The view last shown, if any."""
        return self._view

    def update_view(self, view: TorrentView) -> None:
        """Redraw from a fresh view. Cheap enough to call on every snapshot."""
        self._view = view
        self._info_hash = view.info_hash

        state = view.state.value
        self._state.setText(state)
        self._set_role(self._state, STATE_ROLES.get(state, "muted"))
        self._bar.set_role(BAR_ROLES.get(state, "accent"))
        self._bar.set_fraction(view.progress)

        metrics = view.metrics
        down = metrics.download.displayed if metrics else 0.0
        self._speed.setText(human_rate(down))
        eta = metrics.eta_seconds if metrics else None
        self._eta.setText(human_duration(eta))

        pieces = f"{view.verified_pieces}/{view.piece_count} pieces"
        peers = f"{metrics.peers_connected} peers" if metrics else "0 peers"
        self._meta.setText(f"{human_bytes(view.total_length)}   {pieces}   {peers}")
        self._progress.setText(human_percent(view.progress))
        self.setToolTip(_tooltip(view))

        # The button does what the row is not doing: a paused torrent offers
        # "play", a running one offers "pause".
        if view.active:
            self._toggle.setIcon(icons.icon("pause", size=TOKENS.geometry.icon))
            self._toggle.setToolTip("Pause")
        else:
            self._toggle.setIcon(icons.icon("play", size=TOKENS.geometry.icon))
            self._toggle.setToolTip("Resume")
        self._remove.setToolTip("Remove torrent")

    def _set_role(self, widget: QLabel, role: str) -> None:
        if widget.property("role") == role:
            return
        widget.setProperty("role", role)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _on_open_folder(self) -> None:
        """Tell the main window to open the download directory."""
        self.open_location.emit(self.info_hash)

    # ------------------------------------------------------------------- events

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Clicking anywhere on the row selects the torrent."""
        self.selected.emit(self.info_hash)
        super().mouseReleaseEvent(event)


def _tooltip(view: TorrentView) -> str:
    """The long-form explanation for a row, shown on hover."""
    metrics = view.metrics
    lines = [
        view.name,
        f"{human_percent(view.progress)} of {human_bytes(view.total_length)}",
    ]
    if metrics is not None:
        lines.append(
            f"down {human_rate(metrics.download.displayed)}   "
            f"up {human_rate(metrics.upload.displayed)}   "
            f"peers {metrics.peers_connected} ({metrics.peers_unchoked} unchoked)"
        )
        lines.append(f"wasted {human_bytes(metrics.wasted_bytes)}   ratio {metrics.share_ratio}")
    if view.error:
        lines.append(f"error: {view.error}")
    return "\n".join(lines)
