"""What you actually listen to, on disk.

This is the taste model the radio is built from, and it is entirely local: no
account, no API, nothing that can expire. It exists because the old Discover
panel outsourced "what does Max like?" to YouTube, which meant re-exporting
cookies every few days to ask a question the app could answer itself.

One row per play, written when a track *stops* being the current one — that is
the only moment we know how much of it was actually heard. A skip is as
informative as a play, so skips are recorded too and simply score negative.

SQLite rather than JSON because this file grows forever and the interesting
queries ("what did I finish a lot of, recently") are aggregations. It is opened
with check_same_thread=False and guarded by a lock: radio lookups run on a
QThreadPool worker while playback writes from the UI thread.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths

# A play counts as "finished" past this much of the track. Slowed + reverb
# edits run long and get left on; 70% is late enough that a genuine skip never
# reaches it, early enough that wandering off before the outro still counts.
COMPLETION_RATIO = 0.70

# Anything shorter than this is a scrub through the playlist, not a listen, and
# is dropped rather than recorded as a skip. Hammering "next" six times would
# otherwise bury six tracks in negative score.
MIN_MEANINGFUL_MS = 5_000

# Recency half-life. A track you hammered in March should not outrank one you
# have been playing all week, but taste is slow, so this is generous.
HALF_LIFE_DAYS = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY,
    path        TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    artist      TEXT    NOT NULL DEFAULT '',
    started_at  REAL    NOT NULL,
    played_ms   INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    completed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plays_path    ON plays(path);
CREATE INDEX IF NOT EXISTS idx_plays_started ON plays(started_at);
"""


@dataclass(frozen=True)
class Seed:
    """A track worth building recommendations from."""

    path: str
    title: str
    artist: str
    score: float
    plays: int
    skips: int

    @property
    def label(self) -> str:
        """How this track should be named in a "because you played…" line."""
        if self.artist and self.title:
            return f"{self.artist} — {self.title}"
        return self.title or Path(self.path).stem


def default_path() -> Path:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    folder = Path(base) if base else Path.home() / ".mp3player"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "history.db"


class History:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- writing ---

    def record(
        self,
        path: str,
        title: str = "",
        artist: str = "",
        played_ms: int = 0,
        duration_ms: int = 0,
        started_at: float | None = None,
    ) -> bool:
        """Log one finished-with listen. Returns False if it was too short to count."""
        if played_ms < MIN_MEANINGFUL_MS:
            return False
        completed = bool(
            duration_ms > 0 and played_ms >= duration_ms * COMPLETION_RATIO
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO plays"
                " (path, title, artist, started_at, played_ms, duration_ms, completed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(path),
                    title or "",
                    artist or "",
                    started_at if started_at is not None else time.time(),
                    int(played_ms),
                    int(duration_ms),
                    int(completed),
                ),
            )
            self._db.commit()
        return True

    # --- reading ---

    def _decayed(self, rows) -> dict[str, dict]:
        """Collapse raw play rows into per-path scores with recency decay.

        Decay is applied in Python rather than SQL because SQLite has no exp().
        Doing it per row (not per aggregate) is what lets one recent completion
        outweigh a pile of old ones.
        """
        now = time.time()
        bucket: dict[str, dict] = {}
        for row in rows:
            entry = bucket.setdefault(
                row["path"],
                {
                    "path": row["path"],
                    "title": "",
                    "artist": "",
                    "score": 0.0,
                    "plays": 0,
                    "skips": 0,
                },
            )
            # Keep the most recent non-empty naming; tags can improve over time.
            if row["title"]:
                entry["title"] = row["title"]
            if row["artist"]:
                entry["artist"] = row["artist"]

            age_days = max(0.0, (now - row["started_at"]) / 86_400.0)
            weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
            if row["completed"]:
                entry["score"] += weight
                entry["plays"] += 1
            else:
                # A skip is evidence, just negative — and worth less than a
                # completion, because skipping often means "not right now"
                # rather than "never again".
                entry["score"] -= weight * 0.5
                entry["skips"] += 1
        return bucket

    def seeds(self, limit: int = 12) -> list[Seed]:
        """Your current favourites, best first. The radio starts from these."""
        with self._lock:
            rows = self._db.execute(
                "SELECT path, title, artist, started_at, completed FROM plays"
                " ORDER BY started_at DESC LIMIT 4000"
            ).fetchall()
        scored = [
            Seed(
                path=entry["path"],
                title=entry["title"],
                artist=entry["artist"],
                score=entry["score"],
                plays=entry["plays"],
                skips=entry["skips"],
            )
            for entry in self._decayed(rows).values()
        ]
        scored = [seed for seed in scored if seed.score > 0]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:limit]

    def top_artists(self, limit: int = 8) -> list[tuple[str, float]]:
        """Artist affinity, for the artist-level lane of the radio.

        Grouped case-insensitively: these names come from YouTube-rip titles,
        where the same artist appears as "drake", "Drake" and "DRAKE" across
        three uploaders. The display name is whichever spelling scored highest.
        """
        totals: dict[str, float] = {}
        display: dict[str, tuple[float, str]] = {}
        for seed in self.seeds(limit=200):
            if not seed.artist:
                continue
            name = seed.artist.strip()
            key = name.lower()
            totals[key] = totals.get(key, 0.0) + seed.score
            best = display.get(key)
            if best is None or seed.score > best[0]:
                display[key] = (seed.score, name)
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        return [(display[key][1], score) for key, score in ranked[:limit]]

    def stats(self, path: str) -> tuple[int, int]:
        """(completions, skips) for one track."""
        with self._lock:
            row = self._db.execute(
                "SELECT"
                "  SUM(CASE WHEN completed THEN 1 ELSE 0 END) AS plays,"
                "  SUM(CASE WHEN completed THEN 0 ELSE 1 END) AS skips"
                " FROM plays WHERE path = ?",
                (str(path),),
            ).fetchone()
        return int(row["plays"] or 0), int(row["skips"] or 0)

    def recent_paths(self, limit: int = 40) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT path FROM plays ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [row["path"] for row in rows]

    def total_plays(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM plays").fetchone()
        return int(row["n"] or 0)
