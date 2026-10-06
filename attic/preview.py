"""Shared audition-before-you-download plumbing.

Both the search dialog and the Discover panel need the same thing: stream a
candidate's audio without downloading it, without disturbing the library
player, and clean up afterwards. The pipeline is built on first use and torn
down on shutdown, so a session that never previews anything pays nothing.
"""

from PySide6.QtCore import QObject, QThreadPool, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from downloader import PreviewTask


class PreviewController(QObject):
    """Streams one candidate at a time, pausing the library while it plays."""

    status_changed = Signal(str)
    active_changed = Signal(bool)

    def __init__(self, library_player, parent=None) -> None:
        super().__init__(parent)
        self._library = library_player
        self._pool = QThreadPool.globalInstance()
        self._task: PreviewTask | None = None
        self._player: QMediaPlayer | None = None
        self._output: QAudioOutput | None = None
        self._volume = 80
        # Whether the library was playing when we interrupted it, so callers
        # can put things back the way they found them.
        self._library_was_playing = False

    @property
    def library_was_playing(self) -> bool:
        return self._library_was_playing

    def preview(self, video_id: str, volume: int) -> None:
        self._volume = volume
        if self._library is not None and self._library.is_playing:
            self._library_was_playing = True
            self._library.pause()
        self.stop()
        self.status_changed.emit("Loading preview…")
        task = PreviewTask(video_id)
        task.signals.finished.connect(self._on_ready)
        task.signals.error.connect(self._on_error)
        # Held so the signal object outlives the pooled run.
        self._task = task
        self._pool.start(task)

    def _ensure_player(self) -> QMediaPlayer:
        if self._player is None:
            self._output = QAudioOutput()
            self._output.setVolume(max(0.0, min(1.0, self._volume / 100.0)))
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._output)
        return self._player

    def _on_ready(self, url: str) -> None:
        player = self._ensure_player()
        player.setSource(QUrl(url))
        player.play()
        self.status_changed.emit("Previewing…")
        self.active_changed.emit(True)

    def _on_error(self, message: str) -> None:
        # Inline and non-fatal: previewing is best-effort, since not every
        # stream format plays back on every platform backend.
        self.status_changed.emit("Preview unavailable for this one")
        self.active_changed.emit(False)

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
        self.status_changed.emit("")
        self.active_changed.emit(False)

    def resume_library_if_interrupted(self) -> None:
        if self._library_was_playing and self._library is not None:
            self._library.play()
        self._library_was_playing = False

    def shutdown(self) -> None:
        self.stop()
        if self._player is not None:
            self._player.setSource(QUrl())
