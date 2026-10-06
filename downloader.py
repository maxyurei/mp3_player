"""On-demand YouTube search + download via yt-dlp.

Design goals (see also the project's memory constraints):
  * yt-dlp is NEVER imported into this process. It is invoked as a short-lived
    subprocess (`pythonw -m yt_dlp ...`) that spins up for a single search or
    download and exits immediately, so the resident app stays lightweight.
  * Everything yt-dlp-specific lives in this module's _run/search/download
    functions, so when YouTube breaks yt-dlp you only edit here.
  * The Qt worker classes (SearchTask/DownloadTask) run the blocking subprocess
    off the UI thread via QThreadPool and report back through signals.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

# Invoke yt-dlp through the *current* interpreter so it always resolves to the
# yt-dlp installed in this app's environment (the .bat launches via pythonw, so
# a bare "yt-dlp" on PATH is not guaranteed). The child process loads yt_dlp,
# does its work, and exits — nothing stays resident here.
YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]

# CREATE_NO_WINDOW: the app runs under pythonw (no console), so without this a
# console window would flash on every subprocess call.
_CREATE_NO_WINDOW = 0x08000000


class YtDlpError(Exception):
    """yt-dlp failed; message carries its (surfaced) error output."""


class FfmpegMissingError(Exception):
    """ffmpeg is required for MP3 conversion / art embedding but wasn't found."""


@dataclass
class SearchResult:
    video_id: str
    title: str
    uploader: str
    duration: int | None  # seconds, or None for live/unknown


def _subprocess_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}


def _run(args: list[str], timeout: int) -> str:
    """Run yt-dlp with the given args; return stdout or raise YtDlpError."""
    try:
        proc = subprocess.run(
            YTDLP_CMD + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            **_subprocess_kwargs(),
        )
    except FileNotFoundError as exc:  # interpreter itself not found — unlikely
        raise YtDlpError(f"Could not launch yt-dlp: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise YtDlpError(
            "yt-dlp timed out. Your network may be down or the site is slow."
        ) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "No module named yt_dlp" in stderr:
            raise YtDlpError(
                "yt-dlp is not installed. Install it with:\n\n"
                "    pip install -U yt-dlp"
            )
        raise YtDlpError(stderr or "yt-dlp failed with no error output.")
    return proc.stdout


def _entries(out: str) -> list[dict]:
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise YtDlpError("Could not parse yt-dlp output.") from exc
    return [entry for entry in (data.get("entries") or []) if entry]


def _parse_results(out: str) -> list[SearchResult]:
    """Turn a --dump-single-json --flat-playlist payload into SearchResults."""
    results: list[SearchResult] = []
    for entry in _entries(out):
        if not entry.get("id"):
            continue
        duration = entry.get("duration")
        results.append(
            SearchResult(
                video_id=entry["id"],
                title=entry.get("title") or "Untitled",
                uploader=entry.get("uploader") or entry.get("channel") or "Unknown",
                duration=int(duration) if duration else None,
            )
        )
    return results


def search(query: str, count: int = 5, timeout: int = 60) -> list[SearchResult]:
    """Return up to `count` YouTube results for `query` (ytsearch, no download)."""
    # --flat-playlist keeps this fast and light: it lists the search results
    # without extracting each individual video page.
    return _parse_results(
        _run(
            [
                f"ytsearch{count}:{query}",
                "--dump-single-json",
                "--flat-playlist",
                "--no-warnings",
            ],
            timeout=timeout,
        )
    )


def download(video_id: str, dest_folder: Path, timeout: int = 300) -> Path:
    """Download `video_id` as MP3 into `dest_folder` and return the file path.

    Embeds metadata (title/artist) and the video thumbnail as album art. The
    output filename includes the video id (`Title [id].mp3`) so it doubles as a
    cache key — see MainWindow._find_cached.
    """
    if shutil.which("ffmpeg") is None:
        raise FfmpegMissingError(
            "ffmpeg is required to convert downloads to MP3, but it wasn't "
            "found on PATH.\n\nInstall it with:\n\n"
            "    winget install Gyan.FFmpeg\n\n"
            "then restart the app."
        )

    dest_folder.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_template = str(dest_folder / "%(title)s [%(id)s].%(ext)s")

    _run(
        [
            url,
            "--no-playlist",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "--embed-thumbnail",
            "--embed-metadata",
            "--output",
            out_template,
            "--no-warnings",
        ],
        timeout=timeout,
    )

    # Locate the produced file by its embedded id. Globbing for "[id]" directly
    # is awkward (brackets are glob char-classes), so scan and match by name.
    tag = f"[{video_id}]"
    matches = [p for p in dest_folder.glob("*.mp3") if tag in p.name]
    if not matches:
        raise YtDlpError(
            "Download reported success but no MP3 file was found. ffmpeg may "
            "have failed to convert the audio."
        )
    # Most recently written wins, in case of an older partial.
    return max(matches, key=lambda p: p.stat().st_mtime)


def stream_url(video_id: str, timeout: int = 60) -> str:
    """Return a direct audio stream URL for `video_id` (no download).

    Prefers the m4a/AAC stream because Qt's Media Foundation backend plays it
    reliably for in-app preview; opus/webm may not be playable everywhere.
    """
    out = _run(
        [
            f"https://www.youtube.com/watch?v={video_id}",
            "--no-playlist",
            "-f",
            "bestaudio[ext=m4a]/bestaudio",
            "-g",
            "--no-warnings",
        ],
        timeout=timeout,
    )
    urls = [line.strip() for line in out.splitlines() if line.strip()]
    if not urls:
        raise YtDlpError("No playable audio stream was returned for preview.")
    return urls[0]


# --- Qt workers: run the blocking calls above off the UI thread -------------


class _SearchSignals(QObject):
    finished = Signal(list)  # list[SearchResult]
    error = Signal(str)


class SearchTask(QRunnable):
    def __init__(self, query: str) -> None:
        super().__init__()
        self.query = query
        self.signals = _SearchSignals()

    def run(self) -> None:
        try:
            results = search(self.query)
        except YtDlpError as exc:
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(results)


class _PreviewSignals(QObject):
    finished = Signal(str)  # direct audio stream URL
    error = Signal(str)


class PreviewTask(QRunnable):
    def __init__(self, video_id: str) -> None:
        super().__init__()
        self.video_id = video_id
        self.signals = _PreviewSignals()

    def run(self) -> None:
        try:
            url = stream_url(self.video_id)
        except YtDlpError as exc:
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(url)


class _DownloadSignals(QObject):
    finished = Signal(str)  # path to the downloaded MP3
    error = Signal(str)


class DownloadTask(QRunnable):
    def __init__(self, video_id: str, dest_folder: Path) -> None:
        super().__init__()
        self.video_id = video_id
        self.dest_folder = dest_folder
        self.signals = _DownloadSignals()

    def run(self) -> None:
        try:
            path = download(self.video_id, self.dest_folder)
        except (YtDlpError, FfmpegMissingError) as exc:
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(str(path))
