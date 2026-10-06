import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ui.app import PlayerApp


def main() -> None:
    # Set org/app names so QSettings has a stable identity, and use INI format
    # so settings live in AppData rather than the registry (easier to inspect).
    QCoreApplication.setOrganizationName("MP3Player")
    QCoreApplication.setApplicationName("MP3Player")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)

    if sys.platform == "win32":
        # Give the process its own taskbar identity; without this Windows
        # groups the window under pythonw.exe and shows the Python icon on the
        # taskbar instead of ours.
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MP3Player")

    app = QApplication(sys.argv)

    # The stage is a real top-level window, and closing it must not take the
    # app down — the ribbon is the app. Quitting is explicit, via the ribbon's
    # menu or by closing the ribbon itself.
    app.setQuitOnLastWindowClosed(False)

    icon_path = Path(__file__).parent / "assets" / "icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    qss_path = Path(__file__).parent / "ui" / "styles.qss"
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))

    controller = PlayerApp()
    # aboutToQuit rather than only the explicit quit path: the appbar
    # reservation and the desktop reparent both survive the process if they are
    # not undone, so every route out has to reach shutdown().
    app.aboutToQuit.connect(controller.shutdown)
    controller.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
