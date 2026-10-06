"""Frameless window chrome: a custom title bar plus hand-rolled edge resizing.

The native Windows frame is most of what makes a Qt app read as "a Python app
with a dark stylesheet" rather than a purpose-built device. Dropping it costs
us drag, resize and maximize, which is what this module re-implements.

Two deliberate choices:

  * Maximize is done manually against the screen's *available* geometry. A
    frameless window told to showMaximized() will happily cover the taskbar.
  * Corners stay square with a 1px border, so we never need
    WA_TranslucentBackground — translucency would buy rounded corners at the
    cost of a full-window alpha composite on every repaint.

Edge hit-testing lives on WindowFrame, which spans the whole window. Qt
propagates unhandled mouse events up to the parent, so events landing on the
inert content container reach the frame; both have mouse tracking enabled so
the cursor still changes with no button held.
"""

import sys

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.widgets import set_display_font

RESIZE_MARGIN = 6
MIN_WIDTH = 720
MIN_HEIGHT = 460


def apply_always_on_top(window: QWidget, enabled: bool) -> None:
    """Keep `window` above other apps (the PowerToys "Always on Top" effect).

    Qt's own WindowStaysOnTopHint is the portable way to do this, but toggling
    a window flag on a *visible* window makes Qt destroy and re-create the
    native window: the app blinks, loses focus, and — because we're frameless —
    briefly flashes an unstyled box. So on Windows we ask the OS directly with
    SetWindowPos, which just re-orders the existing window.

    The Win32 path leaves Qt's windowFlags() unaware of the change, which is
    fine as long as nothing else calls setWindowFlag on the main window; if
    something ever does, it will reset the Z-order and this must be re-applied.
    """
    if sys.platform == "win32" and window.isVisible():
        import ctypes
        from ctypes import wintypes

        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010

        user32 = ctypes.windll.user32
        # Explicit argtypes: HWND is pointer-sized, and ctypes would otherwise
        # narrow a 64-bit handle to a C int.
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos(
            wintypes.HWND(int(window.winId())),
            wintypes.HWND(HWND_TOPMOST if enabled else HWND_NOTOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        return

    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
    if window.isVisible():
        # Re-creating the native window leaves it hidden until shown again.
        window.show()

EDGE_LEFT = 0x1
EDGE_RIGHT = 0x2
EDGE_TOP = 0x4
EDGE_BOTTOM = 0x8

_CURSOR_FOR_EDGE = {
    EDGE_LEFT: Qt.CursorShape.SizeHorCursor,
    EDGE_RIGHT: Qt.CursorShape.SizeHorCursor,
    EDGE_TOP: Qt.CursorShape.SizeVerCursor,
    EDGE_BOTTOM: Qt.CursorShape.SizeVerCursor,
    EDGE_LEFT | EDGE_TOP: Qt.CursorShape.SizeFDiagCursor,
    EDGE_RIGHT | EDGE_BOTTOM: Qt.CursorShape.SizeFDiagCursor,
    EDGE_RIGHT | EDGE_TOP: Qt.CursorShape.SizeBDiagCursor,
    EDGE_LEFT | EDGE_BOTTOM: Qt.CursorShape.SizeBDiagCursor,
}


class TitleBar(QWidget):
    """App mark, an optional centre widget, and the window buttons."""

    HEIGHT = 46
    DRAG_TO_RESTORE_PX = 10

    always_on_top_changed = Signal(bool)

    def __init__(self, window: QWidget, frame: "WindowFrame") -> None:
        super().__init__(frame)
        self.setObjectName("titleBar")
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        # Plain QWidget subclasses ignore stylesheet background/border unless
        # they opt into styled backgrounds.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._window = window
        self._frame = frame
        self._drag_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        self._maximized = False
        self._normal_geometry: QRect | None = None

        mark = QLabel("◆ MP3")
        mark.setObjectName("appMark")
        set_display_font(mark, size=14, spacing=3.0, bold=True)

        # Monochrome arrow rather than 📌: the emoji pin renders in colour from
        # Segoe UI Emoji and would be the only coloured glyph up here.
        self._pin_btn = self._window_button("⇧", "winPin")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setToolTip("Always on top  (Ctrl+T)")
        self._pin_btn.toggled.connect(self.always_on_top_changed)

        self._min_btn = self._window_button("─", "winButton")
        self._min_btn.clicked.connect(self._window.showMinimized)
        self._max_btn = self._window_button("□", "winButton")
        self._max_btn.clicked.connect(self.toggle_maximize)
        self._close_btn = self._window_button("✕", "winClose")
        self._close_btn.clicked.connect(self._window.close)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(18, 0, 6, 0)
        self._layout.setSpacing(10)
        self._layout.addWidget(mark)
        self._layout.addSpacing(14)
        self._layout.addStretch(1)
        self._layout.addWidget(self._pin_btn)
        self._layout.addWidget(self._min_btn)
        self._layout.addWidget(self._max_btn)
        self._layout.addWidget(self._close_btn)

    def _window_button(self, text: str, name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        # Deliberately no setFixedSize: styles.qss owns these dimensions. A
        # stylesheet min-width/max-width overrides a programmatic fixed size,
        # so setting both means the QSS silently wins — which is exactly how
        # these ended up 8px wide with clipped glyphs.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def add_center_widget(self, widget: QWidget, stretch: int = 1) -> None:
        # Slot sits just before the stretch that pushes the window buttons
        # right; anchored on the first of those buttons, so it must be updated
        # if another one is ever added ahead of it.
        index = self._layout.indexOf(self._pin_btn) - 1
        self._layout.insertWidget(index, widget, stretch)

    # --- maximize ---

    @property
    def is_maximized(self) -> bool:
        return self._maximized

    def toggle_maximize(self) -> None:
        if self._maximized:
            if self._normal_geometry is not None:
                self._window.setGeometry(self._normal_geometry)
            self._maximized = False
            self._max_btn.setText("□")
        else:
            self._normal_geometry = QRect(self._window.geometry())
            screen = self._window.screen() or QApplication.primaryScreen()
            self._window.setGeometry(screen.availableGeometry())
            self._maximized = True
            self._max_btn.setText("❐")
        self._frame.setProperty("maximized", self._maximized)
        style = self._frame.style()
        style.unpolish(self._frame)
        style.polish(self._frame)

    # --- always on top ---

    @property
    def is_always_on_top(self) -> bool:
        return self._pin_btn.isChecked()

    def toggle_always_on_top(self) -> None:
        self._pin_btn.toggle()  # emits always_on_top_changed

    def set_always_on_top(self, enabled: bool) -> None:
        """Reflect a state applied elsewhere (settings restore) in the button."""
        self._pin_btn.blockSignals(True)
        self._pin_btn.setChecked(enabled)
        self._pin_btn.blockSignals(False)

    def restore_maximized(self, maximized: bool) -> None:
        """Re-apply a persisted maximized state at startup."""
        if maximized and not self._maximized:
            self.toggle_maximize()

    # --- drag ---

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        global_pos = event.globalPosition().toPoint()
        if self._frame.edge_at(global_pos):
            # Cursor is in the resize band along the top — let the frame have it.
            event.ignore()
            return
        self._press_global = global_pos
        self._drag_offset = global_pos - self._window.geometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is None:
            super().mouseMoveEvent(event)
            return
        global_pos = event.globalPosition().toPoint()
        if self._maximized:
            if (
                self._press_global is None
                or (global_pos - self._press_global).manhattanLength()
                < self.DRAG_TO_RESTORE_PX
            ):
                return
            # Restoring mid-drag: keep the window under the cursor at the same
            # horizontal fraction it was grabbed at, so it doesn't teleport.
            fraction = global_pos.x() / max(1, self._window.width())
            self.toggle_maximize()
            self._drag_offset = QPoint(
                int(self._window.width() * fraction), self._drag_offset.y()
            )
        self._window.move(global_pos - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        self._press_global = None
        super().mouseReleaseEvent(event)


class WindowFrame(QWidget):
    """Central widget: draws the border, owns resizing, hosts the title bar."""

    def __init__(self, window: QWidget) -> None:
        super().__init__()
        self.setObjectName("windowFrame")
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._window = window
        self._edge = 0
        self._press_global = QPoint()
        self._press_geometry = QRect()

        self.title_bar = TitleBar(window, self)
        self.content = QWidget()
        self.content.setObjectName("windowContent")
        # Needed so hover moves over the inert container still reach us and can
        # update the resize cursor.
        self.content.setMouseTracking(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(self.title_bar)
        layout.addWidget(self.content, stretch=1)

    def edge_at(self, global_pos: QPoint) -> int:
        if self.title_bar.is_maximized:
            return 0
        geometry = self._window.geometry()
        x = global_pos.x() - geometry.x()
        y = global_pos.y() - geometry.y()
        edge = 0
        if x <= RESIZE_MARGIN:
            edge |= EDGE_LEFT
        elif x >= geometry.width() - RESIZE_MARGIN - 1:
            edge |= EDGE_RIGHT
        if y <= RESIZE_MARGIN:
            edge |= EDGE_TOP
        elif y >= geometry.height() - RESIZE_MARGIN - 1:
            edge |= EDGE_BOTTOM
        return edge

    def _apply_cursor(self, edge: int) -> None:
        shape = _CURSOR_FOR_EDGE.get(edge)
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        edge = self.edge_at(event.globalPosition().toPoint())
        if not edge:
            super().mousePressEvent(event)
            return
        self._edge = edge
        self._press_global = event.globalPosition().toPoint()
        self._press_geometry = QRect(self._window.geometry())
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        global_pos = event.globalPosition().toPoint()
        if self._edge:
            self._resize_to(global_pos)
            event.accept()
            return
        self._apply_cursor(self.edge_at(global_pos))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._edge = 0
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._edge:
            self.unsetCursor()
        super().leaveEvent(event)

    def _resize_to(self, global_pos: QPoint) -> None:
        geometry = QRect(self._press_geometry)
        delta = global_pos - self._press_global
        if self._edge & EDGE_LEFT:
            geometry.setLeft(min(geometry.left() + delta.x(), geometry.right() - MIN_WIDTH))
        if self._edge & EDGE_RIGHT:
            geometry.setRight(max(geometry.right() + delta.x(), geometry.left() + MIN_WIDTH))
        if self._edge & EDGE_TOP:
            geometry.setTop(min(geometry.top() + delta.y(), geometry.bottom() - MIN_HEIGHT))
        if self._edge & EDGE_BOTTOM:
            geometry.setBottom(max(geometry.bottom() + delta.y(), geometry.top() + MIN_HEIGHT))
        self._window.setGeometry(geometry)
