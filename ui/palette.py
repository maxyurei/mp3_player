"""Type-to-do-anything overlay. Summoned by hotkey, gone on Escape.

This is what lets the app stop having a window. The old build kept a 1040x640
library browser open so you had somewhere to browse from; browsing is a thing
you do in bursts, so it is better served by something that appears over your
editor for four seconds and then isn't there.

One field, one list, four kinds of row ranked together:

    LOCAL   a track already on disk           -> plays instantly
    RADIO   build a queue from a seed track   -> history x YouTube mixes
    FETCH   a YouTube result                  -> downloads, then plays
    ACTION  run the search that produces FETCH rows

Local matching is synchronous and re-ranks on every keystroke — it is a list
comprehension over a few hundred tracks. The YouTube lane is debounced, because
it costs a subprocess and a network round trip, and firing one per keystroke
would mean six of them for the word "unravel".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ui import theme

WIDTH = 620
MAX_VISIBLE_ROWS = 8
ROW_HEIGHT = 48
SEARCH_DEBOUNCE_MS = 450
MIN_QUERY_FOR_FETCH = 2

ROLE_ROW = Qt.ItemDataRole.UserRole + 1

CHIP_COLOURS = {
    "LOCAL": theme.MUTED,
    "RADIO": theme.CYAN,
    "FETCH": theme.VIOLET_HI,
    "ACTION": theme.SUBDUED,
}


@dataclass
class Row:
    kind: str  # "local" | "radio" | "fetch" | "action"
    title: str
    subtitle: str = ""
    chip: str = ""
    payload: Any = None
    playing: bool = False
    meta: dict = field(default_factory=dict)


class _RowDelegate(QStyledItemDelegate):
    """Two-line rows with a right-aligned source chip.

    Painted rather than composed from widgets: a QWidget per row costs a layout
    pass and a few hundred bytes each, and this list is rebuilt on every
    keystroke.
    """

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        row: Row = index.data(ROLE_ROW)
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if selected:
            fill = QColor(theme.VIOLET)
            fill.setAlpha(30)
            painter.fillRect(rect, fill)
            painter.fillRect(QRect(rect.x(), rect.y(), 2, rect.height()),
                             QColor(theme.VIOLET))

        # Chip first: the text has to be elided against whatever it leaves.
        chip_left = rect.right() - 12
        if row.chip:
            chip_font = QFont(painter.font())
            chip_font.setPixelSize(9)
            chip_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
            metrics = QFontMetrics(chip_font)
            width = metrics.horizontalAdvance(row.chip) + 14
            chip_rect = QRect(rect.right() - width - 12,
                              rect.y() + (rect.height() - 18) // 2, width, 18)
            colour = QColor(CHIP_COLOURS.get(row.chip, theme.MUTED))
            pen_colour = QColor(colour)
            pen_colour.setAlpha(110)
            painter.setPen(pen_colour)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(chip_rect, 3, 3)
            painter.setFont(chip_font)
            painter.setPen(colour)
            painter.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, row.chip)
            chip_left = chip_rect.left() - 12

        text_left = rect.x() + 18
        available = max(40, chip_left - text_left)

        title_font = QFont(painter.font())
        title_font.setPixelSize(13)
        title_font.setWeight(QFont.Weight.DemiBold)
        title_metrics = QFontMetrics(title_font)
        painter.setFont(title_font)
        # Cyan means live audio everywhere in this app, including here.
        painter.setPen(QColor(theme.CYAN if row.playing else theme.TEXT))
        baseline = rect.y() + (rect.height() // 2) - 3
        painter.drawText(
            text_left,
            baseline,
            title_metrics.elidedText(row.title, Qt.TextElideMode.ElideRight, available),
        )

        if row.subtitle:
            sub_font = QFont(painter.font())
            sub_font.setPixelSize(11)
            sub_metrics = QFontMetrics(sub_font)
            painter.setFont(sub_font)
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(
                text_left,
                baseline + sub_metrics.height() + 1,
                sub_metrics.elidedText(
                    row.subtitle, Qt.TextElideMode.ElideRight, available
                ),
            )
        painter.restore()


class Palette(QWidget):
    play_requested = Signal(object)  # Track
    queue_requested = Signal(object)  # Track
    radio_requested = Signal(object)  # Track | None
    fetch_requested = Signal(object)  # SearchResult
    search_requested = Signal(str)  # query -> caller runs SearchTask

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setObjectName("palette")

        self._tracks: list = []
        self._names: dict = {}
        self._current_path: Path | None = None
        self._fetch_rows: list[Row] = []
        self._pending_query = ""

        self.setStyleSheet(f"""
            QWidget#palette {{
                background-color: {theme.PANEL};
                border: 1px solid {theme.BORDER_HI};
            }}
            QLineEdit {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {theme.BORDER};
                color: {theme.TEXT};
                font-size: 15px;
                padding: 14px 16px;
                selection-background-color: {theme.VIOLET_DIM};
            }}
            QListWidget {{
                background: transparent;
                border: none;
                outline: none;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._input = QLineEdit(self)
        self._input.setPlaceholderText(
            "Play, queue, start a radio, or fetch from YouTube…"
        )
        self._input.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._input)

        self._list = QListWidget(self)
        self._list.setItemDelegate(_RowDelegate(self._list))
        self._list.setUniformItemSizes(True)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list.itemActivated.connect(lambda _: self._activate(False))
        self._list.itemClicked.connect(lambda _: self._activate(False))
        layout.addWidget(self._list)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire_search)

        self.resize(WIDTH, 60)

    # ------------------------------------------------------------------
    # state from the app
    # ------------------------------------------------------------------

    def set_library(self, tracks: list, names: dict | None = None) -> None:
        """`names` maps path -> (title, artist), already cleaned by the caller.

        Falls back to the raw tags when absent, which is only correct for
        properly tagged files — see PlayerApp._describe for why that is the
        exception rather than the rule in this library.
        """
        self._tracks = list(tracks)
        self._names = names or {}

    def set_current(self, path: Path | None) -> None:
        self._current_path = path

    def set_fetch_results(self, query: str, results: list) -> None:
        """Called back when the caller's SearchTask finishes."""
        if query != self._pending_query:
            return  # a newer query has already been typed
        self._fetch_rows = [
            Row(
                kind="fetch",
                title=result.title,
                subtitle=self._describe_result(result),
                chip="FETCH",
                payload=result,
            )
            for result in results
        ]
        self._rebuild()

    @staticmethod
    def _describe_result(result) -> str:
        bits = []
        if getattr(result, "uploader", ""):
            bits.append(result.uploader)
        duration = getattr(result, "duration", None)
        if duration:
            bits.append(f"{int(duration) // 60}:{int(duration) % 60:02d}")
        bits.append("downloads to your library")
        return " · ".join(bits)

    # ------------------------------------------------------------------
    # showing
    # ------------------------------------------------------------------

    def summon(self) -> None:
        self._input.clear()
        self._fetch_rows = []
        self._pending_query = ""
        self._rebuild()
        self._centre()
        self.show()
        self.raise_()
        self.activateWindow()
        self._input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _centre(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        # A third of the way down, not centred: a dialog pinned to the exact
        # middle sits over whatever you were reading.
        self.move(
            area.center().x() - self.width() // 2,
            area.top() + area.height() // 4,
        )

    def _resize_to_content(self) -> None:
        rows = min(self._list.count(), MAX_VISIBLE_ROWS)
        self.resize(WIDTH, self._input.height() + rows * ROW_HEIGHT + 2)

    # ------------------------------------------------------------------
    # searching
    # ------------------------------------------------------------------

    def _on_text_changed(self, text: str) -> None:
        self._fetch_rows = []
        self._rebuild()
        query = text.strip()
        if len(query) >= MIN_QUERY_FOR_FETCH:
            self._debounce.start()
        else:
            self._debounce.stop()

    def _fire_search(self) -> None:
        query = self._input.text().strip()
        if len(query) < MIN_QUERY_FOR_FETCH:
            return
        self._pending_query = query
        self.search_requested.emit(query)

    def _score(self, track, needle: str) -> int | None:
        """Rank a library track against the query, or None if it doesn't match.

        Prefix beats word-start beats substring. Without the tiering, typing
        "juice" surfaces whatever happens to be alphabetically first rather
        than the track actually called "juice wrld — …".
        """
        cleaned_title, cleaned_artist = self._names.get(track.path, ("", ""))
        haystacks = [
            track.display_name.lower(),
            (track.title or "").lower(),
            # The cleaned pair too, so "juice wrld end of the road" matches a
            # file whose raw name is buried in "(slowed + reverb) [id]" noise.
            cleaned_title.lower(),
            cleaned_artist.lower(),
        ]
        best: int | None = None
        for text in haystacks:
            if not text:
                continue
            index = text.find(needle)
            if index < 0:
                continue
            if index == 0:
                score = 0
            elif text[index - 1] in " -_([":
                score = 1
            else:
                score = 2
            best = score if best is None else min(best, score)
        return best

    def _local_rows(self, needle: str) -> list[Row]:
        if not needle:
            # Nothing typed: offer the library in a stable order rather than an
            # empty pane, so the palette is also just a track list.
            candidates = [(0, track) for track in self._tracks[:MAX_VISIBLE_ROWS]]
        else:
            scored = []
            for track in self._tracks:
                score = self._score(track, needle)
                if score is not None:
                    scored.append((score, track))
            scored.sort(key=lambda pair: (pair[0], pair[1].display_name.lower()))
            candidates = scored[:MAX_VISIBLE_ROWS]

        rows = []
        for _, track in candidates:
            title, artist = self._names.get(
                track.path, (track.title or track.display_name, track.artist or "")
            )
            # Folder name as the fallback subtitle: for a track with no
            # recoverable artist, which playlist it came from is the most
            # useful thing left to say about it.
            subtitle = " · ".join(
                part for part in (artist, track.path.parent.name) if part
            )
            rows.append(
                Row(
                    kind="local",
                    title=title,
                    subtitle=subtitle,
                    chip="LOCAL",
                    payload=track,
                    playing=(self._current_path is not None
                             and Path(track.path) == self._current_path),
                )
            )
        return rows

    def _rebuild(self) -> None:
        needle = self._input.text().strip().lower()
        rows: list[Row] = []

        local = self._local_rows(needle)
        rows.extend(local)

        # Radio seeds off the best local match, so it means "more like this"
        # rather than "more like something you typed".
        if local:
            seed = local[0].payload
            rows.append(
                Row(
                    kind="radio",
                    title=f"Radio from “{local[0].title}”",
                    subtitle="builds a queue from your history and YouTube mixes",
                    chip="RADIO",
                    payload=seed,
                )
            )
        elif not needle:
            rows.append(
                Row(
                    kind="radio",
                    title="Start a radio",
                    subtitle="from everything you have been playing lately",
                    chip="RADIO",
                    payload=None,
                )
            )

        rows.extend(self._fetch_rows)

        if needle and not self._fetch_rows:
            waiting = self._debounce.isActive() or bool(self._pending_query)
            rows.append(
                Row(
                    kind="action",
                    title=f"Search YouTube for “{self._input.text().strip()}”",
                    subtitle="searching…" if waiting else "press Enter",
                    chip="ACTION",
                    payload=self._input.text().strip(),
                )
            )

        self._list.clear()
        for row in rows:
            item = QListWidgetItem()
            item.setData(ROLE_ROW, row)
            item.setSizeHint(QSize(WIDTH, ROW_HEIGHT))
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        self._resize_to_content()

    # ------------------------------------------------------------------
    # keyboard
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            count = self._list.count()
            if count:
                step = 1 if key == Qt.Key.Key_Down else -1
                self._list.setCurrentRow(
                    (self._list.currentRow() + step) % count
                )
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._activate(bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            event.accept()
            return
        super().keyPressEvent(event)

    def _activate(self, queue: bool) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        row: Row = item.data(ROLE_ROW)
        if row is None:
            return

        if row.kind == "local":
            (self.queue_requested if queue else self.play_requested).emit(row.payload)
        elif row.kind == "radio":
            self.radio_requested.emit(row.payload)
        elif row.kind == "fetch":
            self.fetch_requested.emit(row.payload)
        elif row.kind == "action":
            self._fire_search()
            self._rebuild()
            return  # stay open; results land in a moment
        self.hide()

    def focusOutEvent(self, event) -> None:
        # Clicking away dismisses, same as Escape. Without this the overlay can
        # be left stranded on top of whatever you switched to.
        super().focusOutEvent(event)
        if not self.isActiveWindow():
            self.hide()

    def hideEvent(self, event) -> None:
        self._debounce.stop()
        super().hideEvent(event)
