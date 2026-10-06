"""A corner-sized window that keeps the visual and drops everything else.

Focus mode collapses the *content* of the main window but not its footprint:
the transport row, seek bar and title bar hold it to 720x460 (ui/chrome.py),
which is still a large hole in the middle of a screen you are trying to work
on. This is the smaller state — one window roughly the size of an app icon,
containing nothing but the looping visual.

Three rules shape it:

  * It borrows the main window's ArtPanel by reparenting rather than building a
    second one. One QMovie, one decode, no duplicated state, and the visual the
    track was already showing simply carries over mid-frame.
  * Nothing resizes on hover. Restoring the full player is the most expensive
    transition in the app, so it is bound only to deliberate gestures — the
    restore glyph, double-click, Ctrl+M — and never to the pointer happening to
    pass over the window on its way somewhere else. Hover fades in the
    transport, which is cheap, reversible, and almost always what you actually
    wanted when you moved there.
  * Controls are painted, not laid out. styles.qss gives every QPushButton a
    60px minimum width, which is half this window; hand-painted glyphs sidestep
    that fight the same way the sliders and level meter already do.

Cost is *lower* here than in the main window: ArtPanel._fill_size decodes
frames to the panel, so a 128px mini window decodes 128px frames instead of
running against the 720px cap.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QShortcut,
)
from PySide6.QtWidgets import QApplication, QMenu, QVBoxLayout, QWidget

from ui import theme
from ui.art_panel import ArtPanel
from ui.chrome import apply_always_on_top

# Offered sizes, in px square. 96 is about a taskbar icon; 240 is roughly the
# art column of the full window, for when you want to actually watch it.
SIZES = (96, 128, 176, 240)
DEFAULT_SIZE = 128

# Long enough that sweeping the cursor across on the way to something else
# doesn't flash the transport, short enough to feel immediate when you meant it.
HOVER_DWELL_MS = 250

FADE_MS = 160
FADE_TICK_MS = 16

# Idle, the window sits slightly translucent so it reads as an overlay on the
# work behind it rather than a hole punched in it.
IDLE_OPACITY = 0.86

SEEK_H = 3
SNAP_PX = 24
DRAG_THRESHOLD_PX = 4
VOLUME_FLASH_MS = 900


class _MiniControls(QWidget):
    """Full-window overlay: the hover transport, and every mouse gesture.

    Covering the whole window rather than just a bottom strip means the panel
    underneath never sees a mouse event, so ArtPanel needs no idea that mini
    mode exists. The overlay declines to paint a background, so the visual
    shows through untouched until it has something to say.
    """

    play_pause_requested = Signal()
    next_requested = Signal()
    prev_requested = Signal()
    restore_requested = Signal()
    seek_requested = Signal(float)
    volume_delta = Signal(int)
    menu_requested = Signal(QPoint)

    def __init__(self, host: "MiniPlayer") -> None:
        super().__init__(host)
        self._host = host
        self.setMouseTracking(True)
        # No background of its own — the QSS `QWidget { background-color }`
        # rule doesn't reach an unstyled QWidget subclass, and autoFillBackground
        # is off by default, so the sibling panel below shows through.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        self._opacity = 0.0
        self._target = 0.0
        self._position_ms = 0
        self._duration_ms = 0
        self._playing = False
        self._title = ""
        self._artist = ""
        self._volume = -1
        self._glyph_px = 14

        self._buttons: dict[str, QRect] = {}
        self._seek_rect = QRect()
        self._hover_key: str | None = None
        self._pressed_key: str | None = None
        self._drag_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        self._dragging = False

        self._dwell = QTimer(self)
        self._dwell.setSingleShot(True)
        self._dwell.setInterval(HOVER_DWELL_MS)
        self._dwell.timeout.connect(lambda: self._set_target(1.0))

        self._fade = QTimer(self)
        self._fade.setInterval(FADE_TICK_MS)
        self._fade.timeout.connect(self._step_fade)

        # A bare click means play/pause, but a double-click means restore — and
        # a double-click always delivers a single click first. Holding the
        # single-click for the system double-click interval is what stops every
        # restore from also pausing the track.
        self._click = QTimer(self)
        self._click.setSingleShot(True)
        self._click.setInterval(QApplication.doubleClickInterval())
        self._click.timeout.connect(self.play_pause_requested)

        self._volume_flash = QTimer(self)
        self._volume_flash.setSingleShot(True)
        self._volume_flash.setInterval(VOLUME_FLASH_MS)
        self._volume_flash.timeout.connect(self._clear_volume)

    # --- state ---

    def set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.update()

    def set_now_playing(self, title: str, artist: str) -> None:
        self._title = title
        self._artist = artist
        if self._opacity > 0.0:
            self.update()

    def set_progress(self, position_ms: int, duration_ms: int) -> None:
        self._position_ms = position_ms
        self._duration_ms = duration_ms
        # Position ticks arrive several times a second whether or not anything
        # is on screen to show them.
        if self._opacity > 0.0:
            self.update()

    def set_volume(self, percent: int) -> None:
        """Flash a volume readout — the wheel is otherwise a blind control."""
        self._volume = percent
        self._volume_flash.start()
        self.update()

    def _clear_volume(self) -> None:
        self._volume = -1
        self.update()

    def reset(self) -> None:
        self._dwell.stop()
        self._click.stop()
        self._fade.stop()
        self._opacity = self._target = 0.0
        self._hover_key = self._pressed_key = None
        self._dragging = False
        self._drag_offset = None
        self._host.setWindowOpacity(IDLE_OPACITY)

    # --- fade ---

    def _set_target(self, target: float) -> None:
        self._target = target
        if abs(self._target - self._opacity) > 0.001:
            self._fade.start()

    def _step_fade(self) -> None:
        step = FADE_TICK_MS / FADE_MS
        if self._opacity < self._target:
            self._opacity = min(self._target, self._opacity + step)
        else:
            self._opacity = max(self._target, self._opacity - step)
        if abs(self._opacity - self._target) < 0.001:
            self._opacity = self._target
            self._fade.stop()
        # Opaque while you are working on it, translucent once you leave.
        self._host.setWindowOpacity(
            IDLE_OPACITY + (1.0 - IDLE_OPACITY) * self._opacity
        )
        self.update()

    def enterEvent(self, event) -> None:
        self._dwell.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._dwell.stop()
        self._hover_key = None
        self._set_target(0.0)
        super().leaveEvent(event)

    # --- geometry ---

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        width, height = self.width(), self.height()
        glyph = max(11, min(20, width // 8))
        button = glyph + 12
        gap = max(4, width // 16)
        row = button * 3 + gap * 2
        left = (width - row) // 2
        top = height - SEEK_H - 8 - button

        self._glyph_px = glyph
        self._buttons = {
            "prev": QRect(left, top, button, button),
            "play": QRect(left + button + gap, top, button, button),
            "next": QRect(left + (button + gap) * 2, top, button, button),
        }
        corner = max(16, min(24, width // 6))
        self._buttons["restore"] = QRect(width - corner - 4, 4, corner, corner)
        self._seek_rect = QRect(0, height - SEEK_H, width, SEEK_H)

    def _key_at(self, pos: QPoint) -> str | None:
        for key, rect in self._buttons.items():
            if rect.contains(pos):
                return key
        if self._seek_rect.adjusted(0, -6, 0, 0).contains(pos):
            return "seek"
        return None

    @property
    def _controls_live(self) -> bool:
        """Whether the transport is solid enough to be a legitimate target."""
        return self._opacity > 0.4

    # --- mouse ---

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        key = self._key_at(pos) if self._controls_live else None
        if key == "seek":
            self._emit_seek(pos)
            event.accept()
            return
        if key is not None:
            self._pressed_key = key
            self.update()
            event.accept()
            return
        # Anywhere else on the visual: a drag if it moves, a click if it doesn't.
        global_pos = event.globalPosition().toPoint()
        self._press_global = global_pos
        self._drag_offset = global_pos - self._host.frameGeometry().topLeft()
        self._dragging = False
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        global_pos = event.globalPosition().toPoint()

        if self._drag_offset is not None and self._press_global is not None:
            if (
                not self._dragging
                and (global_pos - self._press_global).manhattanLength()
                >= DRAG_THRESHOLD_PX
            ):
                self._dragging = True
                self._click.stop()  # this was a drag, not a click
            if self._dragging:
                self._host.move(global_pos - self._drag_offset)
                event.accept()
                return

        key = self._key_at(pos) if self._controls_live else None
        if key != self._hover_key:
            self._hover_key = key
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        pos = event.position().toPoint()

        if self._pressed_key is not None:
            key, self._pressed_key = self._pressed_key, None
            self.update()
            if self._buttons.get(key, QRect()).contains(pos):
                {
                    "prev": self.prev_requested,
                    "play": self.play_pause_requested,
                    "next": self.next_requested,
                    "restore": self.restore_requested,
                }[key].emit()
        elif self._dragging:
            self._host.snap_to_edges()
        elif self._drag_offset is not None:
            # Held for the double-click interval: see _click.
            self._click.start()

        self._drag_offset = None
        self._press_global = None
        self._dragging = False
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._click.stop()
            self.restore_requested.emit()
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

    def contextMenuEvent(self, event) -> None:
        self.menu_requested.emit(event.globalPos())
        event.accept()

    def _emit_seek(self, pos: QPoint) -> None:
        if self.width() > 0:
            self.seek_requested.emit(max(0.0, min(1.0, pos.x() / self.width())))

    # --- painting ---

    def paintEvent(self, event) -> None:
        # _buttons is populated by the first resize; nothing here has geometry
        # to draw against before that.
        if not self._buttons:
            return
        if self._opacity <= 0.01 and self._volume < 0:
            return
        painter = QPainter(self)
        rect = self.rect()

        if self._volume >= 0:
            self._draw_volume(painter, rect)
        if self._opacity <= 0.01:
            return

        painter.setOpacity(self._opacity)
        lines = self._text_lines(rect)
        self._draw_scrim(painter, rect, lines)
        self._draw_text(painter, rect, lines)
        self._draw_buttons(painter)
        self._draw_seek(painter)

    def _text_lines(self, rect: QRect) -> list[tuple[str, QFont, QFontMetrics, str]]:
        """The now-playing lines, top to bottom, or none if there is no room.

        Below ~120px there is no honest way to fit a title over the transport
        without covering the visual entirely, so it doesn't try.
        """
        if rect.width() < 120 or not self._title:
            return []
        lines = [(self._title, 11, theme.TEXT)]
        if rect.width() >= 176 and self._artist:
            lines.append((self._artist, 9, theme.MUTED))

        resolved = []
        for text, size, colour in lines:
            font = QFont(self.font())
            font.setPixelSize(size)
            resolved.append((text, font, QFontMetrics(font), colour))
        return resolved

    def _draw_scrim(self, painter: QPainter, rect: QRect, lines: list) -> None:
        """Two gradients — nothing here is readable over a bright frame.

        The lower one starts above whichever comes first, the text or the
        transport. Anchoring it to the buttons alone leaves the title sitting
        in the transparent head of the gradient, which is exactly as legible as
        no scrim at all.
        """
        top = self._buttons["play"].top()
        if lines:
            top = min(top, self._text_top(rect, lines))
        top = max(0, top - 20)

        # The ramp is front-loaded rather than linear: the text begins about a
        # fifth of the way down, and a linear ramp is still nearly transparent
        # there. The 20px lead-in above is what keeps the near-solid part from
        # reading as a hard band edge.
        gradient = QLinearGradient(0, top, 0, rect.bottom())
        for stop, alpha in ((0.0, 0), (0.22, 210), (1.0, 250)):
            colour = QColor(theme.SCREEN)
            colour.setAlpha(alpha)
            gradient.setColorAt(stop, colour)
        painter.fillRect(QRect(0, top, rect.width(), rect.height() - top), gradient)

        head = self._buttons["restore"].bottom() + 8
        gradient = QLinearGradient(0, 0, 0, head)
        for stop, alpha in ((0.0, 175), (1.0, 0)):
            colour = QColor(theme.SCREEN)
            colour.setAlpha(alpha)
            gradient.setColorAt(stop, colour)
        painter.fillRect(QRect(0, 0, rect.width(), head), gradient)

    @staticmethod
    def _text_pad() -> int:
        return 8

    def _text_top(self, rect: QRect, lines: list) -> int:
        block = sum(metrics.height() for _, _, metrics, _ in lines)
        return self._buttons["play"].top() - 4 - block

    def _draw_text(self, painter: QPainter, rect: QRect, lines: list) -> None:
        if not lines:
            return
        pad = self._text_pad()
        y = self._text_top(rect, lines)
        for text, font, metrics, colour in lines:
            painter.setFont(font)
            painter.setPen(QColor(colour))
            painter.drawText(
                pad,
                y + metrics.ascent(),
                metrics.elidedText(
                    text, Qt.TextElideMode.ElideRight, rect.width() - pad * 2
                ),
            )
            y += metrics.height()

    def _draw_buttons(self, painter: QPainter) -> None:
        glyphs = {
            "prev": "◀◀",
            "play": "▮▮" if self._playing else "▶",
            "next": "▶▶",
            "restore": "❐",
        }
        for key, rect in self._buttons.items():
            if key == self._hover_key or key == self._pressed_key:
                fill = QColor(theme.RAISED_HI)
                fill.setAlpha(200 if key == self._pressed_key else 140)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawRoundedRect(rect, 4, 4)

            font = QFont(self.font())
            font.setPixelSize(
                self._glyph_px if key != "restore" else max(10, self._glyph_px - 3)
            )
            painter.setFont(font)
            # The play button is the one high-emphasis control here too.
            if key == "play":
                colour = theme.VIOLET_HI if key == self._hover_key else theme.TEXT
            else:
                colour = theme.TEXT if key == self._hover_key else theme.MUTED
            painter.setPen(QColor(colour))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyphs[key])

    def _draw_seek(self, painter: QPainter) -> None:
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
        # Cyan is reserved for live audio; here that is the playhead itself.
        painter.setBrush(QColor(theme.CYAN))
        painter.drawRect(QRect(rect.x() + max(0, filled - 2), rect.y(), 2, rect.height()))

    def _draw_volume(self, painter: QPainter, rect: QRect) -> None:
        """Top-edge bar, shown for a moment after the wheel changes volume."""
        painter.setOpacity(1.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BORDER))
        painter.drawRect(QRect(0, 0, rect.width(), SEEK_H))
        painter.setBrush(QColor(theme.VIOLET_HI))
        painter.drawRect(
            QRect(0, 0, int(rect.width() * max(0, min(100, self._volume)) / 100), SEEK_H)
        )


class MiniPlayer(QWidget):
    """Frameless always-on-top window hosting the borrowed ArtPanel."""

    restore_requested = Signal()
    play_pause_requested = Signal()
    next_requested = Signal()
    prev_requested = Signal()
    seek_requested = Signal(float)
    volume_delta = Signal(int)
    size_changed = Signal(int)
    quit_requested = Signal()

    def __init__(self, size: int = DEFAULT_SIZE, position: QPoint | None = None) -> None:
        super().__init__(None)
        self.setWindowTitle("MP3 Player")
        # Parentless top-level rather than a Qt.Tool: the main window is hidden
        # while this is up, so this needs to be the thing the taskbar and
        # Alt+Tab can still reach.
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setObjectName("miniPlayer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowOpacity(IDLE_OPACITY)

        self._panel: ArtPanel | None = None
        self._shutting_down = False
        self._size = size if size in SIZES else DEFAULT_SIZE

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._controls = _MiniControls(self)
        self._controls.play_pause_requested.connect(self.play_pause_requested)
        self._controls.next_requested.connect(self.next_requested)
        self._controls.prev_requested.connect(self.prev_requested)
        self._controls.restore_requested.connect(self.restore_requested)
        self._controls.seek_requested.connect(self.seek_requested)
        self._controls.volume_delta.connect(self.volume_delta)
        self._controls.menu_requested.connect(self._show_menu)

        self.resize(self._size, self._size)
        if position is not None:
            self.move(position)

        # Only reachable while this window actually has keyboard focus, which
        # it usually won't — the transport is the primary control here.
        for sequence, slot in (
            (QKeySequence(Qt.Key.Key_Space), self.play_pause_requested),
            (QKeySequence("Ctrl+M"), self.restore_requested),
            (QKeySequence(Qt.Key.Key_Escape), self.restore_requested),
            (QKeySequence("Ctrl+Right"), self.next_requested),
            (QKeySequence("Ctrl+Left"), self.prev_requested),
        ):
            shortcut = QShortcut(sequence, self)
            shortcut.activated.connect(slot)

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    # --- the borrowed panel ---

    def take_panel(self, panel: ArtPanel) -> None:
        self._panel = panel
        panel.set_mini_mode(True)
        self._layout.addWidget(panel)
        self._controls.setGeometry(self.rect())
        self._controls.raise_()

    def release_panel(self) -> ArtPanel | None:
        """Hand the panel back. The caller re-inserts it into its own layout."""
        panel = self._panel
        if panel is not None:
            self._layout.removeWidget(panel)
        self._panel = None
        self._controls.reset()
        return panel

    # --- forwarded state ---

    def set_playing(self, playing: bool) -> None:
        self._controls.set_playing(playing)

    def set_now_playing(self, title: str, artist: str) -> None:
        self._controls.set_now_playing(title, artist)

    def set_progress(self, position_ms: int, duration_ms: int) -> None:
        self._controls.set_progress(position_ms, duration_ms)

    def set_volume(self, percent: int) -> None:
        self._controls.set_volume(percent)

    # --- window ---

    def appear(self) -> None:
        self.clamp_to_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        # Must follow show(): the Win32 path in apply_always_on_top needs a
        # native window to re-order.
        apply_always_on_top(self, True)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._controls.setGeometry(self.rect())
        self._controls.raise_()

    def set_size(self, size: int) -> None:
        if size == self._size:
            return
        self._size = size
        # Grow from the same top-left, then pull back inside the screen if that
        # pushed it off the edge it was parked against.
        self.resize(size, size)
        self.clamp_to_screen()
        self.size_changed.emit(size)

    @property
    def preset_size(self) -> int:
        return self._size

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
        """Magnetic screen edges — corners are where this wants to live."""
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

    def _show_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        size_menu = menu.addMenu("Size")
        size_actions = {}
        for size in SIZES:
            action = size_menu.addAction(f"{size} px")
            action.setCheckable(True)
            action.setChecked(size == self._size)
            size_actions[action] = size

        menu.addSeparator()
        restore_action = menu.addAction("Restore full player\tCtrl+M")
        quit_action = menu.addAction("Quit")

        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen in size_actions:
            self.set_size(size_actions[chosen])
        elif chosen is restore_action:
            self.restore_requested.emit()
        elif chosen is quit_action:
            self.quit_requested.emit()

    def shutdown(self) -> None:
        """Close for real — closeEvent otherwise refuses and restores instead."""
        self._shutting_down = True
        self.close()

    def closeEvent(self, event) -> None:
        # Alt+F4 here would otherwise take the app down with it while the main
        # window is hidden, skipping its closeEvent and losing the session's
        # settings. Treat it as "give me the full player back".
        if not self._shutting_down:
            event.ignore()
            self.restore_requested.emit()
            return
        super().closeEvent(event)
