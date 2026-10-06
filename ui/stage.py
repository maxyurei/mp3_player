"""The visual at full size, on purpose.

Two modes, one widget:

  STAGE      a borderless window (fullscreen by default) showing the visual
             full-bleed with the now-playing set large over a scrim. You open
             it and you close it; it never appears by itself.

  WALLPAPER  the same surface reparented onto the desktop layer, behind every
             other window. No text, no controls, no input — it is scenery. This
             is the one place the GIF can be permanently full-size without
             occupying a single pixel you were using.

The idle timer in here is the only one left in the app, and it is deliberate:
it hides *chrome*, never geometry. The window does not move, resize or
transform when you stop touching it — it just stops drawing controls over a
picture you are looking at, and hides the cursor with them.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMovie,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from ui import theme
from ui.gifs import honour_scaled_size
from ui.wallpaper import DesktopLayer

CHROME_IDLE_MS = 2000
FADE_MS = 400
FADE_TICK_MS = 16
SEEK_H = 3

# Hard ceiling on how far a source GIF is enlarged. Past roughly 2x there is no
# detail left to show and the blurred backdrop carries the frame better.
MAX_UPSCALE = 2.0
BLUR_WIDTH = 64


def format_time(ms: int) -> str:
    seconds = max(ms, 0) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


class Stage(QWidget):
    closed = Signal()
    play_pause_requested = Signal()
    next_requested = Signal()
    prev_requested = Signal()
    seek_requested = Signal(float)
    volume_delta = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(None)
        self.setWindowTitle("MP3 Player")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("stage")
        self.setMouseTracking(True)

        self._movie: QMovie | None = None
        self._visual_path: Path | None = None
        self._still: QPixmap | None = None
        self._blur: QPixmap | None = None
        self._blur_token = -1

        self._title = ""
        self._artist = ""
        self._context = ""
        self._position_ms = 0
        self._duration_ms = 0
        self._playing = False

        self._chrome = 1.0
        self._chrome_target = 1.0
        self._wallpaper = DesktopLayer(self)

        self._buttons: dict[str, QRect] = {}
        self._seek_rect = QRect()
        self._hover_key: str | None = None

        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(CHROME_IDLE_MS)
        self._idle.timeout.connect(self._hide_chrome)

        self._fade = QTimer(self)
        self._fade.setInterval(FADE_TICK_MS)
        self._fade.timeout.connect(self._step_fade)

    # ------------------------------------------------------------------
    # content
    # ------------------------------------------------------------------

    def set_visual(self, path: Path | None, fallback: QPixmap | None = None) -> None:
        self._still = fallback
        if path is not None and path == self._visual_path and self._movie is not None:
            return
        self._visual_path = path
        self._teardown_movie()
        self._blur = None
        self._blur_token = -1
        if path is not None and path.is_file():
            movie = QMovie(str(path))
            if movie.isValid():
                # See ui/ribbon.py: re-decoding every frame is far more
                # expensive than caching a loop of scaled frames.
                movie.setCacheMode(QMovie.CacheMode.CacheAll)
                movie.frameChanged.connect(self._on_frame)
                self._movie = movie
                self._apply_scale()
                self._sync_motion()
        self.update()

    def _teardown_movie(self) -> None:
        if self._movie is not None:
            self._movie.stop()
            self._movie.frameChanged.disconnect(self._on_frame)
            self._movie.deleteLater()
            self._movie = None

    def _on_frame(self) -> None:
        self._blur_token = -1
        self.update()

    def _apply_scale(self) -> None:
        """Scale to fit, capped — never cover-stretch a small GIF over 1080p.

        The source GIFs here are ~500x281. Covering a 1920x1080 screen demands
        3.84x (5.71x worst case) of a 256-colour dithered image, which is why
        fullscreen looked soft. Capping at MAX_UPSCALE and centring on a
        blurred backdrop is what a video player does with low-res material, and
        it reads as intentional instead of broken.
        """
        if self._movie is None:
            return
        size = self._movie.currentImage().size()
        if size.isEmpty():
            self._movie.jumpToFrame(0)
            size = self._movie.currentImage().size()
        if size.isEmpty():
            return
        scale = min(
            self.width() / max(1, size.width()),
            self.height() / max(1, size.height()),
        )
        scale = min(scale, MAX_UPSCALE)
        self._movie.setScaledSize(
            QSize(
                max(1, round(size.width() * scale)),
                max(1, round(size.height() * scale)),
            )
        )
        self._blur_token = -1

    def _current_frame(self) -> QPixmap | None:
        if self._movie is not None:
            pixmap = self._movie.currentPixmap()
            if not pixmap.isNull():
                return honour_scaled_size(self._movie, pixmap)
        if self._still is not None and not self._still.isNull():
            return self._still
        return None

    def _backdrop(self) -> QPixmap | None:
        """Tiny blurred copy of the frame, stretched to fill behind the picture.

        Squeezing to BLUR_WIDTH and letting the blit stretch it back is the
        entire blur — no filter pass, no full-screen intermediate.
        """
        frame = self._current_frame()
        if frame is None:
            return None
        token = self._movie.currentFrameNumber() if self._movie is not None else 0
        if self._blur is not None and self._blur_token == token:
            return self._blur
        height = max(1, round(BLUR_WIDTH * frame.height() / max(1, frame.width())))
        self._blur = frame.scaled(
            BLUR_WIDTH, height,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._blur_token = token
        return self._blur

    def set_now_playing(self, title: str, artist: str, context: str = "") -> None:
        self._title = title
        self._artist = artist
        self._context = context
        self.update()

    def set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        self._sync_motion()
        self.update()

    def set_progress(self, position_ms: int, duration_ms: int) -> None:
        self._position_ms = position_ms
        self._duration_ms = duration_ms
        if self._chrome > 0.01:
            self.update()

    # ------------------------------------------------------------------
    # motion contract
    # ------------------------------------------------------------------

    def _sync_motion(self) -> None:
        if self._movie is None:
            return
        should_run = self._playing and self.isVisible()
        if should_run:
            if self._movie.state() != QMovie.MovieState.Running:
                self._movie.start()
        elif self._movie.state() == QMovie.MovieState.Running:
            self._movie.setPaused(True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_scale()
        self._sync_motion()
        if not self._wallpaper.attached:
            self._wake_chrome()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._idle.stop()
        self._sync_motion()

    # ------------------------------------------------------------------
    # wallpaper mode
    # ------------------------------------------------------------------

    @property
    def is_wallpaper(self) -> bool:
        return self._wallpaper.attached

    def enter_wallpaper(self) -> bool:
        """Drop behind every window. Returns False if the shell refused."""
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.showNormal()
        self._wallpaper.fill_desktop()
        # Must be shown (and therefore have a real handle) before reparenting.
        self.show()
        if not self._wallpaper.attach():
            return False
        self._idle.stop()
        self._chrome = self._chrome_target = 0.0
        # Scenery does not take clicks — they belong to the desktop underneath.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._apply_scale()
        self.update()
        return True

    def leave_wallpaper(self) -> None:
        self._wallpaper.detach()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.hide()

    # ------------------------------------------------------------------
    # chrome fade
    # ------------------------------------------------------------------

    def _wake_chrome(self) -> None:
        if self._wallpaper.attached:
            return
        self._chrome_target = 1.0
        self._fade.start()
        self.unsetCursor()
        self._idle.start()

    def _hide_chrome(self) -> None:
        if self._wallpaper.attached:
            return
        self._chrome_target = 0.0
        self._fade.start()
        # Hide the pointer with the controls. A cursor parked over a fullscreen
        # visual is the last piece of UI left on screen.
        self.setCursor(Qt.CursorShape.BlankCursor)

    def _step_fade(self) -> None:
        step = FADE_TICK_MS / FADE_MS
        if self._chrome < self._chrome_target:
            self._chrome = min(self._chrome_target, self._chrome + step)
        else:
            self._chrome = max(self._chrome_target, self._chrome - step)
        if abs(self._chrome - self._chrome_target) < 0.001:
            self._chrome = self._chrome_target
            self._fade.stop()
        self.update()

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_scale()
        self._relayout()

    def _relayout(self) -> None:
        width, height = self.width(), self.height()
        button = 44
        gap = 10
        row = button * 3 + gap * 2
        # Right-aligned, not centred. Centred puts the glyphs straight through
        # the middle of a long title, and eliding the title to make room is a
        # worse trade than moving three buttons.
        left = width - row - 60
        top = height - 132
        self._buttons = {
            "prev": QRect(left, top, button, button),
            "play": QRect(left + button + gap, top, button, button),
            "next": QRect(left + (button + gap) * 2, top, button, button),
        }
        self._seek_rect = QRect(60, height - 70, max(1, width - 120), SEEK_H)

    # ------------------------------------------------------------------
    # input
    # ------------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        self._wake_chrome()
        pos = event.position().toPoint()
        key = None
        for name, rect in self._buttons.items():
            if rect.contains(pos):
                key = name
                break
        if key != self._hover_key:
            self._hover_key = key
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        if self._chrome > 0.4:
            for name, rect in self._buttons.items():
                if rect.contains(pos):
                    {
                        "prev": self.prev_requested,
                        "play": self.play_pause_requested,
                        "next": self.next_requested,
                    }[name].emit()
                    event.accept()
                    return
            if self._seek_rect.adjusted(0, -10, 0, 10).contains(pos):
                ratio = (pos.x() - self._seek_rect.x()) / max(1, self._seek_rect.width())
                self.seek_requested.emit(max(0.0, min(1.0, ratio)))
                event.accept()
                return
        self._wake_chrome()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.close()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.volume_delta.emit(5 if delta > 0 else -5)
            self._wake_chrome()
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_F11):
            self.close()
            event.accept()
            return
        if key == Qt.Key.Key_Space:
            self.play_pause_requested.emit()
            self._wake_chrome()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._idle.stop()
        self.closed.emit()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(rect, QColor(theme.SCREEN))

        # Blurred wash edge to edge, then the picture centred at honest size.
        backdrop = self._backdrop()
        if backdrop is not None:
            painter.setOpacity(0.60)
            painter.drawPixmap(rect, backdrop)
            painter.setOpacity(1.0)

        frame = self._current_frame()
        if frame is not None:
            if frame is self._still:
                frame = frame.scaled(
                    rect.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            painter.drawPixmap(
                rect.x() + (rect.width() - frame.width()) // 2,
                rect.y() + (rect.height() - frame.height()) // 2,
                frame,
            )

        # Wallpaper mode is scenery: no scrim, no text, no controls.
        if self._wallpaper.attached:
            return
        if self._chrome <= 0.01:
            return

        painter.setOpacity(self._chrome)
        self._paint_scrim(painter, rect)
        self._paint_text(painter, rect)
        self._paint_transport(painter)
        self._paint_seek(painter)

    def _paint_scrim(self, painter: QPainter, rect: QRect) -> None:
        head = int(rect.height() * 0.45)
        gradient = QLinearGradient(0, head, 0, rect.height())
        for stop, alpha in ((0.0, 0), (0.5, 160), (1.0, 235)):
            colour = QColor(theme.SCREEN)
            colour.setAlpha(alpha)
            gradient.setColorAt(stop, colour)
        painter.fillRect(QRect(0, head, rect.width(), rect.height() - head), gradient)

    def _paint_text(self, painter: QPainter, rect: QRect) -> None:
        left = 60
        # Stop short of the transport rather than running under it.
        available = max(120, self._buttons.get("prev", QRect()).left() - left - 24)
        bottom = self._seek_rect.top() - 26

        if self._artist:
            artist_font = QFont(self.font())
            artist_font.setPixelSize(17)
            metrics = QFontMetrics(artist_font)
            painter.setFont(artist_font)
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(
                left,
                bottom,
                metrics.elidedText(self._artist, Qt.TextElideMode.ElideRight, available),
            )
            bottom -= metrics.height() + 4

        title_font = QFont(self.font())
        title_font.setPixelSize(max(30, min(64, rect.width() // 22)))
        title_font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(title_font)
        painter.setFont(title_font)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            left,
            bottom,
            metrics.elidedText(self._title, Qt.TextElideMode.ElideRight, available),
        )
        bottom -= metrics.height()

        kicker_font = QFont(self.font())
        kicker_font.setPixelSize(11)
        kicker_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.4)
        kicker_metrics = QFontMetrics(kicker_font)
        painter.setFont(kicker_font)
        label = (self._context or ("NOW PLAYING" if self._playing else "PAUSED")).upper()
        # Cyan means live audio and nothing else, so the dot only lights while
        # something is actually coming out of the speakers.
        if self._playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.CYAN))
            painter.drawEllipse(QPoint(left + 3, bottom - 4), 3, 3)
            painter.setPen(QColor(theme.CYAN))
        else:
            painter.setPen(QColor(theme.SUBDUED))
        painter.drawText(left + (14 if self._playing else 0), bottom, label)

    def _paint_transport(self, painter: QPainter) -> None:
        glyphs = {
            "prev": "◀◀",
            "play": "▮▮" if self._playing else "▶",
            "next": "▶▶",
        }
        for key, rect in self._buttons.items():
            if key == self._hover_key:
                fill = QColor(theme.RAISED_HI)
                fill.setAlpha(150)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawRoundedRect(rect, 6, 6)
            font = QFont(self.font())
            font.setPixelSize(17 if key == "play" else 14)
            painter.setFont(font)
            if key == "play":
                painter.setPen(
                    QColor(theme.VIOLET_HI if key == self._hover_key else theme.TEXT)
                )
            else:
                painter.setPen(
                    QColor(theme.TEXT if key == self._hover_key else theme.MUTED)
                )
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyphs[key])

    def _paint_seek(self, painter: QPainter) -> None:
        rect = self._seek_rect
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BORDER))
        painter.drawRect(rect)
        if self._duration_ms > 0:
            ratio = max(0.0, min(1.0, self._position_ms / self._duration_ms))
            filled = int(rect.width() * ratio)
            painter.setBrush(QColor(theme.VIOLET))
            painter.drawRect(QRect(rect.x(), rect.y(), filled, rect.height()))
            if self._playing:
                painter.setBrush(QColor(theme.CYAN))
                painter.drawRect(
                    QRect(rect.x() + max(0, filled - 2), rect.y() - 1, 2,
                          rect.height() + 2)
                )

        clock_font = QFont(self.font())
        clock_font.setPixelSize(11)
        metrics = QFontMetrics(clock_font)
        painter.setFont(clock_font)
        painter.setPen(QColor(theme.SUBDUED))
        painter.drawText(rect.x(), rect.bottom() + metrics.height() + 4,
                         format_time(self._position_ms))
        remaining = format_time(self._duration_ms)
        painter.drawText(
            rect.right() - metrics.horizontalAdvance(remaining),
            rect.bottom() + metrics.height() + 4,
            remaining,
        )

    def shutdown(self) -> None:
        self._wallpaper.detach()
        self._teardown_movie()
