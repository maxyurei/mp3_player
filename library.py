import re
from dataclasses import dataclass
from pathlib import Path

from mutagen import MutagenError
from mutagen.id3 import ID3
from mutagen.mp3 import MP3


# Downloads are saved as "Title [videoid].mp3" so the id doubles as a cache key
# (see downloader.download). It is pure noise in the track list, so strip it for
# display only — every lookup still goes through the real filename.
_VIDEO_ID_SUFFIX = re.compile(r"\s*\[[A-Za-z0-9_-]{11}\]$")


@dataclass
class Track:
    path: Path
    title: str | None = None
    artist: str | None = None
    album: str | None = None

    @property
    def display_name(self) -> str:
        return _VIDEO_ID_SUFFIX.sub("", self.path.stem)


def _read_tags(path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        tags = ID3(str(path))
    except MutagenError:
        # Untagged or unreadable — we degrade to filename downstream.
        return None, None, None

    def first(key: str) -> str | None:
        if key not in tags:
            return None
        value = str(tags[key]).strip()
        return value or None

    return first("TIT2"), first("TPE1"), first("TALB")


def scan_folder(folder: Path) -> list[Track]:
    tracks: list[Track] = []
    # rglob is case-insensitive on Windows, so "*.mp3" also matches "*.MP3".
    for path in sorted(folder.rglob("*.mp3")):
        title, artist, album = _read_tags(path)
        tracks.append(Track(path=path, title=title, artist=artist, album=album))
    return tracks


def read_stream_info(path: Path) -> tuple[int | None, int | None]:
    """Return (bitrate in bits/s, sample rate in Hz) for the status readout.

    Read on track start rather than at scan time — it parses the MP3 header,
    which is cheap once but not cheap a thousand times during a folder scan.
    """
    try:
        info = MP3(str(path)).info
    except (MutagenError, OSError, ValueError):
        return None, None
    return getattr(info, "bitrate", None), getattr(info, "sample_rate", None)


def load_cover(path: Path) -> bytes | None:
    # Loaded lazily per-track rather than at scan time: a 1000-track library
    # with 100 KB covers would otherwise eat ~100 MB just sitting in RAM.
    try:
        tags = ID3(str(path))
    except MutagenError:
        return None
    apic_frames = tags.getall("APIC")
    if not apic_frames:
        return None
    return apic_frames[0].data
