"""A system-wide hotkey, so the palette is reachable from inside your editor.

Qt has no portable global hotkey — QShortcut only fires when the app already
has focus, which is exactly when you don't need it. On Windows the primitive is
RegisterHotKey plus a WM_HOTKEY message, both reachable through ctypes.

The hotkey is registered against the ribbon's real window handle rather than
against the thread (hwnd=NULL). Thread-targeted messages are delivered to
whatever is pumping the queue and are easy to lose; a window handle makes the
delivery path explicit and lets Qt's native event filter see it reliably.

No-ops cleanly off Windows, and reports failure rather than pretending: the
combination can already be taken by another application, and silently doing
nothing would be indistinguishable from a bug.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

IS_WINDOWS = sys.platform == "win32"

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Without NOREPEAT, holding the combination fires continuously.
MOD_NOREPEAT = 0x4000

VK_SPACE = 0x20

DEFAULT_ID = 0xA11C


class GlobalHotkey(QObject, QAbstractNativeEventFilter):
    """Registers one hotkey and emits `activated` when it fires."""

    activated = Signal()

    def __init__(
        self,
        modifiers: int = MOD_CONTROL | MOD_ALT,
        key: int = VK_SPACE,
        hotkey_id: int = DEFAULT_ID,
        parent: QObject | None = None,
    ) -> None:
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._modifiers = modifiers | MOD_NOREPEAT
        self._key = key
        self._id = hotkey_id
        self._hwnd = None
        self._registered = False

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self, window) -> bool:
        """Bind the hotkey to `window`'s handle. Returns False if unavailable."""
        if not IS_WINDOWS or self._registered:
            return self._registered

        from PySide6.QtWidgets import QApplication

        self._hwnd = wintypes.HWND(int(window.winId()))
        ok = ctypes.windll.user32.RegisterHotKey(
            self._hwnd, self._id, self._modifiers, self._key
        )
        if not ok:
            # Almost always means another app owns the combination.
            self._hwnd = None
            return False

        QApplication.instance().installNativeEventFilter(self)
        self._registered = True
        return True

    def unregister(self) -> None:
        if not (IS_WINDOWS and self._registered):
            self._registered = False
            return
        from PySide6.QtWidgets import QApplication

        try:
            ctypes.windll.user32.UnregisterHotKey(self._hwnd, self._id)
            instance = QApplication.instance()
            if instance is not None:
                instance.removeNativeEventFilter(self)
        except Exception:  # noqa: BLE001 - shutdown path, never raise
            pass
        self._registered = False
        self._hwnd = None

    def nativeEventFilter(self, event_type, message):
        # This runs for every native message the app sees, so it has to stay
        # cheap and must never raise.
        if event_type == b"windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
            except Exception:  # noqa: BLE001
                return False, 0
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                self.activated.emit()
                return True, 0
        return False, 0
