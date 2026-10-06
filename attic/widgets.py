"""Custom-painted controls.

Stock Qt widgets are a large part of what makes a themed app still look like a
themed app, so the two controls you look at most — the seek bar and the volume
bar — are painted by hand as neon segments instead of being QSS-skinned
QSliders.
"""

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter
from PySide6.QtWidgets import QLabel, QSlider, QStyle

from ui import theme


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "?:??"
    return f"{seconds // 60}:{seconds % 60:02d}"


def set_display_font(
    widget,
    size: int | None = None,
    spacing: float = 1.6,
    bold: bool = False,
) -> None:
    """Apply letter-spaced display type.

    Qt Style Sheets support neither `letter-spacing` nor `text-transform`, so
    tracking has to be set on the QFont in code (and callers uppercase their own
    strings).
    """
    font = widget.font()
    if size is not None:
        font.setPixelSize(size)
    if bold:
        font.setWeight(QFont.Weight.Bold)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    widget.setFont(font)


class ClickSlider(QSlider):
    """QSlider that jumps straight to the clicked position.

    Stock QSlider only pages toward a click; for a seek bar you want the
    handle under the cursor immediately. Moving the handle before passing the
    event on also means the subsequent drag grabs it, so sliderPressed /
    sliderMoved / sliderReleased fire exactly as if the user grabbed the
    handle directly.
    """

    def mousePressEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.maximum() > self.minimum()
        ):
            value = QStyle.sliderValueFromPosition(
                self.minimum(),
                self.maximum(),
                int(event.position().x()),
                self.width(),
            )
            self.setValue(value)
        super().mousePressEvent(event)


class SegmentedSlider(ClickSlider):
    """Horizontal slider drawn as discrete lit segments.

    Elapsed segments run violet (dim -> bright), the segment under the playhead
    burns cyan, and the remainder sits dark. Hovering marks the position you'd
    seek to. Everything is flat rects, so a repaint is cheap enough to run at
    playback tick rate.
    """

    def __init__(
        self,
        orientation=Qt.Orientation.Horizontal,
        parent=None,
        segment: int = 5,
        gap: int = 3,
        bar_height: int = 12,
    ) -> None:
        super().__init__(orientation, parent)
        self._segment = segment
        self._gap = gap
        self._bar_height = bar_height
        self._hover_x: int | None = None
        self.setMouseTracking(True)
        self.setFixedHeight(bar_height + 8)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setHeight(self._bar_height + 8)
        return hint

    def _ratio(self) -> float:
        span = self.maximum() - self.minimum()
        if span <= 0:
            return 0.0
        return (self.value() - self.minimum()) / span

    def mouseMoveEvent(self, event) -> None:
        self._hover_x = int(event.position().x())
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_x = None
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)

        width = self.width()
        top = (self.height() - self._bar_height) // 2
        pitch = self._segment + self._gap
        count = max(1, width // pitch)
        lit = int(round(self._ratio() * count))
        hover_index = (
            None if self._hover_x is None else max(0, min(count - 1, self._hover_x // pitch))
        )

        # Elapsed segments brighten toward the playhead so the bar reads as a
        # charge meter rather than a flat fill.
        gradient = QLinearGradient(0, 0, max(1, lit * pitch), 0)
        gradient.setColorAt(0.0, QColor(theme.VIOLET_DIM))
        gradient.setColorAt(1.0, QColor(theme.VIOLET_HI))

        dark = QColor(theme.BORDER)
        hover_colour = QColor(theme.BORDER_HI)
        playhead = QColor(theme.CYAN)

        for i in range(count):
            x = i * pitch
            rect = QRectF(x, top, self._segment, self._bar_height)
            if i == lit - 1 and lit > 0:
                painter.setBrush(playhead)
            elif i < lit:
                painter.setBrush(gradient)
            elif i == hover_index:
                painter.setBrush(hover_colour)
            else:
                painter.setBrush(dark)
            painter.drawRect(rect)

        if lit > 0:
            # A soft bloom either side of the playhead: two translucent rects,
            # no blur, no graphics effect.
            glow = QColor(theme.CYAN)
            glow.setAlpha(60)
            painter.setBrush(glow)
            head_x = (lit - 1) * pitch
            painter.drawRect(QRectF(head_x - pitch, top + 2, pitch, self._bar_height - 4))
            painter.drawRect(QRectF(head_x + pitch, top + 2, pitch, self._bar_height - 4))


class LevelMeter(QLabel):
    """Thin vertical bar-pair driven by the live audio level.

    Fed by Player.level_changed; shows nothing (dark segments) when silent, so
    it doubles as a "there is actually audio coming out" indicator.
    """

    SEGMENTS = 14

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self.setFixedSize(16, 40)

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        # Repaint only on a visible change — buffers arrive far faster than the
        # eye resolves, and this widget is repainted from an audio callback.
        if abs(level - self._level) < 0.02:
            return
        self._level = level
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        lit = int(round(self._level * self.SEGMENTS))
        seg_h = self.height() / self.SEGMENTS
        dark = QColor(theme.BORDER)
        for i in range(self.SEGMENTS):
            # Index 0 is the bottom segment.
            y = self.height() - (i + 1) * seg_h
            rect = QRectF(2, y + 1, self.width() - 4, seg_h - 2)
            if i < lit:
                colour = QColor(theme.CYAN if i < self.SEGMENTS - 3 else theme.VIOLET_HI)
            else:
                colour = dark
            painter.setBrush(colour)
            painter.drawRect(rect)


class MarqueeLabel(QLabel):
    """Label that scrolls its text back and forth when it overflows.

    Long YouTube titles are the norm, and wrapping them reflows the whole art
    column. Scrolling keeps the panel a fixed height. The timer only runs while
    the text actually overflows *and* the label is visible.
    """

    TICK_MS = 33
    PAUSE_TICKS = 36  # ~1.2s dwell at each end

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self._offset = 0
        self._direction = 1
        self._pause = self.PAUSE_TICKS
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

    def setText(self, text: str) -> None:
        self._full_text = text
        self._offset = 0
        self._direction = 1
        self._pause = self.PAUSE_TICKS
        super().setText(text)
        self._sync_timer()

    def _overflow(self) -> int:
        return self.fontMetrics().horizontalAdvance(self._full_text) - self.width()

    def _sync_timer(self) -> None:
        if self.isVisible() and self._overflow() > 0:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._offset = 0
        self.update()

    def _tick(self) -> None:
        overflow = self._overflow()
        if overflow <= 0:
            self._sync_timer()
            return
        if self._pause > 0:
            self._pause -= 1
            return
        self._offset += self._direction
        if self._offset >= overflow or self._offset <= 0:
            self._offset = max(0, min(overflow, self._offset))
            self._direction *= -1
            self._pause = self.PAUSE_TICKS
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_timer()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        if self._overflow() <= 0:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(
            self.rect().adjusted(-self._offset, 0, 0, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._full_text,
        )
