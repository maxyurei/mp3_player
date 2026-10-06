"""Modal chooser for yt-dlp search results, with optional in-app preview.

The user picks one of the results to download. Each result can also be
previewed by streaming its audio directly (no download) so near-duplicate edits
can be auditioned before committing. Preview uses its own throwaway
QMediaPlayer, created lazily and torn down when the dialog closes, so it never
disturbs the main library player (which is merely paused while previewing).

Inherits the app-wide QSS automatically (set on the QApplication).
"""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from downloader import SearchResult
from ui.preview import PreviewController
from ui.widgets import format_duration


class SearchResultsDialog(QDialog):
    def __init__(self, results: list[SearchResult], player, volume: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Search Results")
        self.resize(560, 420)
        self._results = results
        self._volume = volume
        # Shared with the Discover panel — see ui/preview.py.
        self._preview = PreviewController(player, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        heading = QLabel("Pick a track to download (or preview first):")
        heading.setObjectName("minorLabel")
        layout.addWidget(heading)

        self._list = QListWidget()
        for result in results:
            text = f"{result.title}\n{result.uploader}  ·  {format_duration(result.duration)}"
            self._list.addItem(QListWidgetItem(text))
        if results:
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _: self.accept())
        layout.addWidget(self._list, stretch=1)

        # --- preview controls ---
        self._preview_btn = QPushButton("▶ Preview")
        self._preview_btn.clicked.connect(self._on_preview)
        self._stop_btn = QPushButton("■ Stop")
        self._stop_btn.clicked.connect(self._preview.stop)
        self._stop_btn.setEnabled(False)
        self._preview_status = QLabel("")
        self._preview_status.setObjectName("minorLabel")
        self._preview.status_changed.connect(self._preview_status.setText)
        self._preview.active_changed.connect(self._stop_btn.setEnabled)

        preview_row = QHBoxLayout()
        preview_row.setSpacing(8)
        preview_row.addWidget(self._preview_btn)
        preview_row.addWidget(self._stop_btn)
        preview_row.addWidget(self._preview_status, stretch=1)
        layout.addLayout(preview_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Download & Play")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # --- preview ---

    def _on_preview(self) -> None:
        result = self.selected_result()
        if result is not None:
            self._preview.preview(result.video_id, self._volume)

    def done(self, result: int) -> None:
        # Tear down preview audio whenever the dialog closes (OK or Cancel).
        self._preview.shutdown()
        super().done(result)

    # --- result access ---

    def selected_result(self) -> SearchResult | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._results):
            return self._results[row]
        return None
