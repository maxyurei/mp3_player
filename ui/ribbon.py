"""The always-present surface: a strip whose background is the looping visual.

This replaces both the old main window and the mini player. The mini player was
a 128px square containing only the GIF — too small to carry a title, and square,
which is the worst possible footprint because it consumes both screen
dimensions at once. Screens have leftover *horizontal* space almost always and
leftover square holes almost never.

Three rules:

  * Geometry never changes on its own. There is no idle timer here. Size,
    position and height change only when you drag, pick a preset, or dock.
  * Detail keys off width, not attention. A window that reshapes itself
    because you stopped moving the mouse is the thing this rebuild exists to
    delete.
  * Motion is pausable by construction. The QMovie is *stopped*, not merely
    ignored, whenever playback stops or the strip is hidden.

## Why the visual is composited rather than stretched

The first version cover-cropped one GIF across the whole strip. That is fine
floating at 460px — the source GIFs are around 500x281, so it lands near 1:1 —
but docked the strip is 1920 wide and the same code demanded a 3.84x upscale
(5.71x worst case) of a 256-colour dithered GIF. No scaler survives that; the
result read as an empty dark band, which is exactly what a docked ribbon looked
like.

So the visual is drawn as two layers instead:

  * a *blurred* backdrop stretched across the full width. Blur is the correct
    tool because it destroys precisely the high-frequency detail that upscaling
    cannot invent — a blurred 4x upscale reads as deliberate, a sharp one reads
    as broken. It is produced by squeezing the current frame down to ~48px and
    letting the blit stretch it back out, so there is no filter pass and no
    per-frame allocation.
  * a *crisp* panel on the left, fit to the strip's height and never upscaled
    past ART_MAX_UPSCALE. For a 500x281 source in a 96px strip that is a 0.34x
    downscale, which is genuinely sharp.

That also made the movie cheaper: frames are now decoded at panel size (~171px)
rather than at the full 1920px width.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMovie,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from ui import theme
from ui.gifs import honour_scaled_size

# Height presets, in px. 56 is a text strip with a hint of visual behind it;
# 200 is close to a poster and is for when the GIF is the point.
HEIGHTS = (56, 96, 140, 200)
DEFAULT_HEIGHT = 96

MIN_WIDTH = 200
DEFAULT_WIDTH = 460

# The crisp panel: at most this aspect, at most this fraction of the strip, and
# never blown up past ART_MAX_UPSCALE (beyond which it stops looking sharp and
# the blurred layer is doing a better job anyway).
ART_ASPECT = 1.78
ART_WIDTH_FRACTION = 0.22
ART_MAX_UPSCALE = 1.25

# Width the backdrop is squeezed to before being stretched back out. Small
# enough to be a real blur, large enough to keep the frame's colour layout.
BLUR_WIDTH = 48

# Idle, the strip sits slightly translucent so it reads as an overlay on the
# work behind it rather than a hole punched in it.
IDLE_OPACITY = 0.90

SNAP_PX = 24
DRAG_THRESHOLD_PX = 4
RESIZE_GRIP_PX = 6
SEEK_H = 2
VOLUME_FLASH_MS = 900
FADE_MS = 160
FADE_TICK_MS = 16

# Spectrum drawing. The analyser produces dsp.BANDS values; the display
# resamples that to fit. Wide strips interpolate *up* — 32 bars across 1245px
# is a 39px-wide block each, which reads as a bar chart rather than a spectrum.
BAR_MIN_W = 2
BAR_GAP = 1
BAR_TARGET_PITCH = 9  # px per bar (bar + gap) aimed for when there is room
BAR_MAX_COUNT = 96
PEAK_FALL = 0.012

# Floor on the interval between background recomposites — a ceiling on cost,
# since each recomposite is a stretched backdrop plus a scrim blit plus the
# panel (measured at 1.3 ms on a docked 1920px strip).
#
# This was 66 ms on the theory that ~15 fps is invisible at this size. It isn't,
# and the reason is that the frames are dropped *unevenly*: against a GIF whose
# own frames are 29 ms apart, a 66 ms floor keeps 3, 3, 2, 3, 3, 2… which judders
# far more visibly than a lower frame rate would. Six of the visuals in this
# library run faster than 66 ms; at 40 ms only the genuinely fast ones are
# touched at all, and the extra cost is paid only while one of those is showing.
MIN_COMPOSITE_MS = 40

# Below these widths there is no honest way to fit the line without crowding
# the transport, so it goes rather than being elided into nonsense.
WIDTH_FOR_ARTIST = 330
WIDTH_FOR_CLOCK = 280
# The spectrum needs real estate to look like anything; under this it is
# dropped and the text takes the room instead.
MIN_SPECTRUM_W = 70


def format_time(ms: int) -> str:
    seconds = max(ms, 0) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _reduce_max(values: list[float], count: int) -> list[float]:
    """Fewer bars, keeping peaks. Averaging would flatten the transients."""
    size = len(values) / count
    out = []
    for index in range(count):
        lo = int(index * size)
        hi = max(lo + 1, int((index + 1) * size))
        out.append(max(values[lo:hi]))
    return out


def _interpolate(values: list[float], count: int) -> list[float]:
    """More bars than bands, linearly interpolated into a smooth curve."""
    last = len(values) - 1
    if last <= 0:
        return [values[0] if values else 0.0] * count
    out = []
    for index in range(count):
        position = index * last / max(1, count - 1)
        low = int(position)
        high = min(last, low + 1)
        blend = position - low
        out.append(values[low] * (1.0 - blend) + values[high] * blend)
    return out


class Ribbon(QWidget):
    play_pause_requested = Signal()
    next_requested = Signal()
    prev_requested = Signal()
    seek_requested = Signal(float)
    volume_delta = Signal(int)
    stage_requested = Signal()
    palette_requested = Signal()
    quit_requested = Signal()
    height_changed = Signal(int)
    # "off", "top" or "bottom" — one signal rather than a bool plus an edge,
    # because docking to the other edge while already docked is a single user
    # action and splitting it into two would let the two halves disagree.
    dock_requested = Signal(str)
    taskbar_autohide_toggled = Signal(bool)
    wallpaper_toggled = Signal(bool)
    reshuffle_requested = Signal()
    geometry_settled = Signal()

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        position: QPoint | None = None,
    ) -> None:
        super().__init__(None)
        self.setWindowTitle("MP3 Player")
        # Parentless top-level rather than a Qt.Tool: this is the whole app now,
        # so it must be the thing the taskbar and Alt+Tab can reach. Stays on
        # top because floating without it means the strip is buried the moment
        # you click your editor — which defeats the entire point of it.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setObjectName("ribbon")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowOpacity(IDLE_OPACITY)
        self.setMouseTracking(True)
        self.setMinimumWidth(MIN_WIDTH)

        self._height = height if height in HEIGHTS else DEFAULT_HEIGHT
        self.resize(max(MIN_WIDTH, width), self._height)
        if position is not None:
            self.move(position)

        # --- visual ---
        self._movie: QMovie | None = None
        self._visual_path: Path | None = None
        self._still: QPixmap | None = None
        self._blur: QPixmap | None = None
        self._blur_token = -1
        # Everything that is constant within one GIF frame — the stretched
        # backdrop, both scrim gradients, the crisp panel — composited once and
        # blitted thereafter. Rebuilding it per paint cost 34% of a core while
        # playing, because the spectrum drives ~20 repaints a second and each
        # one re-ran a smooth upscale and two gradient fills over the full strip.
        self._background: QPixmap | None = None
        self._background_token: tuple | None = None
        # The scrim gradients depend only on the layout, so they are rendered
        # once per resize rather than twice per frame.
        self._scrim: QPixmap | None = None
        self._scrim_size: QSize | None = None
        self._frame_clock = QElapsedTimer()
        self._frame_clock.start()
        self._composite_frame = -1
        # Gradient brush for the spectrum bars. Rebuilt only when the strip is
        # laid out, not per frame — it depends solely on geometry.
        self._spectrum_brush: QBrush | None = None
        self._docked = False
        self._dock_edge = "top"
        self._taskbar_autohide = False
        self._wallpaper_on = False

        # --- playback state ---
        self._title = ""
        self._artist = ""
        self._position_ms = 0
        self._duration_ms = 0
        self._playing = False
        self._volume = -1
        self._bands: list[float] = []
        self._peaks: list[float] = []

        # --- interaction ---
        self._buttons: dict[str, QRect] = {}
        self._art_rect = QRect()
        self._spectrum_rect = QRect()
        self._seek_rect = QRect()
        self._text_left = 12
        self._hover_key: str | None = None
        self._pressed_key: str | None = None
        self._drag_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        self._dragging = False
        self._resizing: str | None = None
        self._resize_start: tuple[QRect, QPoint] | None = None
        self._chrome = 0.0
        self._chrome_target = 0.0

        self._fade = QTimer(self)
        self._fade.setInterval(FADE_TICK_MS)
        self._fade.timeout.connect(self._step_fade)

        self._volume_flash = QTimer(self)
        self._volume_flash.setSingleShot(True)
        self._volume_flash.setInterval(VOLUME_FLASH_MS)
        self._volume_flash.timeout.connect(self._clear_volume)

        self._relayout()

    # ------------------------------------------------------------------
    # visual
    # ------------------------------------------------------------------

    def set_visual(self, path: Path | None, fallback: QPixmap | None = None) -> None:
        self._still = fallback
        if path is not None and self._visual_path == path and self._movie is not None:
            return
        self._visual_path = path
        self._teardown_movie()
        self._blur = None
        self._blur_token = -1
        self._background = None
        self._background_token = None

        if path is not None and path.is_file():
            movie = QMovie(str(path))
            if movie.isValid():
                # CacheAll, not CacheNone. Frames are decoded at panel size
                # (~167x94), so a whole loop caches for a couple of MB — and
                # without it Qt re-decodes the GIF 20+ times a second, which
                # profiling showed was the single largest cost in the app while
                # playing, dwarfing every line of Python in it.
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
        # The backdrop and the composited background are both derived from the
        # frame, so both are invalidated with it. Rebuilding lazily in paint
        # keeps it to one composite per frame actually drawn.
        if self._frame_clock.elapsed() < MIN_COMPOSITE_MS:
            return  # a later frame will carry the update
        self._frame_clock.restart()
        # Pin the frame the composite is allowed to use. The cache token is
        # keyed on *this*, not on the movie's live frame number — otherwise a
        # spectrum repaint 20 times a second sees a moved frame counter and
        # rebuilds the whole background anyway, which made the throttle above
        # do nothing at all.
        self._composite_frame = (
            self._movie.currentFrameNumber() if self._movie is not None else -1
        )
        self._blur_token = -1
        self._background_token = None
        self.update()

    def _source_size(self) -> QSize:
        if self._movie is None:
            return QSize()
        size = self._movie.currentImage().size()
        if size.isEmpty():
            self._movie.jumpToFrame(0)
            size = self._movie.currentImage().size()
        return size

    def _apply_scale(self) -> None:
        """Decode frames at *panel* size, not strip size.

        This is the change that made the docked ribbon affordable: covering a
        1920px strip from a 500px GIF decoded 1920x1920 frames to display a
        96px band of them. The panel needs ~171x96, so that is what gets
        decoded, and the blurred backdrop is derived from the same pixmap.
        """
        if self._movie is None:
            return
        source = self._source_size()
        if source.isEmpty():
            return
        target = self._art_size()
        scale = max(
            target.width() / max(1, source.width()),
            target.height() / max(1, source.height()),
        )
        scale = min(scale, ART_MAX_UPSCALE)
        self._movie.setScaledSize(
            QSize(
                max(1, round(source.width() * scale)),
                max(1, round(source.height() * scale)),
            )
        )

    def _art_size(self) -> QSize:
        height = max(1, self.height() - SEEK_H)
        width = min(
            round(height * ART_ASPECT),
            max(height, int(self.width() * ART_WIDTH_FRACTION)),
        )
        return QSize(max(1, width), height)

    def _build_spectrum_brush(self) -> None:
        """Vertical gradient spanning the spectrum, cached until the next layout.

        A gradient brush covers the whole batched drawRects call, so tall bars
        reach the lifted end of the ramp and short ones stay in the slate —
        height reads as brightness without costing a second draw pass.
        """
        rect = self._spectrum_rect
        if rect.isEmpty():
            self._spectrum_brush = None
            return
        gradient = QLinearGradient(
            float(rect.left()), float(rect.bottom()),
            float(rect.left()), float(rect.top()),
        )
        low = QColor(theme.SPECTRUM_LO)
        low.setAlpha(200)
        high = QColor(theme.SPECTRUM_HI)
        high.setAlpha(225)
        gradient.setColorAt(0.0, low)
        gradient.setColorAt(1.0, high)
        self._spectrum_brush = QBrush(gradient)

    def _current_frame(self) -> QPixmap | None:
        if self._movie is not None:
            pixmap = self._movie.currentPixmap()
            if not pixmap.isNull():
                return honour_scaled_size(self._movie, pixmap)
        if self._still is not None and not self._still.isNull():
            return self._still
        return None

    def _backdrop(self) -> QPixmap | None:
        """A tiny, blurred copy of the current frame, cached per frame.

        Squeezing to BLUR_WIDTH and letting drawPixmap stretch it back out is
        the whole blur: no filter pass, no full-size intermediate, and the
        smooth upscale of a 48px image is exactly the soft wash we want.
        """
        frame = self._current_frame()
        if frame is None:
            return None
        # Keyed on the pinned composite frame for the same reason as the
        # background: the live counter would defeat the throttle.
        token = self._composite_frame
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

    # ------------------------------------------------------------------
    # playback state
    # ------------------------------------------------------------------

    def set_now_playing(self, title: str, artist: str) -> None:
        self._title = title
        self._artist = artist
        self.update()

    def set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        if not playing:
            self._bands = []
            self._peaks = []
        self._sync_motion()
        self.update()

    def set_progress(self, position_ms: int, duration_ms: int) -> None:
        """Position ticks arrive ~10/s but rarely change any pixel.

        Repainting on each one was most of the idle-ish cost while playing.
        Only redraw when the seek fill actually moves a pixel or the clock
        text changes, and then only those two regions.
        """
        previous_clock = format_time(self._position_ms)
        previous_fill = self._fill_px()
        self._position_ms = position_ms
        self._duration_ms = duration_ms

        if self._fill_px() != previous_fill:
            self.update(self._seek_rect)
        if format_time(position_ms) != previous_clock:
            self.update(self._text_region())

    def _fill_px(self) -> int:
        if self._duration_ms <= 0:
            return 0
        ratio = max(0.0, min(1.0, self._position_ms / self._duration_ms))
        return int(self.width() * ratio)

    def _text_region(self) -> QRect:
        limit = (
            self._spectrum_rect.left()
            if not self._spectrum_rect.isEmpty()
            else self._buttons["prev"].left()
        )
        return QRect(
            self._text_left, 0, max(1, limit - self._text_left), self.height()
        )

    def set_spectrum(self, bands: list) -> None:
        if not self.isVisible():
            return
        self._bands = list(bands)
        if len(self._peaks) != len(self._bands):
            self._peaks = list(self._bands)
        else:
            for index, value in enumerate(self._bands):
                # Peak caps rise instantly and sink slowly — the classic
                # analyser read, and it gives the eye something stable to
                # measure the moving bars against.
                self._peaks[index] = (
                    value if value >= self._peaks[index]
                    else max(value, self._peaks[index] - PEAK_FALL)
                )
        # Only the bars changed. Repainting the whole strip re-blitted the
        # full-width background pixmap 20 times a second for nothing.
        if not self._spectrum_rect.isEmpty():
            self.update(self._spectrum_rect)

    def set_volume(self, percent: int) -> None:
        self._volume = percent
        self._volume_flash.start()
        self.update()

    def _clear_volume(self) -> None:
        self._volume = -1
        self.update()

    @property
    def shows_spectrum(self) -> bool:
        return not self._spectrum_rect.isEmpty()

    @staticmethod
    def _bar_count(width: int, bands: int) -> int:
        """How many bars to draw in `width`, aiming for BAR_TARGET_PITCH each."""
        room = max(1, (width + BAR_GAP) // (BAR_MIN_W + BAR_GAP))
        wanted = max(1, width // BAR_TARGET_PITCH)
        return max(1, min(room, wanted, BAR_MAX_COUNT))

    # ------------------------------------------------------------------
    # the motion contract
    # ------------------------------------------------------------------

    def _sync_motion(self) -> None:
        """Stop the movie outright whenever nothing should be moving.

        setPaused(False) on a movie that should not run is the bug this method
        exists to prevent: a paused-but-running QMovie still decodes.
        """
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
        self._sync_motion()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._sync_motion()

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        return QSize(max(MIN_WIDTH, self.width()), self._height)

    @property
    def preset_height(self) -> int:
        return self._height

    @property
    def docked(self) -> bool:
        return self._docked

    @property
    def dock_edge(self) -> str:
        return self._dock_edge

    def set_docked(self, docked: bool, edge: str = "top") -> None:
        self._docked = docked
        if docked:
            self._dock_edge = edge

    @property
    def taskbar_autohide(self) -> bool:
        return self._taskbar_autohide

    def set_taskbar_autohide(self, on: bool) -> None:
        self._taskbar_autohide = on

    def set_wallpaper_on(self, on: bool) -> None:
        self._wallpaper_on = on

    def set_height(self, height: int) -> None:
        if height == self._height:
            return
        self._height = height
        self.resize(self.width(), height)
        self.clamp_to_screen()
        self.height_changed.emit(height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()
        self._apply_scale()
        self._blur_token = -1
        self._background_token = None
        self._scrim = None
        self._scrim_size = None

    def _relayout(self) -> None:
        width, height = self.width(), self.height()
        body = height - SEEK_H

        art = self._art_size()
        self._art_rect = QRect(0, 0, art.width(), body)

        glyph = 11 if height <= 56 else 13
        button = glyph + 14
        gap = 4
        row_width = button * 3 + gap * 2
        left = width - row_width - 12
        top = (body - button) // 2
        self._buttons = {
            "prev": QRect(left, top, button, button),
            "play": QRect(left + button + gap, top, button, button),
            "next": QRect(left + (button + gap) * 2, top, button, button),
        }
        corner = 18
        self._buttons["stage"] = QRect(width - corner - 6, 4, corner, corner)

        self._text_left = self._art_rect.right() + 14

        # Text claims what it needs, the spectrum takes whatever is left before
        # the transport. On a docked 1920px strip that is most of the bar,
        # which is the point — the wide layout exists so the middle isn't dead.
        text_budget = min(360, max(120, int(width * 0.26)))
        spectrum_left = self._text_left + text_budget + 18
        spectrum_right = self._buttons["prev"].left() - 16
        if spectrum_right - spectrum_left >= MIN_SPECTRUM_W:
            # Bottom-anchored, not centred: bars need a baseline to grow from
            # or they read as floating rectangles.
            band_h = max(12, min(int(body * 0.66), body - 10))
            self._spectrum_rect = QRect(
                spectrum_left,
                body - band_h - 4,
                spectrum_right - spectrum_left,
                band_h,
            )
        else:
            self._spectrum_rect = QRect()
        self._build_spectrum_brush()

        self._seek_rect = QRect(0, height - SEEK_H, width, SEEK_H)

    def _key_at(self, pos: QPoint) -> str | None:
        for key, rect in self._buttons.items():
            if rect.contains(pos):
                return key
        if self._seek_rect.adjusted(0, -6, 0, 0).contains(pos):
            return "seek"
        return None

    def _edge_at(self, pos: QPoint) -> str | None:
        if self._docked:
            return None
        if pos.x() <= RESIZE_GRIP_PX:
            return "left"
        if pos.x() >= self.width() - RESIZE_GRIP_PX:
            return "right"
        return None

    def _available(self):
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def clamp_to_screen(self) -> None:
        area = self._available()
        if area is None:
            return
        geometry = self.frameGeometry()
        x = min(max(geometry.x(), area.left()), area.right() - geometry.width() + 1)
        y = min(max(geometry.y(), area.top()), area.bottom() - geometry.height() + 1)
        self.move(x, y)

    def snap_to_edges(self) -> None:
        area = self._available()
        if area is None:
            return
        geometry = self.frameGeometry()
        x, y = geometry.x(), geometry.y()
        if abs(x - area.left()) <= SNAP_PX:
            x = area.left()
        elif abs(geometry.right() - area.right()) <= SNAP_PX:
            x = area.right() - geometry.width() + 1
        if abs(y - area.top()) <= SNAP_PX:
            y = area.top()
        elif abs(geometry.bottom() - area.bottom()) <= SNAP_PX:
            y = area.bottom() - geometry.height() + 1
        self.move(x, y)

    # ------------------------------------------------------------------
    # hover fade
    # ------------------------------------------------------------------

    def enterEvent(self, event) -> None:
        self._chrome_target = 1.0
        self._fade.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_key = None
        self._chrome_target = 0.0
        self._fade.start()
        self.unsetCursor()
        super().leaveEvent(event)

    def _step_fade(self) -> None:
        step = FADE_TICK_MS / FADE_MS
        if self._chrome < self._chrome_target:
            self._chrome = min(self._chrome_target, self._chrome + step)
        else:
            self._chrome = max(self._chrome_target, self._chrome - step)
        if abs(self._chrome - self._chrome_target) < 0.001:
            self._chrome = self._chrome_target
            self._fade.stop()
        self.setWindowOpacity(IDLE_OPACITY + (1.0 - IDLE_OPACITY) * self._chrome)
        self.update()

    # ------------------------------------------------------------------
    # mouse
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()

        edge = self._edge_at(pos)
        if edge is not None:
            self._resizing = edge
            self._resize_start = (self.geometry(), event.globalPosition().toPoint())
            event.accept()
            return

        key = self._key_at(pos)
        if key == "seek":
            self._emit_seek(pos)
            event.accept()
            return
        if key is not None:
            self._pressed_key = key
            self.update()
            event.accept()
            return

        global_pos = event.globalPosition().toPoint()
        self._press_global = global_pos
        self._drag_offset = global_pos - self.frameGeometry().topLeft()
        self._dragging = False
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        global_pos = event.globalPosition().toPoint()

        if self._resizing is not None and self._resize_start is not None:
            self._apply_resize(global_pos)
            event.accept()
            return

        if self._drag_offset is not None and self._press_global is not None:
            if (
                not self._dragging
                and (global_pos - self._press_global).manhattanLength()
                >= DRAG_THRESHOLD_PX
            ):
                self._dragging = True
            if self._dragging:
                # A docked strip is owned by the OS work area; dragging it would
                # desynchronise the reserved rectangle from the window.
                if not self._docked:
                    self.move(global_pos - self._drag_offset)
                event.accept()
                return

        if self._edge_at(pos) is not None:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.unsetCursor()

        key = self._key_at(pos)
        if key != self._hover_key:
            self._hover_key = key
            self.update()
        super().mouseMoveEvent(event)

    def _apply_resize(self, global_pos: QPoint) -> None:
        start_geometry, start_pos = self._resize_start
        delta = global_pos.x() - start_pos.x()
        if self._resizing == "right":
            self.resize(max(MIN_WIDTH, start_geometry.width() + delta), self._height)
        else:
            width = max(MIN_WIDTH, start_geometry.width() - delta)
            self.setGeometry(
                start_geometry.right() - width + 1, start_geometry.y(),
                width, self._height,
            )

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        pos = event.position().toPoint()

        if self._resizing is not None:
            self._resizing = None
            self._resize_start = None
            self.geometry_settled.emit()
            event.accept()
            return

        if self._pressed_key is not None:
            key, self._pressed_key = self._pressed_key, None
            self.update()
            if self._buttons.get(key, QRect()).contains(pos):
                {
                    "prev": self.prev_requested,
                    "play": self.play_pause_requested,
                    "next": self.next_requested,
                    "stage": self.stage_requested,
                }[key].emit()
        elif self._dragging:
            self.snap_to_edges()
            self.geometry_settled.emit()

        self._drag_offset = None
        self._press_global = None
        self._dragging = False
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.stage_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.volume_delta.emit(5 if delta > 0 else -5)
            event.accept()
            return
        super().wheelEvent(event)

    def _emit_seek(self, pos: QPoint) -> None:
        if self.width() > 0:
            self.seek_requested.emit(max(0.0, min(1.0, pos.x() / self.width())))

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)

        height_menu = menu.addMenu("Height")
        height_actions = {}
        for preset in HEIGHTS:
            action = height_menu.addAction(f"{preset} px")
            action.setCheckable(True)
            action.setChecked(preset == self._height)
            height_actions[action] = preset

        dock_menu = menu.addMenu("Dock")
        dock_actions = {}
        for label, mode in (
            ("Off (floating)", "off"),
            ("Top edge", "top"),
            ("Bottom edge", "bottom"),
        ):
            action = dock_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(
                mode == (self._dock_edge if self._docked else "off")
            )
            dock_actions[action] = mode

        dock_menu.addSeparator()
        autohide_action = dock_menu.addAction("Auto-hide the Windows taskbar")
        autohide_action.setCheckable(True)
        autohide_action.setChecked(self._taskbar_autohide)
        # Changing a global Windows setting from a submenu deserves a warning
        # in the one place someone will actually read it.
        autohide_action.setToolTip(
            "Changes a Windows setting while the ribbon is docked. "
            "Restored when you undock or quit."
        )
        dock_menu.setToolTipsVisible(True)

        wallpaper_action = menu.addAction("Show visual as wallpaper")
        wallpaper_action.setCheckable(True)
        wallpaper_action.setChecked(self._wallpaper_on)

        reshuffle_action = menu.addAction("Reshuffle visuals")

        menu.addSeparator()
        search_action = menu.addAction("Search…\tCtrl+Alt+Space")
        stage_action = menu.addAction("Open stage\tF11")
        menu.addSeparator()
        quit_action = menu.addAction("Quit")

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen in height_actions:
            self.set_height(height_actions[chosen])
        elif chosen in dock_actions:
            self.dock_requested.emit(dock_actions[chosen])
        elif chosen is autohide_action:
            self.taskbar_autohide_toggled.emit(not self._taskbar_autohide)
        elif chosen is wallpaper_action:
            self.wallpaper_toggled.emit(not self._wallpaper_on)
        elif chosen is reshuffle_action:
            self.reshuffle_requested.emit()
        elif chosen is search_action:
            self.palette_requested.emit()
        elif chosen is stage_action:
            self.stage_requested.emit()
        elif chosen is quit_action:
            self.quit_requested.emit()

    # ------------------------------------------------------------------
    # painting
    # ------------------------------------------------------------------

    def _compose_background(self) -> QPixmap | None:
        """Backdrop + scrim + crisp panel, flattened into one pixmap.

        Rebuilt only when the frame, the size or the visual changes — so at the
        GIF's own rate (~12/s) rather than at the repaint rate (~20-30/s), and
        each repaint becomes a single blit instead of a smooth upscale plus two
        full-width gradient fills.
        """
        size = self.size()
        if size.isEmpty():
            return None
        token = (
            size.width(), size.height(),
            self._composite_frame, id(self._visual_path),
        )
        if self._background is not None and self._background_token == token:
            return self._background

        # Reuse the buffer across frames. Allocating a fresh 1920x96 pixmap on
        # every GIF frame is ~9 MB/s of churn for a surface whose size almost
        # never changes.
        if self._background is not None and self._background.size() == size:
            pixmap = self._background
        else:
            pixmap = QPixmap(size)
        pixmap.fill(QColor(theme.SCREEN))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRect(0, 0, size.width(), size.height())
        self._paint_backdrop(painter, rect)
        scrim = self._scrim_pixmap(size)
        if scrim is not None:
            painter.drawPixmap(0, 0, scrim)
        self._paint_art(painter)
        painter.end()

        self._background = pixmap
        self._background_token = token
        return pixmap

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()

        # Blit only the dirty region. Spectrum updates dirty a middle band, so
        # the full-width background never needs re-blitting for them.
        dirty = event.rect()
        background = self._compose_background()
        if background is not None:
            painter.drawPixmap(dirty, background, dirty)
        else:
            painter.fillRect(dirty, QColor(theme.SCREEN))

        if self._bands and self._spectrum_rect.intersects(dirty):
            self._paint_spectrum(painter)
        if self._text_region().intersects(dirty):
            self._paint_text(painter, rect)
        if any(button.intersects(dirty) for button in self._buttons.values()):
            self._paint_transport(painter)
        if self._seek_rect.intersects(dirty):
            self._paint_seek(painter)
        if self._volume >= 0:
            self._paint_volume(painter, rect)

    def _paint_backdrop(self, painter: QPainter, rect: QRect) -> None:
        painter.fillRect(rect, QColor(theme.SCREEN))
        backdrop = self._backdrop()
        if backdrop is None:
            return
        # Stretched from ~48px, so this is the blur. Aspect is deliberately
        # ignored: it is a colour wash, not a picture, and letterboxing it
        # would put bars in the one layer that exists to have no edges.
        painter.setOpacity(0.55)
        painter.drawPixmap(rect, backdrop)
        painter.setOpacity(1.0)

    def _scrim_pixmap(self, size: QSize) -> QPixmap | None:
        """The scrim, pre-rendered. It only changes when the layout does."""
        if self._scrim is not None and self._scrim_size == size:
            return self._scrim
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self._paint_scrim(painter, QRect(0, 0, size.width(), size.height()))
        painter.end()
        self._scrim = pixmap
        self._scrim_size = size
        return pixmap

    def _paint_scrim(self, painter: QPainter, rect: QRect) -> None:
        """Darken enough that text over the wash is honest, no more."""
        veil = QColor(theme.SCREEN)
        veil.setAlpha(105)
        painter.fillRect(rect, veil)

        # A ramp under the text block, which starts just right of the panel.
        edge = min(rect.width(), self._buttons["prev"].left() - 6)
        if edge > self._text_left:
            ramp = QLinearGradient(self._text_left, 0, edge, 0)
            for stop, alpha in ((0.0, 170), (0.55, 120), (1.0, 40)):
                colour = QColor(theme.SCREEN)
                colour.setAlpha(alpha)
                ramp.setColorAt(stop, colour)
            painter.fillRect(
                QRect(self._text_left, 0, edge - self._text_left, rect.height()), ramp
            )

        transport_left = max(0, self._buttons["prev"].left() - 40)
        side = QLinearGradient(transport_left, 0, rect.width(), 0)
        start = QColor(theme.SCREEN)
        start.setAlpha(0)
        end = QColor(theme.SCREEN)
        end.setAlpha(185)
        side.setColorAt(0.0, start)
        side.setColorAt(1.0, end)
        painter.fillRect(
            QRect(transport_left, 0, rect.width() - transport_left, rect.height()), side
        )

    def _paint_art(self, painter: QPainter) -> None:
        """The crisp panel: cover-crop of a frame decoded at panel size."""
        frame = self._current_frame()
        rect = self._art_rect
        if frame is None or rect.isEmpty():
            return
        painter.save()
        painter.setClipRect(rect)
        x = rect.x() + (rect.width() - frame.width()) // 2
        y = rect.y() + (rect.height() - frame.height()) // 2
        painter.drawPixmap(x, y, frame)
        painter.restore()

        # A hairline on the inner edge so the panel reads as a deliberate
        # element rather than as the blur happening to be sharp there.
        edge = QColor(theme.BORDER_HI)
        edge.setAlpha(140)
        painter.fillRect(QRect(rect.right(), rect.y(), 1, rect.height()), edge)

    def _paint_spectrum(self, painter: QPainter) -> None:
        rect = self._spectrum_rect
        bands = self._bands
        if not bands or rect.isEmpty():
            return

        peaks = self._peaks or [0.0] * len(bands)
        target = self._bar_count(rect.width(), len(bands))
        if target < len(bands):
            # Downsample by peak, not by average: averaging flattens exactly
            # the transients the display exists to show.
            bands = _reduce_max(bands, target)
            peaks = _reduce_max(peaks, target)
        elif target > len(bands):
            bands = _interpolate(bands, target)
            peaks = _interpolate(peaks, target)

        count = len(bands)
        span = (rect.width() + BAR_GAP) / count
        bar_w = max(BAR_MIN_W, int(span) - BAR_GAP)
        baseline = rect.bottom()
        height = rect.height()
        left = rect.x()

        # Collect first, then draw in two batched calls. Ninety-six bars plus
        # ninety-six caps is 192 individual drawRect calls, and each one is a
        # separate Python-to-C++ crossing — measured at 10% of a core on a
        # docked strip. drawRects hands the whole list over once.
        body_rects = []
        cap_rects = []
        for index in range(count):
            x = left + int(index * span)
            bar_h = max(1, int(bands[index] * height))
            body_rects.append(QRect(x, baseline - bar_h, bar_w, bar_h))
            peak = peaks[index] if index < len(peaks) else 0.0
            if peak > 0.02:
                cap_rects.append(
                    QRect(x, baseline - max(2, int(peak * height)), bar_w, 2)
                )

        painter.setPen(Qt.PenStyle.NoPen)
        if self._spectrum_brush is None:
            self._build_spectrum_brush()
        painter.setBrush(self._spectrum_brush or QColor(theme.SPECTRUM_HI))
        painter.drawRects(body_rects)

        if cap_rects:
            # Caps stay off the gradient so they read as a separate mark, and
            # dim rather than change hue when paused — see theme.SPECTRUM_CAP
            # for why none of this is cyan any more.
            cap = QColor(theme.SPECTRUM_CAP if self._playing else theme.SUBDUED)
            cap.setAlpha(190)
            painter.setBrush(cap)
            painter.drawRects(cap_rects)

    def _paint_text(self, painter: QPainter, rect: QRect) -> None:
        if not self._title:
            return
        limit = (
            self._spectrum_rect.left() - 14
            if not self._spectrum_rect.isEmpty()
            else self._buttons["prev"].left() - 14
        )
        available = limit - self._text_left
        if available < 60:
            return

        short = rect.height() <= 56
        show_artist = rect.width() >= WIDTH_FOR_ARTIST and bool(self._artist)
        show_clock = rect.width() >= WIDTH_FOR_CLOCK
        clock = f"{format_time(self._position_ms)} / {format_time(self._duration_ms)}"

        title_font = QFont(self.font())
        title_font.setPixelSize(12 if short else 13)
        title_font.setWeight(QFont.Weight.DemiBold)

        lines: list[tuple[str, QFont, QFontMetrics, str]] = [
            (self._title, title_font, QFontMetrics(title_font), theme.TEXT)
        ]
        secondary = QFont(self.font())
        secondary.setPixelSize(10 if short else 11)
        if short:
            parts = [
                text for text in (
                    self._artist if show_artist else "",
                    clock if show_clock else "",
                ) if text
            ]
            if parts:
                lines.append(
                    (" · ".join(parts), secondary, QFontMetrics(secondary), theme.MUTED)
                )
        else:
            if show_artist:
                lines.append(
                    (self._artist, secondary, QFontMetrics(secondary), theme.MUTED)
                )
            if show_clock:
                clock_font = QFont(self.font())
                clock_font.setPixelSize(10)
                lines.append(
                    (clock, clock_font, QFontMetrics(clock_font), theme.SUBDUED)
                )

        block = sum(metrics.height() for _, _, metrics, _ in lines)
        y = (rect.height() - SEEK_H - block) // 2

        for text, font, metrics, colour in lines:
            painter.setFont(font)
            painter.setPen(QColor(colour))
            painter.drawText(
                self._text_left,
                y + metrics.ascent(),
                metrics.elidedText(text, Qt.TextElideMode.ElideRight, available),
            )
            y += metrics.height()

    def _paint_transport(self, painter: QPainter) -> None:
        glyphs = {
            "prev": "◀◀",
            "play": "▮▮" if self._playing else "▶",
            "next": "▶▶",
            "stage": "⛶",
        }
        base_alpha = 0.6 + 0.4 * self._chrome

        for key, rect in self._buttons.items():
            if key == "stage" and self._chrome < 0.05:
                continue
            if key in (self._hover_key, self._pressed_key):
                fill = QColor(theme.RAISED_HI)
                fill.setAlpha(200 if key == self._pressed_key else 140)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawRoundedRect(rect, 4, 4)

            font = QFont(self.font())
            font.setPixelSize(
                10 if key == "stage" else (11 if self.height() <= 56 else 12)
            )
            painter.setFont(font)
            if key == "play":
                colour = QColor(theme.VIOLET_HI if key == self._hover_key else theme.TEXT)
            else:
                colour = QColor(theme.TEXT if key == self._hover_key else theme.MUTED)
            colour.setAlphaF(base_alpha if key != "stage" else self._chrome)
            painter.setPen(colour)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyphs[key])

    def _paint_seek(self, painter: QPainter) -> None:
        rect = self._seek_rect
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BORDER))
        painter.drawRect(rect)
        if self._duration_ms <= 0:
            return
        ratio = max(0.0, min(1.0, self._position_ms / self._duration_ms))
        filled = int(rect.width() * ratio)
        painter.setBrush(QColor(theme.VIOLET))
        painter.drawRect(QRect(rect.x(), rect.y(), filled, rect.height()))
        # Cyan means live audio and nothing else — here that is the playhead.
        if self._playing:
            painter.setBrush(QColor(theme.CYAN))
            painter.drawRect(
                QRect(rect.x() + max(0, filled - 2), rect.y(), 2, rect.height())
            )

    def _paint_volume(self, painter: QPainter, rect: QRect) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BORDER))
        painter.drawRect(QRect(0, 0, rect.width(), SEEK_H))
        painter.setBrush(QColor(theme.VIOLET_HI))
        painter.drawRect(
            QRect(0, 0, int(rect.width() * max(0, min(100, self._volume)) / 100), SEEK_H)
        )

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        # The app runs with quitOnLastWindowClosed disabled so that closing the
        # stage doesn't kill it. That makes closing *this* window the explicit
        # quit gesture — otherwise Alt+F4 would leave a headless process behind.
        self.quit_requested.emit()
        event.ignore()

    def shutdown(self) -> None:
        self._teardown_movie()
