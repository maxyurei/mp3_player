"""Reserve a strip of screen edge, so nothing can ever cover the ribbon.

This is the module that makes "visible while I work, without blocking anything,
without me moving it" simultaneously true. Every other approach trades one of
those away:

  * always-on-top floating — gets in the way, and on a single 1920x1080 display
    there is nowhere to park it that is not in front of something
  * behind the editor — invisible the moment you maximise anything
  * auto-hide on idle — the app deciding things for you, which is the whole
    complaint this rebuild exists to fix

An appbar is different in kind. Windows shrinks every other window's idea of
the desktop, exactly as it does for the taskbar, so a maximised editor stops
*above* the ribbon rather than under it. Nothing is covered because nothing is
overlapping.

Implemented with ctypes against shell32. No new dependency, and no-ops cleanly
on anything that isn't Windows.

Coordinates here are physical pixels; Qt's are logical. On a 150% display those
differ by 1.5x, and mixing them puts the reserved rectangle in the wrong place
— hence the deliberate scaling at every boundary.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtWidgets import QApplication, QWidget

IS_WINDOWS = sys.platform == "win32"

# ABM_* messages
ABM_NEW = 0x00000000
ABM_REMOVE = 0x00000001
ABM_QUERYPOS = 0x00000002
ABM_SETPOS = 0x00000003
ABM_GETSTATE = 0x00000004
ABM_SETSTATE = 0x0000000A

# ABS_* taskbar state flags
ABS_AUTOHIDE = 0x0000001
ABS_ALWAYSONTOP = 0x0000002

# ABE_* edges
ABE_LEFT = 0
ABE_TOP = 1
ABE_RIGHT = 2
ABE_BOTTOM = 3

EDGES = {"top": ABE_TOP, "bottom": ABE_BOTTOM, "left": ABE_LEFT, "right": ABE_RIGHT}


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", wintypes.LPARAM),
    ]


if IS_WINDOWS:
    # SHAppBarMessage returns UINT_PTR, which is 64-bit here. ctypes defaults
    # the return type to int and silently truncates it — the same omission has
    # produced a null HWND from the WorkerW search and a zeroed CPU reading
    # elsewhere in this project. Declare the prototype rather than rediscover
    # it a fourth time.
    _shell32 = ctypes.windll.shell32
    _shell32.SHAppBarMessage.restype = ctypes.c_uint64
    _shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
else:  # pragma: no cover - non-Windows
    _shell32 = None


def _appbar_message(message: int, data: APPBARDATA) -> int:
    return int(_shell32.SHAppBarMessage(message, ctypes.byref(data)))


def taskbar_state() -> int | None:
    """Current ABS_* flags for the shell taskbar, or None off Windows."""
    if not IS_WINDOWS:
        return None
    data = APPBARDATA()
    data.cbSize = ctypes.sizeof(APPBARDATA)
    return _appbar_message(ABM_GETSTATE, data)


def set_taskbar_state(flags: int) -> bool:
    """Write the taskbar's ABS_* flags, then read them back to confirm.

    ABM_SETSTATE is documented as always returning TRUE, so its return value
    proves nothing; the read-back is the only real check.

    This changes a global Windows setting that outlives the process. Callers
    are responsible for restoring it — see AutoHideGuard, which is the only
    thing that should be calling this.
    """
    if not IS_WINDOWS:
        return False
    data = APPBARDATA()
    data.cbSize = ctypes.sizeof(APPBARDATA)
    data.lParam = flags
    _appbar_message(ABM_SETSTATE, data)
    return taskbar_state() == flags


class AutoHideGuard:
    """Auto-hides the taskbar while docked, and guarantees it gets undone.

    Reserving a strip at the bottom edge puts the ribbon directly above a
    48px taskbar, which is a lot of screen given to two stacked bars. Hiding
    the taskbar reclaims it — but auto-hide is a *global, persistent Windows
    setting*, not a property of this process. Left on, it silently changes how
    the machine behaves for every other application, forever.

    No in-process handler can survive `kill -9`, a power cut, or a hard GPU
    hang, so "restore on exit" cannot be the whole answer. Instead the original
    state is written to disk and flushed *before* the change is made, which
    turns an unrecoverable leak into a recoverable one: whatever happens, the
    next launch finds the record and puts the taskbar back. `recover()` is what
    closes the crash path, and it is the reason this class owns the persistence
    instead of leaving it to the caller.

    Deliberately does nothing if the taskbar is *already* auto-hiding, since
    that is the user's own setting and not ours to turn off later.
    """

    KEY = "taskbar/restore_state"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def _saved(self) -> int | None:
        raw = self._settings.value(self.KEY, -1, int)
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            return None
        return raw if raw >= 0 else None

    def recover(self) -> bool:
        """Undo a change a previous run died before undoing. Call at startup."""
        saved = self._saved()
        if saved is None or not IS_WINDOWS:
            return False
        set_taskbar_state(saved)
        self._settings.remove(self.KEY)
        self._settings.sync()
        return True

    def enable(self) -> bool:
        if not IS_WINDOWS or self._active:
            return self._active
        current = taskbar_state()
        if current is None:
            return False
        if current & ABS_AUTOHIDE:
            # Already the user's own preference. Leave it entirely alone —
            # claiming it here would mean switching it off on exit.
            return True
        # Write and flush the restore record first. A crash between this line
        # and the next is the whole reason the record exists.
        self._settings.setValue(self.KEY, int(current))
        self._settings.sync()
        if set_taskbar_state(current | ABS_AUTOHIDE):
            self._active = True
            return True
        # Refused: drop the record rather than leave a lie on disk that would
        # make the next launch "restore" a state we never changed.
        self._settings.remove(self.KEY)
        self._settings.sync()
        return False

    def disable(self) -> None:
        """Put the taskbar back. Safe to call repeatedly and when inactive."""
        saved = self._saved()
        if saved is not None and IS_WINDOWS:
            try:
                set_taskbar_state(saved)
            except Exception:  # noqa: BLE001 - may be shutting down
                pass
            self._settings.remove(self.KEY)
            self._settings.sync()
        self._active = False


class AppBar:
    """Registers a widget as a Windows appbar and keeps the reservation honest.

    Usage is deliberately narrow: dock(), undock(), and reapply() for when the
    strip's height changes. The caller owns the widget; this only ever moves it.
    """

    def __init__(self, widget: QWidget) -> None:
        self._widget = widget
        self._registered = False
        self._edge = "top"

    @property
    def active(self) -> bool:
        return self._registered

    @property
    def supported(self) -> bool:
        return IS_WINDOWS

    # --- internals ---

    def _data(self) -> APPBARDATA:
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        data.hWnd = wintypes.HWND(int(self._widget.winId()))
        return data

    def _ratio(self) -> float:
        screen = self._widget.screen() or QApplication.primaryScreen()
        return float(screen.devicePixelRatio()) if screen is not None else 1.0

    @staticmethod
    def _message(message: int, data: APPBARDATA) -> int:
        return _appbar_message(message, data)

    # --- public ---

    def dock(self, edge: str = "top", thickness: int | None = None) -> bool:
        """Reserve `thickness` logical px along `edge`. Returns success.

        The QUERYPOS / SETPOS handshake is not optional ceremony: QUERYPOS is
        where Windows tells us what it will actually allow, given the taskbar
        and any other appbars already registered. Skipping it and asserting a
        rectangle is how you end up overlapping the taskbar.
        """
        if not IS_WINDOWS:
            return False

        # Changing edges needs no re-registration: ABM_SETPOS with a different
        # uEdge moves the reserved strip as well as the window. (Verified —
        # SPI_GETWORKAREA updates asynchronously, so reading it straight after
        # the call shows the *previous* edge and makes this look broken.)
        self._edge = edge if edge in EDGES else "top"
        ratio = self._ratio()
        height_px = int(round((thickness or self._widget.height()) * ratio))

        screen = self._widget.screen() or QApplication.primaryScreen()
        if screen is None:
            return False
        full = screen.geometry()
        left = int(round(full.left() * ratio))
        top = int(round(full.top() * ratio))
        right = int(round((full.left() + full.width()) * ratio))
        bottom = int(round((full.top() + full.height()) * ratio))

        data = self._data()
        data.uEdge = EDGES[self._edge]

        if not self._registered:
            if not self._message(ABM_NEW, data):
                return False
            self._registered = True

        # Propose the full span of the chosen edge.
        if self._edge == "top":
            data.rc = wintypes.RECT(left, top, right, top + height_px)
        elif self._edge == "bottom":
            data.rc = wintypes.RECT(left, bottom - height_px, right, bottom)
        elif self._edge == "left":
            data.rc = wintypes.RECT(left, top, left + height_px, bottom)
        else:
            data.rc = wintypes.RECT(right - height_px, top, right, bottom)

        self._message(ABM_QUERYPOS, data)

        # QUERYPOS moves the edge it owns; restore our thickness against
        # whatever it came back with, then commit that.
        if self._edge == "top":
            data.rc.bottom = data.rc.top + height_px
        elif self._edge == "bottom":
            data.rc.top = data.rc.bottom - height_px
        elif self._edge == "left":
            data.rc.right = data.rc.left + height_px
        else:
            data.rc.left = data.rc.right - height_px

        self._message(ABM_SETPOS, data)

        # Windows works in physical pixels; Qt wants logical ones back.
        self._widget.setGeometry(
            int(round(data.rc.left / ratio)),
            int(round(data.rc.top / ratio)),
            int(round((data.rc.right - data.rc.left) / ratio)),
            int(round((data.rc.bottom - data.rc.top) / ratio)),
        )
        return True

    def reapply(self) -> bool:
        """Re-reserve after a height change or a display reconfiguration."""
        if not self._registered:
            return False
        return self.dock(self._edge)

    def undock(self) -> None:
        """Give the space back. Must run before the window is destroyed.

        An appbar that is never removed leaves the desktop work area
        permanently shrunk — the reservation outlives the process, and the only
        cure is a shell restart. Every exit path has to reach this.
        """
        if not (IS_WINDOWS and self._registered):
            self._registered = False
            return
        try:
            self._message(ABM_REMOVE, self._data())
        except Exception:  # noqa: BLE001 - shutting down; never raise from here
            pass
        self._registered = False
