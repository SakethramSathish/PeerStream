"""The detail overview's field model: one torrent, as labelled numbers.

The library draws its rows from the session view model, so this is not a list
model — it is the table of facts that sits next to the rate graph on the detail
page: size, piece length, piece count, save path, share ratio, ETA, error.

Every value is either measured or the string ``"--"``. Where the engine cannot
know something yet — the ETA before a rate has been measured, or the share
ratio before anything has been uploaded — the row says so instead of showing a
zero that would read as a fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

from app.services.torrent_service import TorrentView
from app.ui.format import (
    UNKNOWN,
    human_bytes,
    human_duration,
    human_percent,
    human_rate,
    human_ratio,
)
from app.ui.viewmodels.torrent_vm import TorrentViewModel


@dataclass(frozen=True, slots=True)
class Field:
    """One labelled value.

    Attributes:
        label: What is being shown.
        value: The text.
        mono: Whether to draw it in the data font (for hashes and paths).
    """

    label: str
    value: str
    mono: bool = False


def _resumed_text(view: TorrentView) -> str:
    """How much of this torrent came from disk, in words.

    ``"--"`` when the torrent did not say: not knowing whether a download was
    resumed is different from knowing it started from nothing.
    """
    resumed = view.resumed
    if resumed is None:
        return UNKNOWN
    if resumed.empty:
        return "from scratch"
    return f"{resumed.pieces} pieces from disk"


def fields_for(view_model: TorrentViewModel) -> tuple[Field, ...]:
    """The facts worth listing for one torrent.

    Args:
        view_model: The torrent, with its metrics.

    Returns:
        Labelled values, in the order the overview shows them.
    """
    view = view_model.view
    if view is None:
        return (
            Field("torrent", "No torrent selected"),
            Field("why", "Pick one in the library, or drop a .torrent file here."),
        )

    metrics = view.metrics
    share = metrics.share_ratio if metrics else None
    verified = metrics.pieces_verified if metrics else view.verified_pieces
    piece_length = (
        view.total_length // view.piece_count
        if view.piece_count and view.total_length >= view.piece_count
        else 0
    )
    return (
        Field("name", view.name),
        Field("state", view.state.value.replace("_", " ")),
        Field("info hash", view.info_hash, mono=True),
        Field("size", human_bytes(view.total_length)),
        Field("pieces", f"{verified}/{view.piece_count} verified"),
        Field("piece size", human_bytes(piece_length) if piece_length else UNKNOWN),
        Field("progress", human_percent(view.progress)),
        Field("down speed", human_rate(view_model.download_rate)),
        Field("up speed", human_rate(view_model.upload_rate)),
        Field("eta", human_duration(view_model.eta_seconds)),
        Field("share ratio", human_ratio(share)),
        Field("downloaded", human_bytes(metrics.download.total if metrics else 0)),
        Field("uploaded", human_bytes(metrics.upload.total if metrics else 0)),
        Field("wasted", human_bytes(metrics.wasted_bytes if metrics else 0)),
        Field("listen port", str(view.port)),
        Field("resumed", _resumed_text(view)),
        Field("error", view.error or "none"),
    )


class TorrentFieldModel(QAbstractTableModel):
    """The overview's label/value table.

    Args:
        view_model: The torrent to describe.
        parent: Qt parent.
    """

    COLUMNS: tuple[str, ...] = ("", "")

    def __init__(self, view_model: TorrentViewModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._fields: tuple[Field, ...] = ()

    # ----------------------------------------------------------------- columns

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """Two: a label and a value."""
        return 0 if parent.isValid() else 2

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """No header: the labels are the first column."""
        return None

    # --------------------------------------------------------------------- rows

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """How many facts are listed."""
        return 0 if parent.isValid() else len(self._fields)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """One cell."""
        if not index.isValid() or not 0 <= index.row() < len(self._fields):
            return None
        field = self._fields[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return field.label if index.column() == 0 else field.value
        if role == Qt.ItemDataRole.FontRole and field.mono and index.column() == 1:
            from PySide6.QtGui import QFontDatabase

            font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            font.setPointSize(max(8, font.pointSize() - 1))
            return font
        return None

    # ------------------------------------------------------------------- update

    def refresh(self) -> None:
        """Re-read the torrent's facts."""
        fields = fields_for(self._view_model)
        if fields == self._fields:
            return
        self.beginResetModel()
        self._fields = fields
        self.endResetModel()

    @property
    def fields(self) -> tuple[Field, ...]:
        """The rows currently shown."""
        return self._fields
