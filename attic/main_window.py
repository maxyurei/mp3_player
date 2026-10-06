import json
import random
import re
import shutil
import zlib
from collections import Counter, deque
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QSettings,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QFontMetrics,
    QKeySequence,
    QShortcut,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from send2trash import send2trash

import visuals
from downloader import CookieSource, DownloadTask, SearchResult, SearchTask
from library import Track, load_cover, read_stream_info, scan_folder
from player import Player
from ui import theme
from ui.art_panel import ArtPanel
from ui.chrome import MIN_HEIGHT, MIN_WIDTH, WindowFrame, apply_always_on_top
from ui.discover_panel import DiscoverPanel
from ui.mini_player import DEFAULT_SIZE as MINI_DEFAULT_SIZE
from ui.mini_player import MiniPlayer
from ui.search_dialog import SearchResultsDialog
from ui.widgets import (
    LevelMeter,
    MarqueeLabel,
    SegmentedSlider,
    set_display_font,
)

REPEAT_LABELS = {"off": "REPEAT", "all": "REPEAT ALL", "one": "REPEAT ONE"}

ART_SIZE = 248

# Upper bound on how many recently-played tracks shuffle refuses to repeat. The
# effective window is 40% of the playlist, capped here so a huge library doesn't
# end up remembering hundreds of entries for no benefit.
SHUFFLE_MEMORY_MAX = 25

# Focus mode: art goes full-bleed and the browsing controls slide away once you
# stop touching the window. One idle threshold covers both "cursor left the
# window" and "cursor parked and not moving" — polling the cursor beats relying
# on MouseMove events, which only arrive from widgets with mouse tracking on.
FOCUS_IDLE_MS = 3000
FOCUS_POLL_MS = 400
FOCUS_SLIDE_MS = 180

# The mini player (ui/mini_player.py) is the other thing idleness can trigger.
# Dropping into it is safe to automate — it *gives* screen back, and one
# keystroke undoes it. Coming back out is not, so nothing here ever does it.
MINI_IDLE_MS = 3000
# Longer threshold after an explicit restore. Without it, bringing the full
# window back to browse the track list would collapse it again three seconds
# later, which reads as the app fighting you.
MINI_REARM_MS = 20000

UNBOUNDED = 16_777_215  # QWIDGETSIZE_MAX


def format_time(ms: int) -> str:
    s = max(ms, 0) // 1000
    return f"{s // 60}:{s % 60:02d}"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MP3 Player")
        self.resize(1040, 640)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        # The native frame is replaced by ui/chrome.TitleBar; see that module
        # for the drag/resize/maximize handling it costs us.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        self._player = Player()
        self._player.source_changed.connect(self._on_source_changed)
        self._player.playback_state_changed.connect(self._on_state_changed)
        self._player.media_ended.connect(self._on_media_ended)
        self._player.position_changed.connect(self._on_position_changed)
        self._player.duration_changed.connect(self._on_duration_changed)

        # Each playlist is a folder path -> list of tracks. _tracks is the
        # currently-loaded playlist's tracks (mirrors what's in _track_list).
        self._playlists: dict[str, list[Track]] = {}
        self._tracks: list[Track] = []
        self._current_index: int = -1
        self._shuffle: bool = False
        # Recently played rows, so shuffle avoids near-term repeats.
        self._shuffle_history: deque[int] = deque(maxlen=SHUFFLE_MEMORY_MAX * 2)
        self._repeat_mode: str = "off"  # "off" | "all" | "one"
        # Row currently highlighted as playing in the list (may differ from
        # the selected row once the user clicks around).
        self._playing_row: int = -1
        # Suppress player-driven slider updates while the user is dragging,
        # otherwise the thumb fights with the user's cursor.
        self._seeking: bool = False
        # Trailing " · 320K · 44.1kHz" for the status readout; read once per
        # track when it starts playing.
        self._stream_info: str = ""

        # Visual overrides. Both are sparse by design — the point of visuals.py
        # is that the common case needs no entry in either.
        self._visual_pins: dict[str, str] = {}  # track filename -> pool-relative
        self._playlist_buckets: dict[str, str] = {}  # playlist folder -> mood
        # Derived, not chosen: the dealt visual per (mood, track). Stored so
        # adding a track can't reshuffle everything else — see visuals.deal.
        self._visual_assignments: dict[str, str] = {}
        visuals.ensure_root()

        # Search/download run as short-lived subprocess tasks on this pool so
        # the UI never blocks. Task refs are held so their signal objects live
        # until delivery.
        self._pool = QThreadPool.globalInstance()
        self._search_task: SearchTask | None = None
        self._download_task: DownloadTask | None = None

        # --- search (hosted in the title bar) ---
        self._search_input = QLineEdit()
        self._search_input.setObjectName("searchInput")
        self._search_input.setPlaceholderText("search youtube…")
        self._search_input.returnPressed.connect(self._on_search)
        self._search_btn = QPushButton("SEARCH")
        self._search_btn.setObjectName("chipButton")
        self._search_btn.clicked.connect(self._on_search)
        set_display_font(self._search_btn, size=11, spacing=1.4)
        self._search_status = QLabel("")
        self._search_status.setObjectName("minorLabel")

        self._discover_btn = QPushButton("DISCOVER")
        self._discover_btn.setObjectName("chipButton")
        self._discover_btn.setCheckable(True)
        self._discover_btn.setToolTip("Your YouTube feeds — fetched only when open")
        set_display_font(self._discover_btn, size=11, spacing=1.4)
        self._discover_btn.toggled.connect(self._on_discover_toggled)

        self._search_widget = QWidget()
        self._search_widget.setObjectName("titleBarSlot")
        search_row = QHBoxLayout(self._search_widget)
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)
        search_row.addWidget(self._search_input, stretch=1)
        search_row.addWidget(self._search_btn)
        search_row.addSpacing(6)
        search_row.addWidget(self._discover_btn)

        playlist_label = QLabel("PLAYLIST")
        playlist_label.setObjectName("sectionLabel")
        set_display_font(playlist_label, size=10, spacing=2.0, bold=True)
        self._playlist_combo = QComboBox()
        self._playlist_combo.currentIndexChanged.connect(self._on_playlist_changed)
        self._new_btn = QPushButton("NEW")
        self._new_btn.clicked.connect(self._new_playlist)
        self._add_btn = QPushButton("ADD FOLDER")
        self._add_btn.clicked.connect(self._add_folder)
        self._remove_btn = QPushButton("REMOVE")
        self._remove_btn.setToolTip("Remove the current playlist from the app")
        self._remove_btn.clicked.connect(self._remove_current_playlist)
        for button in (self._new_btn, self._add_btn, self._remove_btn):
            set_display_font(button, size=11, spacing=1.4)

        # Wrapped in a widget so focus mode can hide the whole row at once.
        self._playlist_holder = QWidget()
        self._playlist_holder.setObjectName("transparentPane")
        playlist_row = QHBoxLayout(self._playlist_holder)
        playlist_row.setContentsMargins(0, 0, 0, 0)
        playlist_row.setSpacing(8)
        playlist_row.addWidget(playlist_label)
        playlist_row.addWidget(self._playlist_combo, stretch=1)
        playlist_row.addWidget(self._new_btn)
        playlist_row.addWidget(self._add_btn)
        playlist_row.addWidget(self._remove_btn)

        self._filter_input = QLineEdit()
        self._filter_input.setObjectName("filterInput")
        self._filter_input.setPlaceholderText("Filter tracks…   Ctrl+F")
        self._filter_input.setClearButtonEnabled(True)
        self._filter_input.textChanged.connect(self._on_filter_changed)
        clear_filter_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Escape), self._filter_input
        )
        clear_filter_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        clear_filter_shortcut.activated.connect(self._filter_input.clear)

        self._track_list = QListWidget()
        # Elide rather than scroll sideways: a horizontal scrollbar under the
        # list is the fastest way to make a tidy layout look accidental.
        self._track_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._track_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._track_list.setWordWrap(False)
        self._track_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        # Delete key removes the selected song; scoped to the list so it doesn't
        # fire while the user is editing the search box.
        remove_song_shortcut = QShortcut(
            QKeySequence.StandardKey.Delete, self._track_list
        )
        remove_song_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        remove_song_shortcut.activated.connect(self._remove_current_song)

        self._remove_song_btn = QPushButton("REMOVE SONG")
        self._remove_song_btn.clicked.connect(self._remove_current_song)
        set_display_font(self._remove_song_btn, size=11, spacing=1.4)

        # --- now-playing column (the "device face") ---
        self._art_panel = ArtPanel(ART_SIZE)
        set_display_font(self._art_panel, size=11, spacing=2.4)
        self._art_panel.context_menu_requested.connect(self._on_art_context_menu)

        self._status_readout = QLabel("■ STOPPED")
        self._status_readout.setObjectName("statusReadout")
        self._status_readout.setProperty("live", False)
        set_display_font(self._status_readout, size=11, spacing=1.8, bold=True)

        # Marquee rather than wrap: long YouTube titles would otherwise reflow
        # the whole column and shove everything below it around.
        self._title_label = MarqueeLabel("—")
        self._title_label.setObjectName("titleLabel")
        self._title_label.setFixedHeight(22)
        self._artist_label = QLabel("—")
        self._artist_label.setObjectName("artistLabel")
        self._album_label = QLabel("—")
        self._album_label.setObjectName("albumLabel")

        art_column = QVBoxLayout()
        art_column.setContentsMargins(0, 0, 0, 0)
        art_column.setSpacing(6)
        art_column.addWidget(self._art_panel, stretch=1)
        art_column.addSpacing(4)
        art_column.addWidget(self._status_readout)
        art_column.addWidget(self._title_label)
        art_column.addWidget(self._artist_label)
        art_column.addWidget(self._album_label)
        # Keeps the art square-ish in normal mode by soaking up leftover height.
        # Focus mode zeroes this stretch, or it would hold the art to its
        # normal size and leave dead space underneath.
        art_column.addStretch(1)
        self._art_column = art_column
        self._art_stretch_index = art_column.count() - 1

        # A holder widget so the column keeps the art's width instead of being
        # stretched by whatever the longest label happens to be.
        self._art_holder = QWidget()
        self._art_holder.setObjectName("transparentPane")
        self._art_holder.setFixedWidth(ART_SIZE)
        self._art_holder.setLayout(art_column)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(self._filter_input, stretch=1)
        filter_row.addWidget(self._remove_song_btn)

        # Likewise: a widget, so its width can be animated when it slides away.
        self._track_holder = QWidget()
        self._track_holder.setObjectName("transparentPane")
        track_panel = QVBoxLayout(self._track_holder)
        track_panel.setContentsMargins(0, 0, 0, 0)
        track_panel.setSpacing(8)
        track_panel.addLayout(filter_row)
        track_panel.addWidget(self._track_list, stretch=1)

        # Hidden until asked for — see the rules in ui/discover_panel.py.
        stored = QSettings()
        self._discover = DiscoverPanel(
            self._player,
            lambda: self._volume_slider.value(),
            CookieSource(
                stored.value("discover/cookie_mode", "none", type=str),
                stored.value("discover/cookie_value", "", type=str),
            ),
            self,
        )
        self._discover.download_requested.connect(self._start_download)
        self._discover.cookies_changed.connect(self._save_cookie_settings)
        self._discover.setVisible(False)

        list_row = QHBoxLayout()
        list_row.setSpacing(18)
        list_row.addWidget(self._art_holder)
        list_row.addWidget(self._track_holder, stretch=1)
        list_row.addWidget(self._discover)

        self._filename_label = QLabel("No file loaded")
        self._filename_label.setObjectName("filenameLabel")

        # Holder so focus mode can drop it: the overlay already shows the
        # track name, and having it twice looks like a bug.
        self._status_holder = QWidget()
        self._status_holder.setObjectName("transparentPane")
        status_row = QHBoxLayout(self._status_holder)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(10)
        status_row.addWidget(self._filename_label, stretch=1)
        status_row.addWidget(self._search_status)

        self._seek_slider = SegmentedSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setObjectName("seekSlider")
        self._seek_slider.setRange(0, 0)
        self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._seek_slider.sliderMoved.connect(self._on_seek_moved)

        self._elapsed_label = QLabel("0:00")
        self._elapsed_label.setObjectName("timeLabel")
        self._total_label = QLabel("0:00")
        self._total_label.setObjectName("timeLabel")
        time_row = QHBoxLayout()
        time_row.setSpacing(10)
        time_row.addWidget(self._elapsed_label)
        time_row.addWidget(self._seek_slider, stretch=1)
        time_row.addWidget(self._total_label)

        self._prev_btn = QPushButton("◀◀")
        self._prev_btn.setObjectName("transportButton")
        self._prev_btn.setToolTip("Previous / restart  (Ctrl+Left)")
        self._prev_btn.clicked.connect(self._play_prev)
        self._play_btn = QPushButton("▶")
        self._play_btn.setObjectName("playButton")
        self._play_btn.setToolTip("Play / pause  (Space)")
        self._play_btn.clicked.connect(self._player.toggle_play_pause)
        self._play_btn.setEnabled(False)
        # QSS has no box-shadow, so the neon bloom on the one high-emphasis
        # control is a graphics effect. It is static — no per-frame cost.
        glow = QGraphicsDropShadowEffect(self._play_btn)
        glow.setBlurRadius(26)
        glow.setOffset(0, 0)
        glow.setColor(QColor(theme.VIOLET))
        self._play_btn.setGraphicsEffect(glow)

        self._next_btn = QPushButton("▶▶")
        self._next_btn.setObjectName("transportButton")
        self._next_btn.setToolTip("Next  (Ctrl+Right)")
        self._next_btn.clicked.connect(self._play_next)
        self._shuffle_btn = QPushButton("SHUFFLE")
        self._shuffle_btn.setCheckable(True)
        self._shuffle_btn.toggled.connect(self._on_shuffle_toggled)
        self._repeat_btn = QPushButton(REPEAT_LABELS["off"])
        self._repeat_btn.setToolTip("Cycle repeat: off → all → one")
        self._repeat_btn.clicked.connect(self._cycle_repeat)
        for button in (self._shuffle_btn, self._repeat_btn):
            set_display_font(button, size=11, spacing=1.4)

        # Audio-reactive: both of these are driven by the decoded PCM, not by a
        # timer pretending to be one. See Player._on_audio_buffer.
        self._level_meter = LevelMeter()
        self._level_meter.setToolTip("Output level")
        self._player.level_changed.connect(self._level_meter.set_level)
        self._player.level_changed.connect(self._art_panel.set_level)

        self._volume_slider = SegmentedSlider(
            Qt.Orientation.Horizontal, segment=4, gap=2, bar_height=10
        )
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setFixedWidth(120)
        self._volume_slider.valueChanged.connect(self._player.set_volume)

        vol_label = QLabel("VOL")
        vol_label.setObjectName("minorLabel")
        set_display_font(vol_label, size=10, spacing=2.0)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(self._prev_btn)
        controls.addWidget(self._play_btn)
        controls.addWidget(self._next_btn)
        controls.addSpacing(12)
        controls.addWidget(self._level_meter)
        controls.addSpacing(12)
        controls.addWidget(self._shuffle_btn)
        controls.addWidget(self._repeat_btn)
        controls.addStretch(1)
        controls.addWidget(vol_label)
        controls.addWidget(self._volume_slider)

        root = QVBoxLayout()
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)
        root.addWidget(self._playlist_holder)
        root.addLayout(list_row, stretch=1)
        root.addWidget(self._status_holder)
        root.addLayout(time_row)
        root.addLayout(controls)

        self._frame = WindowFrame(self)
        self._frame.content.setLayout(root)
        self._frame.title_bar.add_center_widget(self._search_widget)
        self._frame.title_bar.always_on_top_changed.connect(self._set_always_on_top)
        self.setCentralWidget(self._frame)

        # --- focus mode ---
        self._focus_mode = False
        self._focus_pinned = False  # toggled by hand; idle rules don't apply
        self._track_width = 0  # captured before the panel slides away
        self._art_panel.clicked.connect(self._toggle_focus_manual)

        # --- mini player ---
        # What idleness does: "mini" | "focus" | "none". Built lazily, because
        # a session that never goes idle should never pay for the window.
        self._idle_action = "mini"
        self._mini: MiniPlayer | None = None
        self._mini_idle_ms = MINI_IDLE_MS
        self._mini_size = MINI_DEFAULT_SIZE
        self._mini_pos: QPoint | None = None

        self._slide = QPropertyAnimation(self._track_holder, b"maximumWidth", self)
        self._slide.setDuration(FOCUS_SLIDE_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._slide.finished.connect(self._on_slide_finished)

        self._cursor_pos = QCursor.pos()
        self._idle_clock = QElapsedTimer()
        self._idle_clock.start()
        self._focus_timer = QTimer(self)
        self._focus_timer.setInterval(FOCUS_POLL_MS)
        self._focus_timer.timeout.connect(self._tick_focus)

        self._install_shortcuts()
        # App-level filter so Space toggles playback no matter which widget has
        # focus (except text fields — see eventFilter).
        QApplication.instance().installEventFilter(self)

        # Restore persisted state. Must be called after all widgets exist and
        # signals are connected — setValue/setChecked here will fire slots.
        self._load_settings()

    def _install_shortcuts(self) -> None:
        def add(sequence, slot) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(slot)

        add("Ctrl+Right", self._play_next)
        add("Ctrl+Left", self._play_prev)
        add("Ctrl+F", self._focus_filter)
        add("Ctrl+T", self._frame.title_bar.toggle_always_on_top)
        add("Ctrl+M", self._toggle_mini)
        # Window-scoped; the filter box's own Escape is widget-scoped and wins
        # while it has focus.
        add(Qt.Key.Key_Escape, self._exit_focus)
        # Keyboard media keys (delivered while the app has focus).
        add(Qt.Key.Key_MediaTogglePlayPause, self._player.toggle_play_pause)
        add(Qt.Key.Key_MediaPlay, self._player.toggle_play_pause)
        add(Qt.Key.Key_MediaPause, self._player.pause)
        add(Qt.Key.Key_MediaNext, self._play_next)
        add(Qt.Key.Key_MediaPrevious, self._play_prev)

    def _focus_filter(self) -> None:
        self._filter_input.setFocus()
        self._filter_input.selectAll()

    def _set_always_on_top(self, enabled: bool) -> None:
        apply_always_on_top(self, enabled)

    def eventFilter(self, obj, event) -> bool:
        # Any real interaction counts as "not idle", including typing, which
        # the cursor poll can't see.
        if (
            event.type() in (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress)
            and self.isActiveWindow()
        ):
            self._idle_clock.restart()

        # Space = play/pause from anywhere in the main window, unless the user
        # is typing in a text field (search box, filter, dialogs).
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Space
            and not event.isAutoRepeat()
            and self.isActiveWindow()
            and not isinstance(QApplication.focusWidget(), QLineEdit)
        ):
            if self._play_btn.isEnabled():
                self._player.toggle_play_pause()
            return True
        return super().eventFilter(obj, event)

    # --- playlist management ---

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open Folder")
        if not folder:
            return
        if folder in self._playlists:
            # Already added — just switch to it.
            idx = self._index_for_folder(folder)
            if idx >= 0:
                self._playlist_combo.setCurrentIndex(idx)
            return
        self._register_playlist(folder, switch_to=True)

    def _new_playlist(self) -> None:
        name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:")
        if not ok:
            return
        # Strip characters that are illegal in Windows folder names.
        safe = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(".")
        if not safe:
            return
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.MusicLocation
        )
        folder = Path(base) / safe
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self, "Could not create playlist", f"Failed to create folder:\n{exc}"
            )
            return
        key = str(folder)
        if key in self._playlists:
            # Already exists — just switch to it.
            idx = self._index_for_folder(key)
            if idx >= 0:
                self._playlist_combo.setCurrentIndex(idx)
            return
        self._register_playlist(key, switch_to=True)

    def _register_playlist(self, folder: str, switch_to: bool) -> None:
        self._playlists[folder] = scan_folder(Path(folder))
        self._playlist_combo.addItem(Path(folder).name, userData=folder)
        last = self._playlist_combo.count() - 1
        self._playlist_combo.setItemData(last, folder, Qt.ItemDataRole.ToolTipRole)
        if switch_to:
            self._playlist_combo.setCurrentIndex(last)

    def _remove_current_playlist(self) -> None:
        idx = self._playlist_combo.currentIndex()
        if idx < 0:
            return
        folder = self._playlist_combo.itemData(idx)
        self._playlists.pop(folder, None)
        self._playlist_combo.removeItem(idx)
        if self._playlist_combo.count() == 0:
            self._tracks = []
            self._track_list.clear()
            self._current_index = -1

    def _remove_current_song(self) -> None:
        row = self._track_list.currentRow()
        if not (0 <= row < len(self._tracks)):
            return
        track = self._tracks[row]

        reply = QMessageBox.question(
            self,
            "Remove Song",
            f'Move "{track.display_name}" to the Recycle Bin?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Unload first if this is the playing track, otherwise Windows keeps the
        # file locked and send2trash can't move it.
        removing_current = row == self._current_index
        if removing_current:
            self._player.clear()

        try:
            send2trash(str(track.path))
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not remove song",
                f"Failed to move the file to the Recycle Bin:\n{exc}",
            )
            return

        # _tracks is the same list stored in _playlists, so this also updates the
        # playlist itself — and since the file is gone, a rescan won't bring it
        # back.
        del self._tracks[row]
        self._track_list.takeItem(row)

        # Keep _current_index / _playing_row pointing at the same track they
        # referred to before the row shift.
        if removing_current:
            self._current_index = -1
            self._playing_row = -1
            self._reset_now_playing()
        else:
            if row < self._current_index:
                self._current_index -= 1
            if row < self._playing_row:
                self._playing_row -= 1

    def _reset_now_playing(self) -> None:
        # Nothing loaded means nothing worth focusing on.
        self._exit_focus()
        self._filename_label.setText("No file loaded")
        self._play_btn.setText("▶")
        self._play_btn.setEnabled(False)
        self._title_label.setText("—")
        self._artist_label.setText("—")
        self._album_label.setText("—")
        self._art_panel.clear()
        self._seek_slider.setRange(0, 0)
        self._elapsed_label.setText("0:00")
        self._total_label.setText("0:00")
        self._stream_info = ""
        self._set_status_readout("■ STOPPED", live=False)

    def _set_status_readout(self, text: str, live: bool) -> None:
        self._status_readout.setText(text)
        # QSS colours the readout off the `live` property, so it needs a
        # repolish to pick the change up.
        self._status_readout.setProperty("live", live)
        style = self._status_readout.style()
        style.unpolish(self._status_readout)
        style.polish(self._status_readout)
        # In focus mode this text is painted by the art panel instead.
        self._sync_focus_text()

    def _index_for_folder(self, folder: str) -> int:
        for i in range(self._playlist_combo.count()):
            if self._playlist_combo.itemData(i) == folder:
                return i
        return -1

    def _on_playlist_changed(self, index: int) -> None:
        self._playing_row = -1  # list is rebuilt; highlight no longer applies
        if index < 0:
            self._tracks = []
            self._track_list.clear()
            self._current_index = -1
            return
        folder = self._playlist_combo.itemData(index)
        self._tracks = self._playlists.get(folder, [])
        # History holds row indices, which mean nothing in the new playlist.
        self._shuffle_history.clear()
        self._track_list.clear()
        for track in self._tracks:
            self._track_list.addItem(track.display_name)
        self._current_index = -1
        self._on_filter_changed(self._filter_input.text())

    # --- list / playback navigation ---

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        self._play_index(self._track_list.row(item))

    def _play_index(self, index: int) -> None:
        if not (0 <= index < len(self._tracks)):
            return
        self._current_index = index
        self._shuffle_history.append(index)
        self._track_list.setCurrentRow(index)
        self._mark_playing_row(index)
        track = self._tracks[index]
        self._update_metadata_panel(track)
        self._player.load(str(track.path))
        self._player.play()

    def _on_media_ended(self) -> None:
        # Shuffle beats repeat-one. Asking for shuffle and being handed the same
        # track again is never the intent, and repeat-one silently winning here
        # is what made shuffle look like it repeated songs.
        if (
            self._repeat_mode == "one"
            and not self._shuffle
            and 0 <= self._current_index < len(self._tracks)
        ):
            self._play_index(self._current_index)
        else:
            self._advance(wrap=self._repeat_mode == "all")

    def _play_next(self) -> None:
        # Manual "next" always advances, even in repeat-one; any repeat mode
        # allows wrapping past the end.
        self._advance(wrap=self._repeat_mode != "off")

    def _advance(self, wrap: bool) -> None:
        if not self._tracks:
            return
        if self._shuffle and len(self._tracks) > 1:
            self._play_index(self._pick_shuffled())
        elif self._current_index + 1 < len(self._tracks):
            self._play_index(self._current_index + 1)
        elif wrap:
            self._play_index(0)

    def _pick_shuffled(self) -> int:
        """Random track, avoiding the current one and whatever played recently.

        Excluding only the current index (which is all it used to do) still lets
        a small playlist cycle the same four or five songs. Remembering a slice
        of recent history is what makes shuffle actually feel shuffled.
        """
        total = len(self._tracks)
        memory = max(1, min(total * 2 // 5, SHUFFLE_MEMORY_MAX))
        recent = set(list(self._shuffle_history)[-memory:])
        recent.add(self._current_index)
        choices = [i for i in range(total) if i not in recent]
        if not choices:
            # Playlist shorter than the memory window; fall back to the one
            # guarantee that actually matters — never the same track twice.
            choices = [i for i in range(total) if i != self._current_index]
        return random.choice(choices)

    def _play_prev(self) -> None:
        # A few seconds in, "previous" means "restart this track".
        if self._current_index >= 0 and self._player.position > 3000:
            self._player.set_position(0)
            return
        if self._current_index > 0:
            self._play_index(self._current_index - 1)
        elif self._repeat_mode == "all" and self._tracks:
            self._play_index(len(self._tracks) - 1)

    def _on_shuffle_toggled(self, on: bool) -> None:
        self._shuffle = on
        self._shuffle_history.clear()
        if on and self._repeat_mode == "one":
            # Shuffle ignores repeat-one, so leaving the button reading
            # "REPEAT ONE" would advertise something that no longer happens.
            self._set_repeat_mode("all")

    def _cycle_repeat(self) -> None:
        # "one" is unreachable while shuffling, for the same reason.
        order = (
            {"off": "all", "all": "off", "one": "all"}
            if self._shuffle
            else {"off": "all", "all": "one", "one": "off"}
        )
        self._set_repeat_mode(order[self._repeat_mode])

    def _set_repeat_mode(self, mode: str) -> None:
        if mode not in REPEAT_LABELS:
            mode = "off"
        self._repeat_mode = mode
        self._repeat_btn.setText(REPEAT_LABELS[mode])
        # QSS styles [active="true"] like a checked toggle; repolish so the
        # property change takes effect immediately.
        self._repeat_btn.setProperty("active", mode != "off")
        style = self._repeat_btn.style()
        style.unpolish(self._repeat_btn)
        style.polish(self._repeat_btn)

    def _mark_playing_row(self, row: int) -> None:
        if self._playing_row == row:
            return
        old = self._track_list.item(self._playing_row)
        if old is not None:
            font = old.font()
            font.setBold(False)
            old.setFont(font)
            old.setData(Qt.ItemDataRole.ForegroundRole, None)
        new = self._track_list.item(row)
        if new is not None:
            font = new.font()
            font.setBold(True)
            new.setFont(font)
            # Cyan is the app's "live audio" signal — see ui/theme.py.
            new.setForeground(QColor(theme.CYAN))
        self._playing_row = row

    def _on_filter_changed(self, text: str) -> None:
        needle = text.strip().lower()
        for i, track in enumerate(self._tracks):
            if needle:
                haystack = " ".join(
                    part
                    for part in (
                        track.display_name,
                        track.title,
                        track.artist,
                        track.album,
                    )
                    if part
                ).lower()
                hidden = needle not in haystack
            else:
                hidden = False
            self._track_list.setRowHidden(i, hidden)

    # --- seek ---

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self._player.set_position(self._seek_slider.value())
        self._seeking = False

    def _on_seek_moved(self, ms: int) -> None:
        # While dragging, the elapsed label previews the drag target.
        self._elapsed_label.setText(format_time(ms))

    def _on_position_changed(self, ms: int) -> None:
        if self._seeking:
            return
        self._seek_slider.setValue(ms)
        self._elapsed_label.setText(format_time(ms))
        if self._mini_active:
            self._mini.set_progress(ms, self._seek_slider.maximum())

    def _on_duration_changed(self, ms: int) -> None:
        self._seek_slider.setRange(0, ms)
        self._total_label.setText(format_time(ms))

    # --- metadata panel ---

    @staticmethod
    def _elide(label: QLabel, text: str) -> str:
        return QFontMetrics(label.font()).elidedText(
            text, Qt.TextElideMode.ElideRight, ART_SIZE
        )

    def _update_metadata_panel(self, track: Track) -> None:
        self._title_label.setText(track.title or track.path.stem)
        self._title_label.setToolTip(track.title or track.path.stem)
        for label, value in (
            (self._artist_label, track.artist or "—"),
            (self._album_label, track.album or "—"),
        ):
            label.setText(self._elide(label, value))
            label.setToolTip(value)

        # Feeds the "▶ PLAYING · 320K · 44.1kHz" readout. One header read per
        # track start, not per scan.
        bitrate, sample_rate = read_stream_info(track.path)
        parts = []
        if bitrate:
            parts.append(f"{round(bitrate / 1000)}K")
        if sample_rate:
            parts.append(f"{sample_rate / 1000:.1f}kHz")
        self._stream_info = ("  ·  " + "  ·  ".join(parts)) if parts else ""

        self._art_panel.set_track(load_cover(track.path), self._visual_for(track))
        self._sync_focus_text()

    # --- visuals ---

    def _current_track(self) -> Track | None:
        if 0 <= self._current_index < len(self._tracks):
            return self._tracks[self._current_index]
        return None

    @staticmethod
    def _assignment_key(bucket: str | None, filename: str) -> str:
        # Keyed by mood too, so switching a playlist's mood deals a fresh set
        # rather than inheriting the old one.
        return f"{bucket or ''}|{filename}"

    def _visual_for(self, track: Track) -> Path | None:
        # An explicit pin always wins.
        pin = self._visual_pins.get(track.path.name)
        if pin:
            pinned = visuals.visuals_root() / pin
            if pinned.is_file():
                return pinned

        # Bucket is keyed on the track's own folder rather than the selected
        # playlist, so a track keeps its mood however you got to it.
        bucket = self._playlist_buckets.get(str(track.path.parent))
        candidates = visuals.pool_for(bucket)
        if not candidates:
            return None

        self._ensure_assignments(bucket, candidates)
        stored = self._visual_assignments.get(
            self._assignment_key(bucket, track.path.name)
        )
        if stored:
            path = visuals.visuals_root() / stored
            if path.is_file():
                return path
        # No stored deal (track from another playlist, or its visual was
        # deleted) — give it the least-used one.
        return self._assign_least_used(bucket, candidates, track.path.name)

    def _ensure_assignments(self, bucket: str | None, candidates: list[Path]) -> None:
        """Deal the pool across the current playlist, once."""
        unassigned = [
            track.path.name
            for track in self._tracks
            if self._assignment_key(bucket, track.path.name)
            not in self._visual_assignments
        ]
        if not unassigned:
            return
        assigned_count = len(self._tracks) - len(unassigned)
        if assigned_count == 0:
            # Fresh playlist: deal the whole thing at once, which is what
            # guarantees every visual actually gets used.
            for name, path in visuals.deal(
                unassigned, candidates, seed=f"{bucket or ''}{len(self._tracks)}"
            ).items():
                self._store_assignment(bucket, name, path)
        else:
            # Tracks added since the deal: slot them in without disturbing
            # anything already assigned.
            for name in unassigned:
                self._assign_least_used(bucket, candidates, name)

    def _assign_least_used(
        self, bucket: str | None, candidates: list[Path], filename: str
    ) -> Path | None:
        if not candidates:
            return None
        counts = Counter()
        for track in self._tracks:
            stored = self._visual_assignments.get(
                self._assignment_key(bucket, track.path.name)
            )
            if stored:
                counts[stored] += 1
        # Rotate the candidate order by the track's own hash before taking the
        # minimum: min() returns the first of equal values, so without this
        # every tie would resolve to the same file.
        offset = zlib.crc32(filename.encode("utf-8", "surrogateescape")) % len(candidates)
        rotated = candidates[offset:] + candidates[:offset]
        best = min(
            rotated, key=lambda path: counts.get(visuals.relative_pin(path) or "", 0)
        )
        self._store_assignment(bucket, filename, best)
        return best

    def _redeal_visuals(self, bucket: str | None) -> None:
        """Throw away this playlist's deal and lay it out again.

        Stored assignments are what keep a track's visual stable, but they also
        mean visuals added later only reach newly-added tracks. This is the
        escape hatch.
        """
        for track in self._tracks:
            self._visual_assignments.pop(
                self._assignment_key(bucket, track.path.name), None
            )
        candidates = visuals.pool_for(bucket)
        if candidates:
            self._ensure_assignments(bucket, candidates)
        self._refresh_visual()

    def _store_assignment(self, bucket: str | None, filename: str, path: Path) -> None:
        relative = visuals.relative_pin(path)
        if relative:
            self._visual_assignments[self._assignment_key(bucket, filename)] = relative

    def _refresh_visual(self) -> None:
        track = self._current_track()
        if track is not None:
            self._art_panel.set_track(load_cover(track.path), self._visual_for(track))

    def _on_art_context_menu(self, pos) -> None:
        track = self._current_track()
        folder = str(track.path.parent) if track is not None else None
        menu = QMenu(self)

        pin_action = menu.addAction("Pin visual for this track…")
        pin_action.setEnabled(track is not None)
        clear_action = menu.addAction("Clear pin")
        clear_action.setEnabled(
            track is not None and track.path.name in self._visual_pins
        )

        menu.addSeparator()
        mood_menu = menu.addMenu("Playlist mood")
        mood_menu.setEnabled(folder is not None)
        current_bucket = self._playlist_buckets.get(folder) if folder else None
        default_action = mood_menu.addAction("Default pool")
        default_action.setCheckable(True)
        default_action.setChecked(current_bucket is None)
        bucket_actions = {}
        for name in visuals.buckets():
            action = mood_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == current_bucket)
            bucket_actions[action] = name

        reshuffle_action = menu.addAction("Re-deal visuals for this playlist")
        reshuffle_action.setToolTip(
            "Spread the pool over this playlist again — use after adding visuals"
        )
        reshuffle_action.setEnabled(folder is not None)

        menu.addSeparator()
        mini_action = menu.addAction("Mini player now\tCtrl+M")
        mini_action.setEnabled(track is not None)
        mini_action.setToolTip("Shrink to just the visual, always on top")

        idle_menu = menu.addMenu("When idle while playing")
        idle_actions = {}
        for key, label, tip in (
            ("mini", "Shrink to mini player", "Tuck into a corner at icon size"),
            ("focus", "Collapse to the art", "Hide the browsing controls, keep the window"),
            ("none", "Do nothing", "Leave the window exactly as it is"),
        ):
            action = idle_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self._idle_action == key)
            action.setToolTip(tip)
            idle_actions[action] = key

        motion_action = menu.addAction("Reduce motion")
        motion_action.setCheckable(True)
        motion_action.setChecked(self._art_panel.reduce_motion)
        open_action = menu.addAction("Open visuals folder…")

        chosen = menu.exec(self._art_panel.mapToGlobal(pos))
        if chosen is None:
            return

        if chosen is pin_action and track is not None:
            self._pin_visual(track)
        elif chosen is clear_action and track is not None:
            self._visual_pins.pop(track.path.name, None)
            self._refresh_visual()
        elif chosen is default_action and folder:
            self._playlist_buckets.pop(folder, None)
            self._refresh_visual()
        elif chosen in bucket_actions and folder:
            self._playlist_buckets[folder] = bucket_actions[chosen]
            self._refresh_visual()
        elif chosen is reshuffle_action and folder:
            self._redeal_visuals(self._playlist_buckets.get(folder))
        elif chosen is mini_action:
            self._enter_mini()
        elif chosen in idle_actions:
            self._idle_action = idle_actions[chosen]
            if self._idle_action != "focus":
                self._exit_focus()
            self._idle_clock.restart()
        elif chosen is motion_action:
            self._art_panel.set_reduce_motion(motion_action.isChecked())
        elif chosen is open_action:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(visuals.ensure_root())))

    def _pin_visual(self, track: Track) -> None:
        root = visuals.ensure_root()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Pin a visual for this track",
            str(root),
            "Visuals (*.gif *.webp)",
        )
        if not path:
            return
        relative = visuals.relative_pin(Path(path))
        if relative is None:
            QMessageBox.information(
                self,
                "Outside the visuals folder",
                "Pins are stored relative to the visuals folder so it stays "
                f"movable.\n\nCopy the file into:\n{root}\n\nthen pin it.",
            )
            return
        self._visual_pins[track.path.name] = relative
        self._refresh_visual()

    def _on_source_changed(self, path: str) -> None:
        if 0 <= self._current_index < len(self._tracks):
            self._filename_label.setText(self._tracks[self._current_index].display_name)
        else:
            self._filename_label.setText(Path(path).name)
        self._play_btn.setEnabled(True)

    def _on_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        # Motion doubles as the playback indicator: art while stopped, visual
        # while playing.
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._art_panel.set_playing(playing)
        if self._mini is not None:
            self._mini.set_playing(playing)
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._play_btn.setText("▮▮")
            self._set_status_readout(f"▶ PLAYING{self._stream_info}", live=True)
            self._idle_clock.restart()
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self._play_btn.setText("▶")
            self._set_status_readout(f"❚❚ PAUSED{self._stream_info}", live=False)
        else:
            self._play_btn.setText("▶")
            self._set_status_readout("■ STOPPED", live=False)

        # Pausing is a deliberate act — you're about to do something, so give
        # the browsing controls back rather than staying collapsed.
        if state != QMediaPlayer.PlaybackState.PlayingState and not self._focus_pinned:
            self._set_focus_mode(False)
        self._sync_focus_timer()

    # --- focus mode ---

    def _focus_eligible(self) -> bool:
        """Whether auto-focus may engage right now.

        Each of these is a case where collapsing the window out from under the
        user would be actively hostile rather than clean.
        """
        if not self._player.is_playing or not self._tracks:
            return False
        if self._discover_btn.isChecked():
            return False
        if self._seeking:
            return False
        if QApplication.activeModalWidget() is not None:
            return False
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return False
        return True

    def _tick_focus(self) -> None:
        position = QCursor.pos()
        moved = position != self._cursor_pos
        self._cursor_pos = position

        if moved and self.frameGeometry().contains(position):
            self._idle_clock.restart()
            if self._focus_mode and not self._focus_pinned:
                self._set_focus_mode(False)
            return

        if self._mini_active or not self._focus_eligible():
            return

        elapsed = self._idle_clock.elapsed()
        if self._idle_action == "mini" and elapsed >= self._mini_idle_ms:
            self._enter_mini()
        elif (
            self._idle_action == "focus"
            and not self._focus_mode
            and elapsed >= FOCUS_IDLE_MS
        ):
            self._set_focus_mode(True)

    def _sync_focus_timer(self) -> None:
        # Nothing to collapse while the main window isn't even on screen.
        if self._mini_active:
            self._focus_timer.stop()
            return
        # Only runs while it can do something: playing, or already collapsed.
        if self._player.is_playing or self._focus_mode:
            if not self._focus_timer.isActive():
                self._focus_timer.start()
        else:
            self._focus_timer.stop()

    def _set_focus_mode(self, on: bool) -> None:
        if on == self._focus_mode:
            return
        self._focus_mode = on

        self._art_panel.set_focus_mode(on)
        self._playlist_holder.setVisible(not on)
        self._status_holder.setVisible(not on)
        # Let the art claim the full height instead of the trailing spacer.
        self._art_column.setStretch(self._art_stretch_index, 0 if on else 1)
        # The column's labels are replaced by the panel's painted overlay.
        for label in (
            self._status_readout,
            self._title_label,
            self._artist_label,
            self._album_label,
        ):
            label.setVisible(not on)

        if on:
            self._art_holder.setMinimumWidth(0)
            self._art_holder.setMaximumWidth(UNBOUNDED)
        else:
            self._art_holder.setFixedWidth(ART_SIZE)
            self._focus_pinned = False

        self._sync_focus_text()
        self._slide_track_panel(collapse=on)
        self._sync_focus_timer()

    def _slide_track_panel(self, collapse: bool) -> None:
        self._slide.stop()
        if collapse:
            width = self._track_holder.width()
            if width > 0:
                self._track_width = width
            self._slide.setStartValue(self._track_width or ART_SIZE)
            self._slide.setEndValue(0)
        else:
            self._track_holder.setVisible(True)
            self._track_holder.setMaximumWidth(0)
            self._slide.setStartValue(0)
            self._slide.setEndValue(self._track_width or ART_SIZE)
        self._slide.start()

    def _on_slide_finished(self) -> None:
        if self._focus_mode:
            self._track_holder.setVisible(False)
        else:
            # Hand sizing back to the layout, or the panel stays pinned at
            # whatever width the animation happened to stop on.
            self._track_holder.setMaximumWidth(UNBOUNDED)

    def _sync_focus_text(self) -> None:
        track = self._current_track()
        title = (track.title or track.path.stem) if track is not None else ""
        artist = (track.artist or "") if track is not None else ""
        self._art_panel.set_now_playing(
            self._status_readout.text(), title, artist, self._player.is_playing
        )
        if self._mini is not None:
            self._mini.set_now_playing(title, artist)

    def _toggle_focus_manual(self) -> None:
        if self._focus_mode:
            self._focus_pinned = False
            self._set_focus_mode(False)
        elif self._current_track() is not None:
            # Pinned, or the very next cursor move would undo it — the click
            # that got here counts as cursor activity inside the window.
            self._focus_pinned = True
            self._set_focus_mode(True)
        self._idle_clock.restart()

    def _exit_focus(self) -> None:
        self._focus_pinned = False
        self._set_focus_mode(False)
        self._idle_clock.restart()

    # --- mini player ---

    @property
    def _mini_active(self) -> bool:
        return self._mini is not None and self._mini.isVisible()

    def _ensure_mini(self) -> MiniPlayer:
        if self._mini is None:
            mini = MiniPlayer(self._mini_size, self._mini_pos)
            mini.restore_requested.connect(self._exit_mini)
            mini.play_pause_requested.connect(self._player.toggle_play_pause)
            mini.next_requested.connect(self._play_next)
            mini.prev_requested.connect(self._play_prev)
            mini.seek_requested.connect(self._on_mini_seek)
            mini.volume_delta.connect(self._nudge_volume)
            mini.size_changed.connect(self._on_mini_size_changed)
            mini.quit_requested.connect(self._quit_from_mini)
            self._mini = mini
        return self._mini

    def _toggle_mini(self) -> None:
        if self._mini_active:
            self._exit_mini()
        else:
            self._enter_mini()

    def _enter_mini(self) -> None:
        if self._mini_active or self._current_track() is None:
            return

        # Focus mode and the mini player are two answers to the same question.
        # Leaving it on would hand the mini window a panel still sized and
        # painted for a full-bleed column.
        self._set_focus_mode(False)
        self._focus_timer.stop()

        mini = self._ensure_mini()
        # Reparent *before* hiding: the panel stops being this window's child,
        # so hiding the main window never reaches it.
        mini.take_panel(self._art_panel)
        mini.set_playing(self._player.is_playing)
        mini.set_progress(self._player.position, self._seek_slider.maximum())
        self._sync_focus_text()
        self.hide()
        mini.appear()
        self._mini_idle_ms = MINI_IDLE_MS

    def _exit_mini(self) -> None:
        if self._mini is None:
            return
        self._mini.hide()
        self._mini.release_panel()
        # Back to the head of the art column, with the stretch it had before.
        self._art_column.insertWidget(0, self._art_panel, stretch=1)
        self._art_panel.set_mini_mode(False)

        self.show()
        self.raise_()
        self.activateWindow()
        # Asked for the full window back by hand — don't take it away again
        # three seconds later while they're still reading the track list.
        self._mini_idle_ms = MINI_REARM_MS
        self._idle_clock.restart()
        self._sync_focus_timer()

    def _on_mini_seek(self, ratio: float) -> None:
        duration = self._seek_slider.maximum()
        if duration > 0:
            self._player.set_position(int(ratio * duration))

    def _nudge_volume(self, delta: int) -> None:
        value = max(0, min(100, self._volume_slider.value() + delta))
        self._volume_slider.setValue(value)
        if self._mini is not None:
            self._mini.set_volume(value)

    def _on_mini_size_changed(self, size: int) -> None:
        self._mini_size = size

    def _quit_from_mini(self) -> None:
        # Via the full window, so its closeEvent still writes the session out.
        self._exit_mini()
        self.close()

    # --- discover ---

    def _on_discover_toggled(self, shown: bool) -> None:
        if shown:
            # Browsing and focus mode are opposites; don't make the user fight
            # a collapsing layout while picking tracks.
            self._exit_focus()
        self._discover.setVisible(shown)
        if shown:
            # Opening the panel is the trigger to load anything at all; nothing
            # is fetched while it's closed.
            self._discover.activate()
        if not self._frame.title_bar.is_maximized:
            # Grow to make room instead of crushing the track list.
            delta = DiscoverPanel.WIDTH + 18
            self.resize(self.width() + (delta if shown else -delta), self.height())

    def _save_cookie_settings(self) -> None:
        # Written immediately rather than at exit: re-doing a cookie export
        # because the app didn't shut down cleanly would be infuriating.
        settings = QSettings()
        settings.setValue("discover/cookie_mode", self._discover.cookies.mode)
        settings.setValue("discover/cookie_value", self._discover.cookies.value)

    # --- search & download ---

    def _on_search(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            return
        self._set_search_busy(True, "Searching…")
        task = SearchTask(query)
        task.signals.finished.connect(self._on_search_done)
        task.signals.error.connect(self._on_search_error)
        self._search_task = task
        self._pool.start(task)

    def _on_search_done(self, results: list[SearchResult]) -> None:
        self._set_search_busy(False)
        if not results:
            QMessageBox.information(
                self, "No results", "No results found for that search."
            )
            return
        # Preview (inside the dialog) pauses the library player. Remember the
        # state so we can resume if the user previews then cancels.
        was_playing = self._player.is_playing
        dialog = SearchResultsDialog(
            results, self._player, self._volume_slider.value(), self
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            result = dialog.selected_result()
            if result is not None:
                self._start_download(result)  # this takes over playback
        elif was_playing:
            self._player.play()  # resume the song preview interrupted

    def _on_search_error(self, message: str) -> None:
        self._set_search_busy(False)
        self._show_error("Search failed", message)

    def _start_download(self, result: SearchResult) -> None:
        # Cache check: if we already have this exact video locally, play it
        # instantly instead of re-downloading.
        cached = self._find_cached(result.video_id)
        if cached is not None:
            self._search_status.setText("Playing cached copy")
            self._add_downloaded_track(cached)
            return

        if shutil.which("ffmpeg") is None:
            self._show_error(
                "ffmpeg not found",
                "ffmpeg is required to convert downloads to MP3, but it wasn't "
                "found on PATH.\n\nInstall it with:\n\n"
                "    winget install Gyan.FFmpeg\n\nthen restart the app.",
            )
            return

        folder = self._download_target_folder()
        self._set_search_busy(True, f"Downloading: {result.title[:40]}…")
        task = DownloadTask(result.video_id, folder)
        task.signals.finished.connect(self._on_download_done)
        task.signals.error.connect(self._on_download_error)
        self._download_task = task
        self._pool.start(task)

    def _on_download_done(self, path: str) -> None:
        self._set_search_busy(False)
        self._search_status.setText("")
        self._add_downloaded_track(Path(path))

    def _on_download_error(self, message: str) -> None:
        self._set_search_busy(False)
        self._show_error("Download failed", message)

    def _set_search_busy(self, busy: bool, status: str = "") -> None:
        self._search_input.setEnabled(not busy)
        self._search_btn.setEnabled(not busy)
        self._search_status.setText(status)

    def _show_error(self, title: str, message: str) -> None:
        # yt-dlp errors can be long; show the gist up top and the full output
        # (its stderr) under "Show Details" so failures are debuggable.
        lines = [ln for ln in message.strip().splitlines() if ln.strip()]
        summary = lines[-1] if lines else message
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(summary[:400])
        if message.strip() != summary:
            box.setDetailedText(message)
        box.exec()

    def _download_target_folder(self) -> Path:
        # Downloads land in the currently selected playlist folder so they show
        # up right where you're looking. With no playlist, fall back to a
        # dedicated folder under Music that gets registered on first download.
        idx = self._playlist_combo.currentIndex()
        if idx >= 0:
            return Path(self._playlist_combo.itemData(idx))
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.MusicLocation
        )
        folder = Path(base) / "MP3Player"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _find_cached(self, video_id: str) -> Path | None:
        # The downloaded filename embeds "[video_id]", so a cached copy is any
        # mp3 with that tag across known library folders + the download folder.
        tag = f"[{video_id}]"
        folders = set(self._playlists.keys())
        folders.add(str(self._download_target_folder()))
        for folder in folders:
            base = Path(folder)
            if not base.exists():
                continue
            for mp3 in base.glob("*.mp3"):
                if tag in mp3.name:
                    return mp3
        return None

    def _add_downloaded_track(self, path: Path) -> None:
        # Make sure the file's folder is a registered, freshly-scanned playlist,
        # then select and play the new track.
        folder = str(path.parent)
        if folder in self._playlists:
            self._playlists[folder] = scan_folder(path.parent)
            idx = self._index_for_folder(folder)
            if idx == self._playlist_combo.currentIndex():
                self._on_playlist_changed(idx)  # rebuild list from rescanned tracks
            else:
                self._playlist_combo.setCurrentIndex(idx)  # fires _on_playlist_changed
        else:
            self._register_playlist(folder, switch_to=True)

        for i, track in enumerate(self._tracks):
            if track.path.name == path.name:
                self._play_index(i)
                return

    # --- persistence ---

    def _load_settings(self) -> None:
        settings = QSettings()

        geometry = settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

        size = settings.beginReadArray("playlists")
        folders: list[str] = []
        for i in range(size):
            settings.setArrayIndex(i)
            path = settings.value("path", "", type=str)
            if path:
                folders.append(path)
        settings.endArray()
        for folder in folders:
            self._register_playlist(folder, switch_to=False)

        last = settings.value("last_playlist", "", type=str)
        if last:
            idx = self._index_for_folder(last)
            if idx >= 0:
                self._playlist_combo.setCurrentIndex(idx)

        self._volume_slider.setValue(int(settings.value("volume", 80, type=int)))
        self._shuffle_btn.setChecked(bool(settings.value("shuffle", False, type=bool)))
        self._set_repeat_mode(str(settings.value("repeat", "off", type=str)))

        self._visual_pins = self._read_json_setting(settings, "visuals/pins")
        self._playlist_buckets = self._read_json_setting(settings, "visuals/buckets")
        self._visual_assignments = self._read_json_setting(settings, "visuals/assigned")
        self._art_panel.set_reduce_motion(
            bool(settings.value("visuals/reduce_motion", False, type=bool))
        )
        action = str(settings.value("idle/action", "mini", type=str))
        self._idle_action = action if action in ("mini", "focus", "none") else "mini"
        self._mini_size = int(settings.value("mini/size", MINI_DEFAULT_SIZE, type=int))
        stored_x = settings.value("mini/x", None)
        stored_y = settings.value("mini/y", None)
        if stored_x is not None and stored_y is not None:
            # Off-screen coordinates are survivable: MiniPlayer.appear() clamps
            # to the current screen, so unplugging a monitor can't lose it.
            self._mini_pos = QPoint(int(stored_x), int(stored_y))

        # Restore the panel without going through the toggle slot: the stored
        # geometry already accounts for its width, and launching must never
        # trigger a fetch.
        self._discover.set_feed_key(
            settings.value("discover/feed", "music", type=str)
        )
        discover_visible = bool(settings.value("discover/visible", False, type=bool))
        self._discover_btn.blockSignals(True)
        self._discover_btn.setChecked(discover_visible)
        self._discover_btn.blockSignals(False)
        self._discover.setVisible(discover_visible)
        if discover_visible:
            self._discover.activate(allow_fetch=False)

        if bool(settings.value("maximized", False, type=bool)):
            self._frame.title_bar.restore_maximized(True)

        # Runs before the window is shown, so this takes the plain window-flag
        # path — no native window exists to re-order yet, and setting the flag
        # pre-show costs nothing.
        if bool(settings.value("window/always_on_top", False, type=bool)):
            self._frame.title_bar.set_always_on_top(True)
            apply_always_on_top(self, True)

    @staticmethod
    def _read_json_setting(settings: QSettings, key: str) -> dict[str, str]:
        # JSON in one key rather than a QSettings group: these dicts are keyed
        # by filenames and folder paths, which INI section keys would mangle on
        # '=', '/' and '\'.
        raw = settings.value(key, "", type=str)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _save_settings(self) -> None:
        settings = QSettings()

        settings.beginWriteArray("playlists")
        for i in range(self._playlist_combo.count()):
            settings.setArrayIndex(i)
            settings.setValue("path", self._playlist_combo.itemData(i))
        settings.endArray()

        idx = self._playlist_combo.currentIndex()
        settings.setValue(
            "last_playlist",
            self._playlist_combo.itemData(idx) if idx >= 0 else "",
        )
        settings.setValue("volume", self._volume_slider.value())
        settings.setValue("shuffle", self._shuffle_btn.isChecked())
        settings.setValue("repeat", self._repeat_mode)
        settings.setValue("visuals/pins", json.dumps(self._visual_pins))
        settings.setValue("visuals/buckets", json.dumps(self._playlist_buckets))
        settings.setValue("visuals/assigned", json.dumps(self._visual_assignments))
        settings.setValue("visuals/reduce_motion", self._art_panel.reduce_motion)
        settings.setValue("idle/action", self._idle_action)
        settings.setValue("mini/size", self._mini_size)
        if self._mini is not None:
            position = self._mini.pos()
            settings.setValue("mini/x", position.x())
            settings.setValue("mini/y", position.y())
        settings.setValue("window/always_on_top", self._frame.title_bar.is_always_on_top)
        settings.setValue("discover/visible", self._discover_btn.isChecked())
        settings.setValue("discover/feed", self._discover.feed_key)
        settings.setValue("discover/cookie_mode", self._discover.cookies.mode)
        settings.setValue("discover/cookie_value", self._discover.cookies.value)

        # While maximized the window's geometry *is* the screen, so saving it
        # would lose the restored size. Keep the last non-maximized geometry
        # and re-apply maximize on top of it at startup.
        maximized = self._frame.title_bar.is_maximized
        if not maximized:
            settings.setValue("geometry", self.saveGeometry())
        settings.setValue("maximized", maximized)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            # A minimised window keeps decoding animation frames unless it is
            # told not to. While the mini player has the panel it is not this
            # window's child, and this window's state says nothing about it.
            self._art_panel.set_suspended(
                not self._mini_active
                and bool(self.windowState() & Qt.WindowState.WindowMinimized)
            )
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_settings()
        if self._mini is not None:
            # Left open, a second top-level window keeps the app alive.
            self._mini.shutdown()
        self._discover.shutdown()
        super().closeEvent(event)
