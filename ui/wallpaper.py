"""Render a window on the desktop layer, behind everything else.

The third scale the visual can live at. The ribbon gives you a band of GIF that
is always visible; the stage gives you all of it when you deliberately open it.
This gives you all of it *permanently*, at the one place on screen that cannot
possibly be in your way — behind your windows. You see it in whatever gaps your
editor leaves, and it costs zero screen space by construction.

How it works on Windows: the desktop is drawn by Progman. Sending it the
undocumented message 0x052C makes it spawn a WorkerW window that sits between
the wallpaper and the icons. Walking the top-level windows finds the WorkerW
that is a *sibling* of the one hosting SHELLDLL_DefView (the icon layer), and
reparenting into it puts our content under the icons and under every
application window.

This is the technique Wallpaper Engine and Lively use. It is undocumented, so
it is written defensively and every failure returns False rather than raising:
losing wallpaper mode should never be more than a feature that didn't turn on.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

SPAWN_WORKERW = 0x052C

HWND_BOTTOM = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _ENUM_PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    # Prototypes are mandatory here, not tidiness. ctypes defaults an unnamed
    # function's restype to C int — 32 bits — so on 64-bit Windows every HWND
    # these return comes back truncated. The symptom is subtle and total:
    # FindWindow appears to succeed, the handle is garbage, and the WorkerW
    # search silently finds nothing.
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]

    _user32.FindWindowExW.restype = wintypes.HWND
    _user32.FindWindowExW.argtypes = [
        wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR
    ]

    _user32.GetParent.restype = wintypes.HWND
    _user32.GetParent.argtypes = [wintypes.HWND]

    _user32.SetParent.restype = wintypes.HWND
    _user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]

    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]

    _user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    _user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD),
    ]

    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]

    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int
    ]
else:  # pragma: no cover - non-Windows
    _user32 = None
    _ENUM_PROC = None


def _class_name(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _find_worker() -> int | None:
    """The window to parent into so we render as desktop, or None.

    Three layouts, in preference order:

      1. Classic (Win10 / some Win11): a WorkerW hosts SHELLDLL_DefView and the
         *next* WorkerW is the wallpaper layer. Parent into that one.
      2. A WorkerW that is a direct child of Progman.
      3. Windows 11 single-Progman (measured on this machine, build 26200):
         SHELLDLL_DefView hangs directly off Progman and no WorkerW hosts it,
         so there is no sibling to find. Parent into Progman itself and sink
         below the icons — see DesktopLayer.attach.
    """
    progman = _user32.FindWindowW("Progman", None)
    if not progman:
        return None

    # Ask the shell to create the wallpaper WorkerW. Progman ignores this if it
    # already did, so it is safe to send more than once. A timeout is used
    # rather than SendMessageW so a wedged shell cannot hang the UI thread.
    result = wintypes.DWORD()
    _user32.SendMessageTimeoutW(
        progman, SPAWN_WORKERW, 0, 0, 0x0000, 1000, ctypes.byref(result)
    )

    found: list[int] = []

    def callback(hwnd, _lparam):
        # The layout is: a WorkerW hosting SHELLDLL_DefView (the icons),
        # immediately followed by the WorkerW we want (the wallpaper).
        if _user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            sibling = _user32.FindWindowExW(None, hwnd, "WorkerW", None)
            if sibling:
                found.append(sibling)
                return False
        return True

    _user32.EnumWindows(_ENUM_PROC(callback), 0)
    if found:
        return found[0]

    # Some Windows builds park the wallpaper WorkerW as a child of Progman
    # instead of promoting it to a top-level window.
    child = _user32.FindWindowExW(progman, None, "WorkerW", None)
    if child:
        return child

    # Layout 3: nothing but Progman. Its children render as the desktop, so it
    # works as the host provided we drop below SHELLDLL_DefView afterwards.
    return progman


class DesktopLayer:
    """Moves one widget between the normal window stack and the desktop layer."""

    def __init__(self, widget) -> None:
        self._widget = widget
        self._original_parent: int | None = None
        self._attached = False

    @property
    def supported(self) -> bool:
        return IS_WINDOWS

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self) -> bool:
        """Reparent onto the desktop. Returns False if the shell didn't cooperate."""
        if not IS_WINDOWS or self._attached:
            return self._attached
        worker = _find_worker()
        if not worker:
            return False
        try:
            hwnd = int(self._widget.winId())
            self._original_parent = _user32.GetParent(hwnd)
            if not _user32.SetParent(hwnd, worker):
                return False
            # SetParent puts the new child on top of its siblings, which on the
            # single-Progman layout means on top of SHELLDLL_DefView — i.e. the
            # GIF would hide the desktop icons. Sink to the bottom of the
            # sibling order so the icons stay above it.
            _user32.SetWindowPos(
                hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception:  # noqa: BLE001 - undocumented territory
            return False
        self._attached = True
        return True

    def detach(self) -> bool:
        """Put the window back in the normal stack.

        Must run before the widget is destroyed: a window left parented to
        WorkerW outlives its Qt object badly, and the desktop keeps painting a
        dead frame until Explorer restarts.
        """
        if not (IS_WINDOWS and self._attached):
            self._attached = False
            return True
        try:
            _user32.SetParent(int(self._widget.winId()), self._original_parent or 0)
        except Exception:  # noqa: BLE001
            return False
        finally:
            self._attached = False
            self._original_parent = None
        return True

    def fill_desktop(self) -> None:
        """Size to the whole virtual desktop, not one screen.

        The desktop layer spans every monitor, and a window sized to the primary
        screen leaves the others showing bare wallpaper.
        """
        if not IS_WINDOWS:
            return
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        x = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        height = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if width and height:
            self._widget.setGeometry(x, y, width, height)
