"""The add-torrent dialog: choose a file or paste a magnet, choose a folder.

Adding is deliberately a two-step, visible operation. The dialog parses what
the user gave it *before* the torrent is handed to the session, so the
confirmation shows the real name, the real size, and the real piece count from
the metainfo. Nothing is guessed: if the file cannot be parsed, the dialog says
which file and why rather than disabling "Add" with no explanation.

A magnet is accepted, but not as a torrent: it is a name for one. The dialog
shows what the link itself claims — the info hash, the display name, how many
trackers and peers it offers — and says plainly that the size and the file list
cannot be known until a peer hands over the metadata. That fetch happens after
the user confirms, and it can fail; a dialog that pretended otherwise would be
promising a torrent nobody has promised us.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.torrent import (
    MagnetError,
    Torrent,
    TorrentError,
    UnsupportedMagnetError,
    parse_torrent_file,
)
from app.torrent.magnet import MagnetUri, parse_magnet
from app.ui.format import human_bytes
from app.ui.theme import icons
from app.ui.theme.tokens import TOKENS

# What the dialog accepts in the file picker. The metainfo file is a .torrent;
# everything else is a mistake the user should be told about, politely.
FILE_FILTER: str = "Torrent files (*.torrent);;All files (*)"


class AddTorrentDialog(QDialog):
    """Pick a ``.torrent`` file, see what is inside, and add it.

    Args:
        default_directory: Where downloaded data goes by default.
        parent: Qt parent.
    """

    torrent_accepted = Signal(Torrent, str)
    """Emitted with the parsed :class:`~app.torrent.Torrent` and the chosen
    save directory as a string."""

    magnet_accepted = Signal(MagnetUri, str)
    """Emitted with a parsed :class:`~app.torrent.magnet.MagnetUri` and the
    chosen save directory as a string.

    Separate from :attr:`torrent_accepted` because a magnet is a promise, not a
    torrent: the name and size are unknown until a peer supplies the metadata,
    so the caller has a different job to do.
    """

    def __init__(
        self,
        *,
        default_directory: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dialog")
        self.setWindowTitle("Add torrent")
        self.setModal(True)
        self.setMinimumWidth(720)
        self.setMinimumHeight(480)
        self.resize(960, 620)
        spacing = TOKENS.spacing

        column = QVBoxLayout(self)
        column.setContentsMargins(spacing.xl, spacing.lg, spacing.xl, spacing.lg)
        column.setSpacing(spacing.md)

        heading = QLabel("Add a torrent")
        heading.setProperty("role", "strong")
        heading.setStyleSheet(f"font-size: {TOKENS.type.title}pt;")
        column.addWidget(heading)

        # ---- source row
        source = QHBoxLayout()
        source.setSpacing(spacing.sm)
        self._path = QLineEdit()
        self._path.setPlaceholderText("Choose a .torrent file, or paste a magnet link")
        self._path.setMinimumHeight(TOKENS.geometry.control_height)
        self._path.textChanged.connect(self._on_source_changed)
        source.addWidget(self._path, stretch=1)

        self._browse = QPushButton(" Browse")
        self._browse.setIcon(icons.icon("folder", size=TOKENS.geometry.icon))
        self._browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse.setMinimumHeight(TOKENS.geometry.control_height)
        self._browse.clicked.connect(self._browse_for_file)
        source.addWidget(self._browse)
        column.addLayout(source)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setProperty("role", "muted")
        self._status.setStyleSheet(f"font-size: {TOKENS.type.small}pt;")
        column.addWidget(self._status)

        # ---- parsed summary
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setProperty("kind", "card")
        self._summary.setContentsMargins(spacing.md, spacing.md, spacing.md, spacing.md)
        self._summary.hide()
        column.addWidget(self._summary)

        # ---- destination
        destination = QHBoxLayout()
        destination.setSpacing(spacing.sm)
        caption = QLabel("Save to")
        caption.setProperty("role", "muted")
        destination.addWidget(caption)
        self._directory = QLineEdit(str(default_directory or ""))
        self._directory.setMinimumHeight(TOKENS.geometry.control_height)
        destination.addWidget(self._directory, stretch=1)
        pick = QPushButton(" Choose")
        pick.setIcon(icons.icon("folder", size=TOKENS.geometry.icon))
        pick.setCursor(Qt.CursorShape.PointingHandCursor)
        pick.setMinimumHeight(TOKENS.geometry.control_height)
        pick.clicked.connect(self._browse_for_directory)
        destination.addWidget(pick)
        column.addLayout(destination)

        self._start = QCheckBox("Start downloading immediately")
        self._start.setChecked(True)
        column.addWidget(self._start)

        column.addStretch(1)

        # ---- buttons
        buttons = QHBoxLayout()
        buttons.setSpacing(spacing.sm)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setMinimumHeight(TOKENS.geometry.control_height)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self._accept = QPushButton(" Add torrent")
        self._accept.setObjectName("primary")
        self._accept.setIcon(icons.icon("plus", size=TOKENS.geometry.icon))
        self._accept.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accept.setMinimumHeight(TOKENS.geometry.control_height)
        self._accept.setEnabled(False)
        self._accept.clicked.connect(self._on_accept)
        buttons.addWidget(self._accept)
        column.addLayout(buttons)

        self._torrent: Torrent | None = None
        self._magnet: MagnetUri | None = None

    # ------------------------------------------------------------------ content

    @property
    def torrent(self) -> Torrent | None:
        """The parsed torrent, or ``None`` if nothing valid has been chosen."""
        return self._torrent

    @property
    def directory(self) -> str:
        """Where the user asked for the data to go."""
        return self._directory.text().strip()

    @property
    def magnet(self) -> MagnetUri | None:
        """The parsed magnet link, or ``None`` if the source is not one."""
        return self._magnet

    @property
    def start_now(self) -> bool:
        """Whether the torrent should start transferring as soon as it is added."""
        return self._start.isChecked()

    def set_source(self, source: str) -> None:
        """Pre-fill the source field and parse it, as drag-and-drop would."""
        self._path.setText(source)
        self._on_source_changed(source)

    # ------------------------------------------------------------------- events

    def _browse_for_file(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(self, "Choose a torrent", "", FILE_FILTER)
        if path:
            self._path.setText(path)

    def _browse_for_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose where to save", self.directory)
        if path:
            self._directory.setText(path)

    def _on_source_changed(self, text: str) -> None:
        self._torrent = None
        self._magnet = None
        self._summary.hide()
        source = text.strip()
        if not source:
            self._status.setText("")
            self._accept.setEnabled(False)
            return
        if source.startswith("magnet:"):
            self._on_magnet(source)
            return
        try:
            torrent = parse_torrent_file(source)
        except (OSError, ValueError, TorrentError) as error:
            # Every way a file can fail to be a torrent is caught, because an
            # exception raised inside a Qt slot does not stop at the dialog: it
            # travels up into the event loop.
            self._status.setText(f"Could not read that torrent: {error}")
            self._accept.setEnabled(False)
            return
        self._torrent = torrent
        self._status.setText("")
        self._summary.setText(_describe(torrent))
        self._summary.show()
        self._accept.setEnabled(True)

    def _on_magnet(self, source: str) -> None:
        """Parse a magnet link and describe what it actually promises."""
        try:
            magnet = parse_magnet(source)
        except UnsupportedMagnetError as error:
            self._status.setText(str(error))
            self._accept.setEnabled(False)
            return
        except MagnetError as error:
            self._status.setText(f"That is not a usable magnet link: {error}")
            self._accept.setEnabled(False)
            return

        self._magnet = magnet
        self._status.setText(
            "A magnet names a torrent; it does not describe it. "
            "The size and file list appear once a peer sends the metadata."
        )
        self._summary.setText(_describe_magnet(magnet))
        self._summary.show()
        self._accept.setEnabled(True)

    def _on_accept(self) -> None:
        # Disable the button immediately to prevent double-submission if the
        # user clicks twice before the dialog closes.
        self._accept.setEnabled(False)
        directory = self.directory
        if self._magnet is not None:
            self.magnet_accepted.emit(self._magnet, directory)
            self.accept()
            return
        if self._torrent is None:
            self._accept.setEnabled(True)
            return
        self.torrent_accepted.emit(self._torrent, directory)

    def show_error(self, message: str) -> None:
        """Show an error message and re-enable the accept button."""
        self._status.setText(message)
        self._accept.setEnabled(True)


def _describe_magnet(magnet: MagnetUri) -> str:
    """What the link itself carries — and what it cannot know yet.

    The display name is a claim made by whoever wrote the link; the real name
    arrives with the metadata and may differ. Both are worth showing, because
    the difference is the interesting part.
    """
    lines = [f"<b>{magnet.name}</b>"]
    lines.append(f"Info hash: {magnet.hex_info_hash}")
    hints = []
    if magnet.trackers:
        hints.append(f"{len(magnet.trackers)} tracker" + ("s" if len(magnet.trackers) > 1 else ""))
    if magnet.peers:
        hints.append(f"{len(magnet.peers)} peer" + ("s" if len(magnet.peers) > 1 else ""))
    lines.append("Sources: " + (", ".join(hints) if hints else "none — it will need the DHT"))
    lines.append("<i>Size and files: unknown until the metadata arrives.</i>")
    return "<br>".join(lines)


def _describe(torrent: Torrent) -> str:
    """The lines shown once a torrent has been parsed."""
    files = torrent.files
    return "\n".join(
        [
            f"<b>{torrent.name}</b>",
            f"{human_bytes(torrent.total_length)}   "
            f"{len(files)} file{'s' if len(files) != 1 else ''}   "
            f"{torrent.piece_count} pieces of {human_bytes(torrent.piece_length)}",
            f"info hash {torrent.info_hash.hex()}",
        ]
    )
