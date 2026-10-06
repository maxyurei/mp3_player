"""The art "screen": album art, a looping visual, and a level-driven bloom.

Motion is a *playback indicator*, not decoration. Stopped or paused shows the
album art; pressing play crossfades to the track's visual. That is nicer to
look at than a permanent backdrop, and it means an idle player animates
nothing at all.

Cost control, in order of how much it matters:
  * frames are scaled once at load, so nothing is ever blitted larger than the
    panel;
  * the movie is paused whenever playback stops, the window is minimised, or
    the panel is hidden — Qt will otherwise happily keep decoding an animation
    nobody can see;
  * CacheNone, because caching every frame of a 60-frame loop at panel size
    costs ~15 MB of RAM to save a cheap re-decode;
  * "reduce motion" swaps the movie for its own first frame, keeping the
    composition identical with zero animation.
"""

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImageReader,
    QLinearGradient,
    QMovie,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ui import theme

# QWIDGETSIZE_MAX — the value Qt uses for "no maximum".
_UNBOUNDED = 16_777_215

# Frames are never decoded larger than this on the long edge. Full-bleed at
# 1040x640 is ~10x the pixels of the 248px column, which is the one part of
# focus mode that pushes against the app's whole reason for existing. Capping
# the decode and letting the blit upscale the last stretch is invisible on a
# soft looping GIF and keeps the cost bounded.
MAX_DECODE_PX = 720


class ArtPanel(QWidget):
    context_menu_requested = Signal(QPoint)
    clicked = Signal()

    FADE_MS = 260
    FADE_TICK_MS = 16
    BRACKET_LEN = 13
    MIN_HEIGHT = 100
    RESCALE_DEBOUNCE_MS = 120

    def __init__(self, width: int, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("artPanel")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.context_menu_requested)

        # Square when there is room, shorter when the window is cramped. A
        # hard-fixed square cannot fit the minimum window height, and a fixed
        # widget in a squeezed layout makes Qt overlap everything below it.
        self._preferred = width
        self.setFixedWidth(width)
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setMaximumHeight(width)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        self._cover_source: QPixmap | None = None
        self._cover: QPixmap | None = None
        self._movie: QMovie | None = None
        self._still: QPixmap | None = None
        self._visual_path: Path | None = None

        self._fade = 0.0  # 0 = album art, 1 = visual
        self._target_fade = 0.0
        self._playing = False
        self._suspended = False
        self._reduce_motion = False
        self._level = 0.0

        # Focus mode: the panel goes full-bleed and paints the now-playing text
        # over itself instead of the column laying it out underneath.
        self._focus = False
        # Mini mode: the panel has been reparented into ui/mini_player, which
        # paints its own transport over it.
        self._mini = False
        self._status_text = ""
        self._status_live = False
        self._title_text = ""
        self._artist_text = ""

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(self.FADE_TICK_MS)
        self._fade_timer.timeout.connect(self._step_fade)

        # Rescaling on every tick of a window drag would re-read the visual off
        # disk dozens of times a second; wait for the drag to settle instead.
        # Until then the old pixmap just crops slightly differently.
        self._rescale_timer = QTimer(self)
        self._rescale_timer.setSingleShot(True)
        self._rescale_timer.setInterval(self.RESCALE_DEBOUNCE_MS)
        self._rescale_timer.timeout.connect(self._rescale)

    def sizeHint(self) -> QSize:
        return QSize(self._preferred, self._preferred)

    # --- content ---

    def set_track(self, cover_data: bytes | None, visual_path: Path | None) -> None:
        pixmap = QPixmap()
        self._cover_source = (
            pixmap if cover_data and pixmap.loadFromData(cover_data) else None
        )
        self._rescale_cover()
        self._load_visual(visual_path)
        self._retarget_fade()
        self.update()

    def clear(self) -> None:
        self._cover_source = None
        self._cover = None
        self._load_visual(None)
        self._playing = False
        self._level = 0.0
        self._fade = self._target_fade = 0.0
        self._fade_timer.stop()
        self.update()

    def _load_visual(self, path: Path | None) -> None:
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        self._still = None
        self._visual_path = path
        if path is None:
            return

        target = self._fill_size(QImageReader(str(path)).size())

        if self._reduce_motion:
            reader = QImageReader(str(path))
            if target.isValid():
                reader.setScaledSize(target)
            image = reader.read()
            if not image.isNull():
                self._still = QPixmap.fromImage(image)
            return

        movie = QMovie(str(path))
        if not movie.isValid():
            return
        if target.isValid():
            movie.setScaledSize(target)
        movie.setCacheMode(QMovie.CacheMode.CacheNone)
        movie.frameChanged.connect(self._on_frame)
        self._movie = movie
        movie.start()
        self._sync_motion()

    def _fill_size(self, native: QSize) -> QSize:
        """Smallest size that covers the panel, capped at MAX_DECODE_PX."""
        if not native.isValid() or native.isEmpty():
            return QSize()
        scale = max(self.width() / native.width(), self.height() / native.height())
        width = max(1, round(native.width() * scale))
        height = max(1, round(native.height() * scale))
        longest = max(width, height)
        if longest > MAX_DECODE_PX:
            shrink = MAX_DECODE_PX / longest
            width = max(1, round(width * shrink))
            height = max(1, round(height * shrink))
        return QSize(width, height)

    def _rescale_cover(self) -> None:
        if self._cover_source is None:
            self._cover = None
            return
        # Aspect-fill: the overflow is cropped against the clip rect in paint,
        # rather than letterboxed against the bezel.
        self._cover = self._cover_source.scaled(
            self._fill_size(self._cover_source.size()),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _rescale(self) -> None:
        self._rescale_cover()
        if self._visual_path is not None:
            target = self._fill_size(QImageReader(str(self._visual_path)).size())
            if target.isValid():
                if self._movie is not None:
                    self._movie.setScaledSize(target)
                elif self._still is not None:
                    reader = QImageReader(str(self._visual_path))
                    reader.setScaledSize(target)
                    image = reader.read()
                    if not image.isNull():
                        self._still = QPixmap.fromImage(image)
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale_timer.start()

    def _visual_pixmap(self) -> QPixmap | None:
        if self._movie is not None:
            pixmap = self._movie.currentPixmap()
            return None if pixmap.isNull() else pixmap
        return self._still

    def _has_visual(self) -> bool:
        return self._movie is not None or self._still is not None

    # --- state ---

    def set_playing(self, playing: bool) -> None:
        self._playing = playing
        if not playing:
            self._level = 0.0
        self._retarget_fade()

    def set_suspended(self, suspended: bool) -> None:
        """Called when the window is minimised — stop decoding frames."""
        self._suspended = suspended
        self._sync_motion()

    def set_reduce_motion(self, reduce_motion: bool) -> None:
        if reduce_motion == self._reduce_motion:
            return
        self._reduce_motion = reduce_motion
        self._load_visual(self._visual_path)  # swap movie <-> still frame
        self._retarget_fade()
        self.update()

    @property
    def reduce_motion(self) -> bool:
        return self._reduce_motion

    @property
    def focus_mode(self) -> bool:
        return self._focus

    def set_focus_mode(self, on: bool) -> None:
        """Full-bleed with an overlay, or back to the fixed art column."""
        if on == self._focus:
            return
        self._focus = on
        if on:
            self.setMinimumWidth(0)
            self.setMaximumWidth(_UNBOUNDED)
            self.setMaximumHeight(_UNBOUNDED)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
        else:
            self.setFixedWidth(self._preferred)
            self.setMinimumHeight(self.MIN_HEIGHT)
            self.setMaximumHeight(self._preferred)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._rescale_timer.start()
        self.update()

    @property
    def mini_mode(self) -> bool:
        return self._mini

    def set_mini_mode(self, on: bool) -> None:
        """Detached into the mini window, or back in the art column.

        Sizing matches focus mode — fill whatever the container is — but the
        painted now-playing text is suppressed: its scrim is sized as a
        fraction of panel height, which at 128px would bury the visual it is
        supposed to caption. The mini window paints its own transport instead.

        MIN_HEIGHT goes too. It exists so a squeezed art *column* doesn't make
        Qt overlap the labels below it; in the mini window there is nothing
        below it, and 100px would put a floor under the smallest preset.
        """
        if on == self._mini:
            return
        self._mini = on
        if on:
            self._focus = False
            self.setMinimumSize(0, 0)
            self.setMaximumWidth(_UNBOUNDED)
            self.setMaximumHeight(_UNBOUNDED)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
        else:
            self.setFixedWidth(self._preferred)
            self.setMinimumHeight(self.MIN_HEIGHT)
            self.setMaximumHeight(self._preferred)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._rescale_timer.start()
        self.update()

    def set_now_playing(self, status: str, title: str, artist: str, live: bool) -> None:
        self._status_text = status
        self._title_text = title
        self._artist_text = artist
        self._status_live = live
        if self._focus:
            self.update()

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        # Repaint only on a change the eye can resolve: this is driven from an
        # audio callback that fires far faster than the display refreshes.
        if abs(level - self._level) < 0.03:
            return
        self._level = level
        self.update()

    def _retarget_fade(self) -> None:
        self._target_fade = 1.0 if (self._playing and self._has_visual()) else 0.0
        if abs(self._target_fade - self._fade) > 0.001:
            self._fade_timer.start()
        self._sync_motion()

    def _step_fade(self) -> None:
        step = self.FADE_TICK_MS / self.FADE_MS
        if self._fade < self._target_fade:
            self._fade = min(self._target_fade, self._fade + step)
        else:
            self._fade = max(self._target_fade, self._fade - step)
        if abs(self._fade - self._target_fade) < 0.001:
            self._fade = self._target_fade
            self._fade_timer.stop()
            self._sync_motion()  # pause now that it is fully hidden
        self.update()

    def _sync_motion(self) -> None:
        if self._movie is None:
            return
        visible = self.isVisible() and not self._suspended
        wanted = visible and (self._playing or self._fade > 0.0)
        self._movie.setPaused(not wanted)

    def _on_frame(self) -> None:
        if self._fade > 0.0:
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_motion()

    def hideEvent(self, event) -> None:
        if self._movie is not None:
            self._movie.setPaused(True)
        super().hideEvent(event)

    # --- painting ---

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.SCREEN))

        inner = rect.adjusted(1, 1, -1, -1)
        visual = self._visual_pixmap()

        painter.save()
        painter.setClipRect(inner)
        if self._cover is not None and self._fade < 1.0:
            painter.setOpacity(1.0 - self._fade)
            self._draw_filled(painter, self._cover, inner)
        if visual is not None and self._fade > 0.0:
            painter.setOpacity(self._fade)
            self._draw_filled(painter, visual, inner)
        painter.restore()

        # In focus mode the overlay already carries the track name, so an empty
        # panel gets the large type treatment instead of a "NO SIGNAL" stub.
        if self._cover is None and visual is None and not (self._focus and self._title_text):
            painter.setPen(QColor(theme.SUBDUED))
            painter.setFont(self.font())
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "NO SIGNAL")

        self._draw_overlay(painter, rect)
        self._draw_bloom(painter, rect)
        self._draw_bezel(painter, rect)

    @staticmethod
    def _draw_filled(painter: QPainter, pixmap: QPixmap, rect: QRect) -> None:
        """Draw `pixmap` covering `rect`, cropping whatever overflows.

        The decode cap means a frame can now be *smaller* than the panel, so
        the cover scale is computed here rather than assumed from the decode.
        """
        if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            return
        scale = max(rect.width() / pixmap.width(), rect.height() / pixmap.height())
        width = max(1, round(pixmap.width() * scale))
        height = max(1, round(pixmap.height() * scale))
        # Only pay for smoothing when actually enlarging.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, scale > 1.0)
        painter.drawPixmap(
            QRect(
                rect.x() + (rect.width() - width) // 2,
                rect.y() + (rect.height() - height) // 2,
                width,
                height,
            ),
            pixmap,
        )

    def _draw_overlay(self, painter: QPainter, rect: QRect) -> None:
        """Now-playing text over the art, for focus mode.

        Painted rather than laid out: no widget juggling between two layouts,
        and the scrim can sit exactly under the text it needs to make legible.
        """
        if self._mini or not self._focus or not (self._title_text or self._status_text):
            return
        painter.setOpacity(1.0)

        width = rect.width()
        title_px = max(16, min(34, width // 24))
        artist_px = max(11, min(18, width // 46))
        status_px = max(10, min(14, width // 62))
        pad = max(14, width // 40)

        band = max(
            int(rect.height() * 0.42),
            title_px + artist_px + status_px + pad * 3,
        )
        band = min(band, rect.height())
        top = rect.bottom() - band

        # Without a scrim, a bright frame makes every one of these unreadable.
        gradient = QLinearGradient(0, top, 0, rect.bottom())
        for stop, alpha in ((0.0, 0), (0.55, 190), (1.0, 242)):
            colour = QColor(theme.SCREEN)
            colour.setAlpha(alpha)
            gradient.setColorAt(stop, colour)
        painter.fillRect(QRect(rect.x(), top, width, band), gradient)

        text_width = width - pad * 2
        baseline = rect.bottom() - pad

        # Stacked upwards from the bottom edge: artist, then title, then status.
        for text, size, colour, weight in (
            (self._artist_text, artist_px, theme.MUTED, QFont.Weight.Normal),
            (self._title_text, title_px, theme.TEXT, QFont.Weight.DemiBold),
            (
                self._status_text,
                status_px,
                theme.CYAN if self._status_live else theme.SUBDUED,
                QFont.Weight.Normal,
            ),
        ):
            if not text:
                continue
            font = QFont(self.font())
            font.setPixelSize(size)
            font.setWeight(weight)
            painter.setFont(font)
            metrics = QFontMetrics(font)
            baseline -= metrics.height()
            painter.setPen(QColor(colour))
            painter.drawText(
                rect.x() + pad,
                baseline + metrics.ascent(),
                metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_width),
            )
            baseline -= max(2, size // 6)

    def _draw_bloom(self, painter: QPainter, rect: QRect) -> None:
        """Cyan rings that swell with the audio level — see Player.level_changed."""
        if self._level <= 0.02:
            return
        painter.setOpacity(1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(3):
            colour = QColor(theme.CYAN)
            colour.setAlpha(int(110 * self._level / (i + 1)))
            pen = QPen(colour)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(i + 1, i + 1, -(i + 2), -(i + 2)))

    def _draw_bezel(self, painter: QPainter, rect: QRect) -> None:
        painter.setOpacity(1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(theme.BORDER_HI))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

        # Corner brackets: cheap, static, and the thing that makes it read as a
        # display rather than a framed image.
        pen.setColor(QColor(theme.VIOLET))
        pen.setWidth(2)
        painter.setPen(pen)
        # Scaled, not fixed: 13px brackets on a 96px panel stop reading as
        # corner marks and start reading as a broken border.
        length = max(5, min(self.BRACKET_LEN, min(rect.width(), rect.height()) // 9))
        left, top = rect.left() + 2, rect.top() + 2
        right, bottom = rect.right() - 2, rect.bottom() - 2
        for x, y, dx, dy in (
            (left, top, 1, 1),
            (right, top, -1, 1),
            (left, bottom, 1, -1),
            (right, bottom, -1, -1),
        ):
            painter.drawLine(x, y, x + dx * length, y)
            painter.drawLine(x, y, x, y + dy * length)
