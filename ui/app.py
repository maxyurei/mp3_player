"""The controller. Owns the model, wires the three surfaces, holds no UI itself.

Replaces ui/main_window.py, which was a thousand lines of QMainWindow that also
happened to contain the application logic. There is no main window any more —
the ribbon is the app — so the logic needed somewhere to live that is not a
widget.

Surfaces:
    ribbon   always present, edge-docked or floating
    palette  summoned by global hotkey, gone on Escape
    stage    opened deliberately; can drop to the desktop layer as wallpaper

Nothing here starts a timer that changes geometry. The only automatic
transition in the whole app is the stage hiding its own controls.
"""

from __future__ import annotations

import random
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import (
    QElapsedTimer,
    QObject,
    QPoint,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
)
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QMessageBox

from collections import Counter

import history
import radio as radio_module
import visuals
from downloader import DownloadTask, SearchTask
from library import Track, load_cover, scan_folder
from player import Player
from ui.dock import AppBar, AutoHideGuard
from ui.hotkey import GlobalHotkey
from ui.palette import Palette
from ui.ribbon import DEFAULT_HEIGHT, HEIGHTS, Ribbon
from ui.stage import Stage

# Upper bound on how many recently-played tracks shuffle refuses to repeat.
SHUFFLE_MEMORY_MAX = 25

# How far ahead the radio downloads. One is enough to hide the latency of the
# next track without filling the disk with things you will skip.
RADIO_PREFETCH = 1

# Minimum gap between forwarding playback position to the UI. The seek fill
# moves one pixel roughly every 130 ms on a wide strip and the clock ticks once
# a second, so anything faster than this repaints pixels that did not change.
POSITION_FORWARD_MS = 200


class PlayerApp(QObject):
    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings()
        self._pool = QThreadPool.globalInstance()

        # --- model ---
        self._player = Player()
        self._history = history.History()
        self._tracks: list[Track] = []
        # Everything across every playlist, for the palette. Kept separate from
        # _tracks (the current playlist) so searching is library-wide while
        # next/previous still stay inside the playlist you are listening to.
        self._library: list[Track] = []
        # path -> (title, artist), recovered by radio.describe. Cached because
        # it runs a dozen regexes per track and the palette re-ranks on every
        # keystroke.
        self._names: dict[Path, tuple[str, str]] = {}
        # QThreadPool does not keep the Python wrapper alive, so a task whose
        # last reference is dropped can be collected while it is still running
        # and emit into a deleted signals object on completion.
        self._tasks: set = set()
        self._index = -1
        self._playlists: list[Path] = []
        self._playlist: Path | None = None
        self._shuffle = False
        self._repeat = "off"
        self._recent: deque[int] = deque(maxlen=SHUFFLE_MEMORY_MAX)
        self._volume = 80

        # --- visuals ---
        self._assigned: dict[str, str] = {}

        # --- history bookkeeping ---
        self._play_started = 0.0
        self._last_position = 0
        self._duration = 0
        self._position_clock = QElapsedTimer()
        self._position_clock.start()

        # --- radio ---
        self._radio: list = []
        self._radio_index = -1
        self._radio_active = False
        self._radio_busy = False

        self._load_settings()

        # --- surfaces ---
        self._ribbon = Ribbon(
            width=self._settings.value("ribbon/width", 460, int),
            height=self._settings.value("ribbon/height", DEFAULT_HEIGHT, int),
            position=QPoint(
                self._settings.value("ribbon/x", 200, int),
                self._settings.value("ribbon/y", 80, int),
            ),
        )
        # Where the strip sat before it was docked, so undocking can put it
        # back rather than leaving it screen-wide.
        self._float_geometry: tuple[int, int, int] | None = None
        self._palette = Palette()
        self._stage = Stage()
        # A second Stage for wallpaper mode rather than reusing the first one:
        # the wallpaper is parented into the desktop and click-through, and
        # flipping one widget between that and a fullscreen window means every
        # open/close has to unwind the reparent first.
        self._wallpaper_stage = Stage()
        self._appbar = AppBar(self._ribbon)
        self._autohide = AutoHideGuard(self._settings)
        self._hotkey = GlobalHotkey()

        self._wire()
        self._apply_volume(self._volume)

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        """Read settings, migrating the old window-based keys forward.

        The playlists, volume and — most importantly — the stored visual
        assignments all carry over. Those assignments are a hundred hand-built
        track/GIF pairings; regenerating them would silently reshuffle which
        visual every song shows.
        """
        self._volume = int(self._settings.value("General/volume", 80, int))
        self._shuffle = self._settings.value("General/shuffle", False, bool)
        self._repeat = str(self._settings.value("General/repeat", "off"))

        size = int(self._settings.value("playlists/size", 0, int) or 0)
        for index in range(1, size + 1):
            raw = self._settings.value(f"playlists/{index}/path", "", str)
            if raw:
                path = Path(raw)
                if path.is_dir():
                    self._playlists.append(path)

        last = self._settings.value("General/last_playlist", "", str)
        if last and Path(last).is_dir():
            self._playlist = Path(last)
        elif self._playlists:
            self._playlist = self._playlists[0]

        self._assigned = self._read_json("visuals/assigned")
        # Assignments used to be keyed "<mood>|<filename>" when subfolders acted
        # as per-playlist pools. Those keys can't match a bare filename, so
        # rather than half-migrate a distribution that was skewed anyway, drop
        # them and let the first playlist load deal a clean, even spread.
        if any("|" in key for key in self._assigned):
            self._assigned = {}

    def _read_json(self, key: str) -> dict:
        import json

        raw = self._settings.value(key, "", str)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_json(self, key: str, value: dict) -> None:
        import json

        self._settings.setValue(key, json.dumps(value))

    def save_settings(self) -> None:
        self._settings.setValue("General/volume", self._volume)
        self._settings.setValue("General/shuffle", self._shuffle)
        self._settings.setValue("General/repeat", self._repeat)
        if self._playlist is not None:
            self._settings.setValue("General/last_playlist", str(self._playlist))

        self._settings.setValue("playlists/size", len(self._playlists))
        for index, path in enumerate(self._playlists, start=1):
            self._settings.setValue(f"playlists/{index}/path", str(path))

        self._write_json("visuals/assigned", self._assigned)

        # Persist the *floating* geometry even while docked: the docked strip
        # is screen-wide by definition, and saving that would mean undocking
        # after a restart hands you a 1920px strip you never asked for.
        if self._appbar.active and self._float_geometry is not None:
            x, y, width = self._float_geometry
        else:
            geometry = self._ribbon.geometry()
            x, y, width = geometry.x(), geometry.y(), geometry.width()
        self._settings.setValue("ribbon/width", width)
        self._settings.setValue("ribbon/height", self._ribbon.preset_height)
        self._settings.setValue("ribbon/x", x)
        self._settings.setValue("ribbon/y", y)
        self._settings.setValue("ribbon/docked", self._appbar.active)
        self._settings.setValue("ribbon/dock_edge", self._ribbon.dock_edge)
        self._settings.sync()

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------

    def _wire(self) -> None:
        player = self._player
        player.media_ended.connect(self._on_media_ended)
        player.position_changed.connect(self._on_position)
        player.duration_changed.connect(self._on_duration)
        player.playback_state_changed.connect(self._on_state)
        player.spectrum_changed.connect(self._ribbon.set_spectrum)

        ribbon = self._ribbon
        ribbon.play_pause_requested.connect(self.toggle_play)
        ribbon.next_requested.connect(self.next_track)
        ribbon.prev_requested.connect(self.previous_track)
        ribbon.seek_requested.connect(self._seek_ratio)
        ribbon.volume_delta.connect(self._nudge_volume)
        ribbon.stage_requested.connect(self.open_stage)
        ribbon.palette_requested.connect(self.open_palette)
        ribbon.quit_requested.connect(self.quit)
        ribbon.height_changed.connect(self._on_height_changed)
        ribbon.dock_requested.connect(self.set_dock)
        ribbon.taskbar_autohide_toggled.connect(self.set_taskbar_autohide)
        ribbon.wallpaper_toggled.connect(self.set_wallpaper)
        ribbon.reshuffle_requested.connect(self.reshuffle_visuals)
        ribbon.geometry_settled.connect(self.save_settings)

        palette = self._palette
        palette.play_requested.connect(self._play_track)
        palette.queue_requested.connect(self._queue_track)
        palette.radio_requested.connect(self.start_radio)
        palette.fetch_requested.connect(self._fetch)
        palette.search_requested.connect(self._search)

        stage = self._stage
        stage.play_pause_requested.connect(self.toggle_play)
        stage.next_requested.connect(self.next_track)
        stage.prev_requested.connect(self.previous_track)
        stage.seek_requested.connect(self._seek_ratio)
        stage.volume_delta.connect(self._nudge_volume)

        for sequence, slot in (
            ("Space", self.toggle_play),
            ("Ctrl+Right", self.next_track),
            ("Ctrl+Left", self.previous_track),
            ("F11", self.open_stage),
            ("Ctrl+F", self.open_palette),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self._ribbon)
            shortcut.activated.connect(slot)

        self._hotkey.activated.connect(self.open_palette)

    # ------------------------------------------------------------------
    # startup
    # ------------------------------------------------------------------

    def start(self) -> None:
        # Before anything else: if a previous run died while the taskbar was
        # hidden, put it back. The record on disk is the only thing that
        # survives a crash, so this is the whole crash path.
        self._autohide.recover()
        # Create the pool folder and its explainer on a fresh install; existing
        # ones are left alone.
        visuals.ensure_root()
        self._ribbon.set_taskbar_autohide(
            self._settings.value("ribbon/taskbar_autohide", False, bool)
        )

        self._ribbon.show()
        if self._settings.value("ribbon/docked", False, bool):
            edge = self._settings.value("ribbon/dock_edge", "top", str)
            self.set_dock(edge if edge in ("top", "bottom") else "top")

        if not self._hotkey.register(self._ribbon):
            # Silent failure here would be indistinguishable from a bug, and
            # the palette is still reachable from the context menu and Ctrl+F.
            self._ribbon.set_now_playing(
                "Ctrl+Alt+Space is taken by another app",
                "right-click the ribbon to search",
            )

        self.load_playlist(self._playlist)

        if self._settings.value("ribbon/wallpaper", False, bool):
            self.set_wallpaper(True)

    def load_playlist(self, folder: Path | None) -> None:
        if folder is None or not folder.is_dir():
            self._ribbon.set_now_playing("No playlist", "right-click to search")
            return
        self._playlist = folder
        if folder not in self._playlists:
            self._playlists.append(folder)
        self._tracks = scan_folder(folder)
        self._index = -1
        self._recent.clear()
        self._ensure_assignments()
        self._rescan_library()
        if self._tracks:
            self._ribbon.set_now_playing(
                f"{len(self._tracks)} tracks", folder.name
            )
            self._show_visual_for(self._tracks[0])

    def _rescan_library(self) -> None:
        """Rebuild the palette's index across every known playlist.

        The palette replaced the library browser, so it has to see everything —
        searching only the folder you happen to be playing would make nine of
        your ten playlists unreachable without a mode switch.
        """
        seen: set[Path] = set()
        merged: list[Track] = []
        folders = list(self._playlists)
        if self._playlist is not None and self._playlist not in folders:
            folders.insert(0, self._playlist)
        for folder in folders:
            if not folder.is_dir():
                continue
            for track in scan_folder(folder):
                if track.path in seen:
                    continue
                seen.add(track.path)
                merged.append(track)
        self._library = merged
        for track in merged:
            if track.path not in self._names:
                self._names[track.path] = self._describe(track)
        self._palette.set_library(self._library, self._names)

    def _describe(self, track: Track) -> tuple[str, str]:
        """(title, artist) fit to show a human.

        The ID3 artist tag on a YouTube rip is the *uploader* — "FineTunes",
        "slowedmusic4u", "chancho" — so showing it raw labels every track with
        the name of whoever posted it. The real pair is recoverable from the
        title tag or the filename, which is what radio.describe does.
        """
        artist, title = radio_module.describe(
            str(track.path), track.title or "", track.artist or ""
        )
        return title or track.display_name, artist

    # ------------------------------------------------------------------
    # visuals
    # ------------------------------------------------------------------

    def _ensure_assignments(self) -> None:
        """Deal visuals to any track in this playlist that has none yet.

        Existing entries are never rewritten — that is what keeps a given song
        showing the same visual until you ask for a reshuffle. New tracks are
        dealt against the counts of everything already assigned, so a library
        that grew one download at a time ends up as evenly spread as one dealt
        in a single go.
        """
        candidates = visuals.pool()
        if not candidates:
            return
        missing = [
            track.path.name
            for track in self._tracks
            if track.path.name not in self._assigned
        ]
        if not missing:
            return
        dealt = visuals.deal(missing, candidates, Counter(self._assigned.values()))
        for name, path in dealt.items():
            relative = visuals.relative_name(path)
            if relative:
                self._assigned[name] = relative

    def reshuffle_visuals(self) -> None:
        """Throw the deal away and hand every track a fresh visual."""
        if not visuals.pool():
            self._notify(
                "No visuals to shuffle",
                "Drop .gif or .webp files into assets/visuals first.",
            )
            return
        self._assigned.clear()
        self._ensure_assignments()
        self.save_settings()
        track = self.current
        if track is not None:
            # Show the new visual straight away rather than at the next track
            # change, or the command looks like it did nothing.
            self._show_visual_for(track)

    def _visual_for(self, track: Track) -> Path | None:
        relative = self._assigned.get(track.path.name)
        if relative:
            candidate = visuals.visuals_root() / relative
            if candidate.is_file():
                return candidate
        return visuals.resolve(track.path)

    def _show_visual_for(self, track: Track) -> None:
        visual = self._visual_for(track)
        art: QPixmap | None = None
        data = load_cover(track.path)
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                art = pixmap
        self._ribbon.set_visual(visual, art)
        if self._stage.isVisible():
            self._stage.set_visual(visual, art)
        if self._wallpaper_stage.is_wallpaper:
            self._wallpaper_stage.set_visual(visual, art)

    # ------------------------------------------------------------------
    # playback
    # ------------------------------------------------------------------

    @property
    def current(self) -> Track | None:
        if 0 <= self._index < len(self._tracks):
            return self._tracks[self._index]
        return None

    def _play_index(self, index: int) -> None:
        if not (0 <= index < len(self._tracks)):
            return
        self._flush_history()
        self._index = index
        self._recent.append(index)
        track = self._tracks[index]

        self._player.load(str(track.path))
        self._player.play()
        self._play_started = time.time()
        self._last_position = 0

        title, artist = self._names.get(track.path) or self._describe(track)
        self._names.setdefault(track.path, (title, artist))
        self._ribbon.set_now_playing(title, artist)
        self._stage.set_now_playing(title, artist)
        self._palette.set_current(track.path)
        self._show_visual_for(track)

    def _play_track(self, track: Track) -> None:
        for index, candidate in enumerate(self._tracks):
            if candidate.path == track.path:
                self._play_index(index)
                return
        # Picked from another playlist via the palette: switch to that playlist
        # so next/previous continue through the album it came from rather than
        # dead-ending on a single adopted track.
        folder = track.path.parent
        if folder.is_dir() and folder != self._playlist:
            self.load_playlist(folder)
            for index, candidate in enumerate(self._tracks):
                if candidate.path == track.path:
                    self._play_index(index)
                    return
        self._tracks.append(track)
        self._play_index(len(self._tracks) - 1)

    def _queue_track(self, track: Track) -> None:
        """Insert directly after the current track."""
        target = min(self._index + 1, len(self._tracks))
        self._tracks.insert(target, track)

    def toggle_play(self) -> None:
        if self.current is None:
            if self._tracks:
                self._play_index(0)
            return
        self._player.toggle_play_pause()

    def next_track(self) -> None:
        if self._radio_active:
            self._advance_radio()
            return
        if not self._tracks:
            return
        if self._shuffle:
            self._play_index(self._pick_shuffled())
        else:
            self._play_index((self._index + 1) % len(self._tracks))

    def previous_track(self) -> None:
        if not self._tracks:
            return
        # Restart the track if we're past the first few seconds, which is what
        # every other player does and what the muscle memory expects.
        if self._player.position > 3000:
            self._player.set_position(0)
            return
        self._play_index((self._index - 1) % len(self._tracks))

    def _pick_shuffled(self) -> int:
        count = len(self._tracks)
        window = min(SHUFFLE_MEMORY_MAX, max(1, int(count * 0.4)))
        recent = set(list(self._recent)[-window:])
        pool = [i for i in range(count) if i not in recent] or list(range(count))
        return random.choice(pool)

    def _seek_ratio(self, ratio: float) -> None:
        if self._duration > 0:
            self._player.set_position(int(self._duration * ratio))

    def _on_duration(self, ms: int) -> None:
        self._duration = ms
        self._ribbon.set_progress(self._player.position, ms)

    def _on_position(self, ms: int) -> None:
        # positionChanged arrives ~20 times a second, once per decoded chunk.
        # The clock only changes once a second and the seek fill moves a pixel
        # every ~130 ms, so forwarding all of them just generated repaints for
        # pixels that were already correct.
        self._last_position = ms
        if self._position_clock.elapsed() < POSITION_FORWARD_MS:
            return
        self._position_clock.restart()
        self._ribbon.set_progress(ms, self._duration)
        if self._stage.isVisible():
            self._stage.set_progress(ms, self._duration)

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._ribbon.set_playing(playing)
        self._stage.set_playing(playing)
        # The wallpaper obeys the same motion contract as everything else: it
        # stops decoding frames the moment the music stops.
        self._wallpaper_stage.set_playing(playing)

    def _on_media_ended(self) -> None:
        self._flush_history()
        if self._radio_active:
            self._advance_radio()
            return
        if self._repeat == "one":
            self._play_index(self._index)
            return
        if self._repeat == "all" or self._index < len(self._tracks) - 1:
            self.next_track()

    def _apply_volume(self, percent: int) -> None:
        self._volume = max(0, min(100, percent))
        self._player.set_volume(self._volume)

    def _nudge_volume(self, delta: int) -> None:
        self._apply_volume(self._volume + delta)
        self._ribbon.set_volume(self._volume)

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------

    def _flush_history(self) -> None:
        """Log the track we are leaving. Called before every source change."""
        track = self.current
        if track is None or self._last_position <= 0:
            return
        title, artist = self._names.get(track.path) or self._describe(track)
        self._history.record(
            str(track.path),
            title=title,
            artist=artist,
            played_ms=self._last_position,
            duration_ms=self._duration,
            started_at=self._play_started or None,
        )
        self._last_position = 0

    # ------------------------------------------------------------------
    # radio
    # ------------------------------------------------------------------

    def start_radio(self, seed: Track | None = None) -> None:
        if self._radio_busy:
            return
        seeds = []
        if seed is not None:
            artist, title = radio_module.describe(
                str(seed.path), seed.title or "", seed.artist or ""
            )
            seeds.append(
                history.Seed(
                    path=str(seed.path), title=title, artist=artist,
                    score=1.0, plays=1, skips=0,
                )
            )
        seeds.extend(self._history.seeds(limit=8))
        if not seeds:
            self._notify(
                "Nothing to build a radio from yet",
                "Play a few tracks through first — the radio is built from "
                "what you actually finish.",
            )
            return

        self._radio_busy = True
        self._ribbon.set_now_playing("Building radio…", "from your history")
        # Exclude everything already on disk anywhere, not just this playlist —
        # otherwise the radio cheerfully recommends tracks from your own
        # library that happen to live in a different folder.
        exclude = {radio_module._key(t.display_name) for t in self._library}
        task = radio_module.RadioTask(seeds, exclude_keys=exclude, limit=30)
        task.signals.finished.connect(self._on_radio_ready)
        task.signals.error.connect(self._on_radio_error)
        self._run_task(task)

    def _on_radio_ready(self, picks: list) -> None:
        self._radio_busy = False
        self._radio = picks
        self._radio_index = -1
        self._radio_active = True
        self._advance_radio()

    def _on_radio_error(self, message: str) -> None:
        self._radio_busy = False
        self._radio_active = False
        self._notify("Radio failed", message)

    def _advance_radio(self) -> None:
        self._radio_index += 1
        if self._radio_index >= len(self._radio):
            self._radio_active = False
            self._notify("Radio finished", "Start another from the palette.")
            return
        pick = self._radio[self._radio_index]
        self._ribbon.set_now_playing(pick.result.title, pick.reason)
        self._download_and_play(pick.result.video_id, pick.result.title)

    def _download_and_play(self, video_id: str, label: str) -> None:
        folder = self._playlist or (Path.home() / "Music")
        task = DownloadTask(video_id, folder)
        task.signals.finished.connect(self._on_download_finished)
        task.signals.error.connect(
            lambda message: self._notify(f"Could not fetch “{label}”", message)
        )
        self._run_task(task)

    def _on_download_finished(self, path_text: str) -> None:
        path = Path(path_text)
        if not path.is_file():
            return
        existing = next((t for t in self._tracks if t.path == path), None)
        if existing is None:
            self._tracks = scan_folder(path.parent)
            self._ensure_assignments()
            self._rescan_library()
            existing = next((t for t in self._tracks if t.path == path), None)
        if existing is not None:
            self._play_track(existing)

    # ------------------------------------------------------------------
    # palette actions
    # ------------------------------------------------------------------

    def open_palette(self) -> None:
        if not self._library:
            self._rescan_library()
        self._palette.set_library(self._library, self._names)
        track = self.current
        self._palette.set_current(track.path if track else None)
        self._palette.summon()

    def _search(self, query: str) -> None:
        task = SearchTask(query)
        task.signals.finished.connect(
            lambda results, q=query: self._palette.set_fetch_results(q, results)
        )
        task.signals.error.connect(lambda message: self._notify("Search failed", message))
        self._run_task(task)

    def _fetch(self, result) -> None:
        self._radio_active = False
        self._ribbon.set_now_playing(f"Fetching {result.title}…", "")
        self._download_and_play(result.video_id, result.title)

    # ------------------------------------------------------------------
    # stage / dock
    # ------------------------------------------------------------------

    def open_stage(self) -> None:
        track = self.current
        if track is not None:
            self._stage.set_visual(self._visual_for(track))
            self._stage.set_now_playing(
                track.title or track.display_name, track.artist or ""
            )
        self._stage.set_playing(self._player.is_playing)
        self._stage.set_progress(self._player.position, self._duration)
        self._stage.showFullScreen()
        self._stage.raise_()
        self._stage.activateWindow()

    def set_dock(self, mode: str) -> None:
        """Dock to an edge, or float. `mode` is "off", "top" or "bottom".

        Bottom exists because the top edge is where browser tabs and title bars
        live; reserving it there pushes every window down and is the one place
        a permanent strip is most in the way.
        """
        if mode not in ("off", "top", "bottom"):
            mode = "off"
        if mode != "off":
            if not self._appbar.supported:
                self._notify(
                    "Docking needs Windows",
                    "The ribbon will stay floating on this platform.",
                )
                return
            # Re-docking to the other edge must not overwrite the remembered
            # floating geometry with the current screen-wide docked strip.
            if not self._appbar.active:
                geometry = self._ribbon.geometry()
                self._float_geometry = (
                    geometry.x(), geometry.y(), geometry.width()
                )
            if self._appbar.dock(mode, self._ribbon.preset_height):
                self._ribbon.set_docked(True, mode)
                if self._ribbon.taskbar_autohide and self._autohide.enable():
                    # The taskbar has just stopped reserving its own strip, but
                    # our position was negotiated while it still did. Re-run the
                    # handshake or the ribbon sits above a band of dead space
                    # exactly the height of the taskbar it just hid.
                    self._appbar.reapply()
        else:
            # Undocking gives the screen edge back, so the taskbar has no
            # reason to stay hidden.
            self._autohide.disable()
            self._appbar.undock()
            self._ribbon.set_docked(False)
            if self._float_geometry is not None:
                x, y, width = self._float_geometry
                self._ribbon.setGeometry(
                    x, y, width, self._ribbon.preset_height
                )
                self._ribbon.clamp_to_screen()
        self.save_settings()

    def set_wallpaper(self, on: bool) -> None:
        """Put the visual on the desktop layer, behind every window."""
        if on:
            if not self._wallpaper_stage._wallpaper.supported:
                self._notify(
                    "Wallpaper mode needs Windows",
                    "This uses the Windows desktop compositor to render behind "
                    "your windows.",
                )
                return
            track = self.current
            if track is not None:
                self._wallpaper_stage.set_visual(self._visual_for(track))
            self._wallpaper_stage.set_playing(self._player.is_playing)
            if not self._wallpaper_stage.enter_wallpaper():
                self._notify(
                    "Could not reach the desktop layer",
                    "Windows Explorer refused the request. Restarting Explorer "
                    "usually clears this.",
                )
                return
        else:
            self._wallpaper_stage.leave_wallpaper()
        self._ribbon.set_wallpaper_on(on)
        self._settings.setValue("ribbon/wallpaper", on)

    def set_taskbar_autohide(self, on: bool) -> None:
        """Opt in to hiding the taskbar while docked.

        Only takes effect while docked — the point is reclaiming the edge the
        ribbon is sitting on, and hiding the taskbar for a floating window
        would be changing a system setting for no benefit at all.
        """
        self._ribbon.set_taskbar_autohide(on)
        self._settings.setValue("ribbon/taskbar_autohide", on)
        if on and self._appbar.active:
            if self._autohide.enable():
                self._appbar.reapply()
            else:
                self._notify(
                    "Could not hide the taskbar",
                    "Windows refused the request; the taskbar is unchanged.",
                )
        elif not on:
            self._autohide.disable()
            if self._appbar.active:
                # The taskbar wants its strip back; renegotiate so the ribbon
                # moves out of the way rather than sitting under it.
                self._appbar.reapply()

    def _on_height_changed(self, height: int) -> None:
        if self._appbar.active:
            self._appbar.reapply()
        self.save_settings()

    # ------------------------------------------------------------------

    def _run_task(self, task) -> None:
        """Start a pooled task, holding a reference until it reports back.

        Without the reference the task can be garbage-collected mid-flight and
        its completion lands on a deleted signals object — which surfaces as
        "Signal source has been deleted" during shutdown.
        """
        self._tasks.add(task)
        signals = task.signals
        for name in ("finished", "error"):
            signal = getattr(signals, name, None)
            if signal is not None:
                signal.connect(lambda *_, t=task: self._tasks.discard(t))
        self._pool.start(task)

    def _notify(self, title: str, message: str) -> None:
        box = QMessageBox(self._ribbon)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(message)
        box.setIcon(QMessageBox.Icon.Information)
        box.exec()

    def quit(self) -> None:
        self.shutdown()
        QApplication.instance().quit()

    def shutdown(self) -> None:
        """Every exit path must reach this.

        The appbar reservation, the taskbar auto-hide state and the wallpaper
        reparent all outlive the process if they are not undone — a leaked
        appbar shrinks the desktop until the shell restarts, and a leaked
        auto-hide silently changes the machine for every other application.
        """
        # First, before anything here can raise: a hidden taskbar is the one
        # piece of leaked state that affects the whole desktop rather than just
        # this app. (If we die before reaching it anyway, start() repairs it.)
        self._autohide.disable()
        self._flush_history()
        self.save_settings()
        # Let in-flight downloads and searches land before the signal objects
        # they emit into are torn down.
        self._pool.waitForDone(3000)
        self._tasks.clear()
        self._appbar.undock()
        self._hotkey.unregister()
        self._wallpaper_stage.shutdown()
        self._stage.shutdown()
        self._ribbon.shutdown()
        self._history.close()
