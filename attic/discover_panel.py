"""Your personalised YouTube feeds, on demand.

Three rules this panel is built around, because it is the one feature that
could quietly undo the app's whole reason for existing:

  * Nothing happens until you open it. No fetch at launch, no polling, no
    background refresh, no resident process. The panel is hidden by default.
  * Feeds are cached to disk, so re-opening shows the last result instantly
    and going back to the network is always an explicit click.
  * Downloading reuses the plumbing the search box already uses. This is a new
    *source* of results, not a second downloader.

It is also the most fragile thing here — it breaks whenever YouTube changes
something — so failures land in the panel's own status line rather than in a
modal that interrupts playback.
"""

import json
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from downloader import FEEDS, CookieSource, FeedTask, SearchResult
from ui.preview import PreviewController
from ui.widgets import format_duration, set_display_font

FEED_TABS = [
    ("music", "MUSIC", "Just the music YouTube is recommending you"),
    ("all", "ALL", "Your full home feed, unfiltered"),
]

BROWSERS = ["firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"]

_SETUP_TEXT = (
    "Reads your real YouTube feeds. Needs your cookies — they go to yt-dlp "
    "and nowhere else."
)

_SETUP_TOOLTIP = (
    "The YouTube Data API has no recommendations endpoint at all, and dropped "
    "watch-history and watch-later years ago. Reading your cookies is the only "
    "way to get the real feeds."
)

_FILE_TOOLTIP = (
    "Export once with any 'Get cookies.txt' browser extension. This is the "
    "reliable route — a file needs no browser running and doesn't care which "
    "browser it came from."
)

_BROWSER_TOOLTIP = (
    "Reads the browser's cookie store directly. Firefox works well; Chrome on "
    "Windows often fails since version 127 added app-bound encryption, and it "
    "generally wants the browser closed."
)


def _cache_dir() -> Path:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    path = Path(base) / "feeds"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _describe_age(fetched: float) -> str:
    seconds = max(0, int(time.time() - fetched))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


class DiscoverPanel(QWidget):
    download_requested = Signal(object)  # SearchResult
    cookies_changed = Signal()

    WIDTH = 300

    def __init__(self, player, volume_getter, cookies: CookieSource, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("discoverPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self.WIDTH)

        self._cookies = cookies
        self._volume_getter = volume_getter
        self._pool = QThreadPool.globalInstance()
        self._task: FeedTask | None = None
        self._results: list[SearchResult] = []
        self._feed_key = FEED_TABS[0][0]
        # Feeds fetched this session, so switching tabs back and forth doesn't
        # re-read the cache file every time.
        self._fetched: set[str] = set()
        # How far down each feed the current page starts. Refresh advances it,
        # so refreshing means "show me more" rather than "show me that again".
        self._offsets: dict[str, int] = {}

        self._preview = PreviewController(player, self)
        # Word-wrapped QLabels under-report their height to a layout, so they
        # get clipped. The panel is a fixed width, so the real wrapped height
        # can just be computed and pinned — see _fit_wrapped_labels.
        self._wrapped: list[QLabel] = []

        heading = QLabel("DISCOVER")
        heading.setObjectName("sectionLabel")
        set_display_font(heading, size=10, spacing=2.4, bold=True)
        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setObjectName("chipButton")
        self._settings_btn.setToolTip("Change the cookie source")
        self._settings_btn.clicked.connect(self._show_setup)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(heading)
        header.addStretch(1)
        header.addWidget(self._settings_btn)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_feed_page())
        # Scrolled, so a short window shrinks the setup page gracefully instead
        # of over-constraining the layout and overlapping widgets.
        setup_scroll = QScrollArea()
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QFrame.Shape.NoFrame)
        setup_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        setup_scroll.setWidget(self._build_setup_page())
        self._stack.addWidget(setup_scroll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(self._stack, stretch=1)

        self._sync_page()

    # --- layout helpers ---

    def _track_wrapped(self, label: QLabel) -> QLabel:
        label.setWordWrap(True)
        self._wrapped.append(label)
        return label

    def _fit_wrapped_labels(self) -> None:
        # Done on show, not at construction: the stylesheet font isn't applied
        # until the widget is polished, so heightForWidth would use the wrong
        # metrics beforehand.
        width = max(80, self.width() - 24)
        for label in self._wrapped:
            label.setMinimumHeight(label.heightForWidth(width))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_wrapped_labels()

    def _set_status(self, text: str, tooltip: str = "") -> None:
        self._status.setText(text)
        self._status.setToolTip(tooltip)
        self._status.setMinimumHeight(
            self._status.heightForWidth(max(80, self.width() - 24))
        )

    # --- construction ---

    def _build_feed_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("transparentPane")

        tabs = QHBoxLayout()
        tabs.setContentsMargins(0, 0, 0, 0)
        tabs.setSpacing(4)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        for index, (key, label, tip) in enumerate(FEED_TABS):
            button = QPushButton(label)
            button.setObjectName("feedTab")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setToolTip(tip)
            set_display_font(button, size=10, spacing=1.2)
            button.clicked.connect(lambda _, k=key: self._select_feed(k))
            self._tab_group.addButton(button, index)
            tabs.addWidget(button)
        tabs.addStretch(1)

        self._list = QListWidget()
        self._list.setObjectName("discoverList")
        self._list.setWordWrap(True)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list.itemDoubleClicked.connect(self._on_item_activated)

        self._status = QLabel("")
        self._status.setObjectName("hintLabel")
        self._status.setWordWrap(True)
        self._preview.status_changed.connect(self._on_preview_status)

        self._preview_btn = QPushButton("▶ PREVIEW")
        self._preview_btn.setObjectName("chipButton")
        self._preview_btn.clicked.connect(self._on_preview)
        self._stop_btn = QPushButton("■")
        self._stop_btn.setObjectName("chipButton")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._preview.stop)
        self._preview.active_changed.connect(self._stop_btn.setEnabled)
        self._refresh_btn = QPushButton("⟳")
        self._refresh_btn.setObjectName("chipButton")
        self._refresh_btn.setToolTip("Re-read your feed from YouTube")
        self._refresh_btn.clicked.connect(
            lambda: self._fetch(self._feed_key, advance=True)
        )
        for button in (self._preview_btn, self._stop_btn, self._refresh_btn):
            set_display_font(button, size=10, spacing=1.2)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        footer.addWidget(self._preview_btn)
        footer.addWidget(self._stop_btn)
        footer.addStretch(1)
        footer.addWidget(self._refresh_btn)

        hint = self._track_wrapped(
            QLabel("Double-click to download into the current playlist.")
        )
        hint.setObjectName("hintLabel")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(tabs)
        layout.addWidget(self._list, stretch=1)
        layout.addWidget(self._status)
        layout.addLayout(footer)
        layout.addWidget(hint)
        return page

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("transparentPane")

        blurb = self._track_wrapped(QLabel(_SETUP_TEXT))
        blurb.setObjectName("hintLabel")
        blurb.setToolTip(_SETUP_TOOLTIP)

        file_btn = QPushButton("USE A cookies.txt FILE…")
        file_btn.setToolTip(_FILE_TOOLTIP)
        file_btn.clicked.connect(self._choose_cookie_file)
        browser_btn = QPushButton("USE A BROWSER…")
        browser_btn.setToolTip(_BROWSER_TOOLTIP)
        browser_btn.clicked.connect(self._choose_browser)
        self._clear_btn = QPushButton("CLEAR")
        self._clear_btn.clicked.connect(self._clear_cookies)
        for button in (file_btn, browser_btn, self._clear_btn):
            set_display_font(button, size=11, spacing=1.4)

        self._cookie_status = self._track_wrapped(QLabel(""))
        self._cookie_status.setObjectName("hintLabel")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(blurb)
        layout.addWidget(file_btn)
        layout.addWidget(browser_btn)
        layout.addStretch(1)
        layout.addWidget(self._cookie_status)
        layout.addWidget(self._clear_btn)
        return page

    # --- cookies ---

    @property
    def cookies(self) -> CookieSource:
        return self._cookies

    def _set_cookies(self, cookies: CookieSource) -> None:
        self._cookies = cookies
        self._fetched.clear()
        self.cookies_changed.emit()
        self._sync_page()

    def _choose_cookie_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select cookies.txt", "", "Cookie files (*.txt);;All files (*)"
        )
        if path:
            self._set_cookies(CookieSource("file", path))

    def _choose_browser(self) -> None:
        name, ok = QInputDialog.getItem(
            self, "Read cookies from browser", "Browser:", BROWSERS, 0, False
        )
        if ok and name:
            self._set_cookies(CookieSource("browser", name))

    def _clear_cookies(self) -> None:
        self._set_cookies(CookieSource())

    def _show_setup(self) -> None:
        self._cookie_status.setText(f"Currently: {self._cookies.describe()}")
        self._fit_wrapped_labels()  # a long file path wraps to more lines
        self._clear_btn.setEnabled(self._cookies.configured)
        self._stack.setCurrentIndex(1)

    def _sync_page(self) -> None:
        if self._cookies.configured:
            self._stack.setCurrentIndex(0)
        else:
            self._show_setup()

    # --- feeds ---

    @property
    def feed_key(self) -> str:
        return self._feed_key

    def set_feed_key(self, key: str) -> None:
        keys = [k for k, _, _ in FEED_TABS]
        if key not in keys:
            return
        self._feed_key = key
        button = self._tab_group.button(keys.index(key))
        if button is not None:
            button.setChecked(True)

    def _select_feed(self, key: str, allow_fetch: bool = True) -> None:
        self._feed_key = key
        self._results = []
        self._list.clear()
        cached = self._load_cache(key)
        if cached is not None:
            items, fetched, offset = cached
            self._offsets[key] = offset
            self._populate(items)
            page = (
                f" · page {self._page_of(key, offset)}"
                if key in FEEDS and FEEDS[key].paginated
                else ""
            )
            self._set_status(f"Cached{page} · {_describe_age(fetched)}")
        elif allow_fetch and key not in self._fetched:
            # Nothing on disk and nothing tried this session. Fetching is still
            # user-initiated: they opened the panel and chose this tab.
            self._fetch(key)
        else:
            self._set_status("Nothing cached — hit ⟳ to fetch.")

    def activate(self, allow_fetch: bool = True) -> None:
        """Called when the panel becomes visible; loads the current tab.

        allow_fetch is False when restoring a panel that was left open at exit,
        so launching the app never touches the network.
        """
        if self._cookies.configured:
            self._select_feed(self._feed_key, allow_fetch=allow_fetch)

    @staticmethod
    def _page_of(key: str, offset: int) -> int:
        depth = FEEDS[key].depth if key in FEEDS else 1
        return offset // max(1, depth) + 1

    def _fetch(self, key: str, advance: bool = False) -> None:
        if not self._cookies.configured:
            self._sync_page()
            return
        spec = FEEDS[key]
        offset = self._offsets.get(key, 0)
        if advance and spec.paginated:
            offset += spec.depth
        elif not spec.paginated:
            offset = 0  # this feed is taken whole; refresh just re-reads it
        self._fetched.add(key)
        self._set_busy(
            True,
            f"Fetching page {self._page_of(key, offset)}…"
            if spec.paginated
            else "Fetching…",
        )
        task = FeedTask(key, self._cookies, offset)
        task.signals.finished.connect(self._on_feed_done)
        task.signals.error.connect(self._on_feed_error)
        self._task = task
        self._pool.start(task)

    def _on_feed_done(self, key: str, offset: int, results: list) -> None:
        self._set_busy(False)

        if not results and offset > 0:
            # Walked off the end of the feed — wrap to the top. Guaranteed to
            # terminate: the retry runs at offset 0, which can't land here.
            self._offsets[key] = 0
            self._set_status("Reached the end of the feed — starting over…")
            self._fetch(key)
            return

        self._offsets[key] = offset
        self._save_cache(key, results, offset)
        if key != self._feed_key:
            return  # user switched tabs mid-fetch
        self._populate(results)
        if results:
            page = (
                f" · page {self._page_of(key, offset)}"
                if FEEDS[key].paginated
                else ""
            )
            self._set_status(f"{len(results)} items{page} · just now")
        else:
            # An unauthenticated or stale-cookie request doesn't fail — YouTube
            # just hands back an empty feed. So empty almost always means the
            # cookies aren't being accepted, not that you have no history.
            self._set_status(
                "Empty feed — your cookies are probably expired or not being "
                "accepted. Re-export them via ⚙."
            )

    def _on_feed_error(self, key: str, message: str) -> None:
        self._set_busy(False)
        if key != self._feed_key:
            return
        lines = [line for line in message.strip().splitlines() if line.strip()]
        summary = lines[-1] if lines else message
        if "cookies" in summary.lower() or "sign in" in summary.lower():
            summary += "  Your cookies may have expired — re-export them."
        self._set_status(summary[:220], tooltip=message)

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._refresh_btn.setEnabled(not busy)
        for button in self._tab_group.buttons():
            button.setEnabled(not busy)
        if text:
            self._set_status(text)

    def _populate(self, results: list[SearchResult]) -> None:
        self._results = results
        self._list.clear()
        for result in results:
            # Music rows come from flat mix entries, which carry no uploader or
            # duration — show the title alone rather than "Unknown · ?:??".
            meta = "  ·  ".join(
                part
                for part in (
                    result.uploader,
                    format_duration(result.duration) if result.duration else "",
                )
                if part
            )
            item = QListWidgetItem(f"{result.title}\n{meta}" if meta else result.title)
            item.setToolTip(result.title)
            self._list.addItem(item)

    # --- cache ---

    def _cache_file(self, key: str) -> Path:
        return _cache_dir() / f"{key}.json"

    def _save_cache(self, key: str, results: list[SearchResult], offset: int = 0) -> None:
        payload = {
            "fetched": time.time(),
            "offset": offset,
            "items": [
                {
                    "video_id": r.video_id,
                    "title": r.title,
                    "uploader": r.uploader,
                    "duration": r.duration,
                }
                for r in results
            ],
        }
        try:
            self._cache_file(key).write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass  # a cache that can't be written is not worth an error dialog

    def _load_cache(self, key: str) -> tuple[list[SearchResult], float, int] | None:
        path = self._cache_file(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = [
                SearchResult(
                    video_id=str(item["video_id"]),
                    title=str(item.get("title") or "Untitled"),
                    uploader=str(item.get("uploader") or "Unknown"),
                    duration=item.get("duration"),
                )
                for item in payload["items"]
            ]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return items, float(payload.get("fetched", 0.0)), int(payload.get("offset", 0))

    # --- selection / actions ---

    def selected_result(self) -> SearchResult | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._results):
            return self._results[row]
        return None

    def _on_preview(self) -> None:
        result = self.selected_result()
        if result is not None:
            self._preview.preview(result.video_id, self._volume_getter())

    def _on_preview_status(self, text: str) -> None:
        if text:
            self._set_status(text)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        row = self._list.row(item)
        if not (0 <= row < len(self._results)):
            return
        # Downloading takes over playback anyway, so stop auditioning but do
        # not resume whatever the preview interrupted.
        self._preview.stop()
        self.download_requested.emit(self._results[row])

    def shutdown(self) -> None:
        self._preview.shutdown()
