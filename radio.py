"""Recommendations, built from your own listening. No account, no cookies.

Replaces the old Discover panel, which read your personalised YouTube feed and
therefore needed browser cookies that expired every few days.

Two lanes, both keyless and both anonymous:

  1. MIX  — YouTube's own "RD" radio playlist, seeded from a track you actually
     finished. Reached with plain yt-dlp, no cookies. This is the strong lane
     for this library specifically: it is almost entirely slowed + reverb
     YouTube rips, a scene MusicBrainz has no concept of and Deezer barely
     models, but which YouTube's mixes navigate natively. Measured: seeding
     from a slowed Billy Idol rip returned slowed depeche mode, the weeknd,
     chris isaak and Cutting Crew — the right neighbourhood, first try.

  2. ARTIST — Deezer's public related-artists endpoint (no API key), resolved
     back to audio with a normal ytsearch. This lane exists to break out of the
     mix's gravity: mixes stay very close to the seed, so left alone the radio
     would only ever play more of the same evening. It also supplies clean
     canonical artist names, which YouTube titles never do.

ListenBrainz was the original plan and was dropped after measuring it: its
LB Radio endpoint answers HTTP 500 "currently disabled due to high load", and
similar-artists returns an empty list even for artists as large as Drake.

yt-dlp is still never imported — downloader._run shells out to it, and this
module reuses that path rather than opening a second one.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from downloader import SearchResult, YtDlpError, _entries, _run

# --- naming -----------------------------------------------------------------

# Everything that turns up in a YouTube-rip filename and is not part of the
# artist or the title. Order matters: the 11-character video id goes first, so
# the general bracket rule doesn't eat half of it and leave a stump behind.
_NOISE = [
    r"\[[A-Za-z0-9_\-]{11}\]",
    r"\(\s*(?:official|unofficial)[^)]*\)",
    r"\[[^\]]*\]",
    # The leading \s* matters: uploaders write "( s l o w e d + r e v e r b )".
    # Without it the bracket survives, the contents get eaten by the standalone
    # rules below, and the title keeps an empty "( + )".
    r"\(\s*(?:super\s+)?s\s*l\s*o\s*w\s*e\s*d[^)]*\)",
    r"\(\s*[^)]*reverb[^)]*\)",
    r"\(\s*[^)]*slowed[^)]*\)",
    r"\(\s*[^)]*432\s*hz[^)]*\)",
    r"\(\s*[^)]*remix[^)]*\)",
    r"\(\s*[^)]*\bmix\s*\)",
    r"\(\s*[^)]*version\s*\)",
    r"\bslowed\s*(?:\+|x|and|&|to)?\s*(?:perfection|reverb)\b",
    r"\bsuper\s+slowed\b",
    r"\bs\s+l\s+o\s+w\s+e\s+d\b",
    r"\br\s+e\s+v\s+e\s+r\s+b\b",
    r"\breverb\b",
    r"\bslowed\b",
    r"\bsped\s*up\b",
    r"\bprod\.?\s*by\s+\S+",
    # Full-width brackets, contents and all. Stripping only the bracket
    # characters leaves the noise they were wrapping ("cigarettes + 432hz").
    r"〔[^〕]*〕",
    r"（[^）]*）",
    r"［[^］]*］",
    r"[〔〕（）［］｜]",
    r"\b432\s*hz\b",
]

# Applied after the noise rules, repeatedly: stripping "[Slowed & Reverb]" out
# of "Eyes Without A Face [Slowed & Reverb] + [No Guitar Solo Part]" leaves a
# dangling "+", and removing one empty bracket can expose another.
_EMPTY_BRACKETS = re.compile(r"[(\[]\s*[^A-Za-z0-9)\]]*\s*[)\]]")
_EDGE_PUNCT = " -–—_·|+&,.:;~"

# Real hyphenated separator, or the run of spaces that YouTube uploaders leave
# where a hyphen used to be ("Trippie Redd   Taking A Walk").
_SPLIT = re.compile(r"\s+[-–—]\s+|\s{3,}")

_YT_ID = re.compile(r"\[([A-Za-z0-9_\-]{11})\]")

# Uploader names that mean "someone who posts slowed edits", not an artist.
# The ID3 artist tag on these rips is the *uploader*, so it can't be trusted:
# measured across the library it yields "FineTunes", "slowedmusic4u",
# "YourLocalSchizo". The title tag, by contrast, usually holds the real
# "Artist - Title" — so that is what gets parsed, and the artist tag ignored.
_UPLOADER_HINTS = ("slowed", "reverb", "music", "tunes", "records", "audio")


def _tidy(text: str) -> str:
    """Drop empty brackets and edge punctuation left behind by the noise rules."""
    for _ in range(3):
        cleaned = _EMPTY_BRACKETS.sub(" ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(_EDGE_PUNCT)
        if cleaned == text:
            break
        text = cleaned
    return text


def strip_noise(text: str) -> str:
    for pattern in _NOISE:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return _tidy(text)


def split_artist_title(text: str) -> tuple[str, str] | None:
    cleaned = strip_noise(text)
    if not cleaned:
        return None
    parts = _SPLIT.split(cleaned, maxsplit=1)
    if len(parts) != 2:
        return None
    # Tidy each half separately: the split can expose punctuation that was
    # interior a moment ago ("… - Slowed + Reverb | Agartha Remix" leaves the
    # title starting with a pipe).
    artist, title = _tidy(parts[0]), _tidy(parts[1])
    if not artist or not title:
        return None
    return artist, title


def describe(path: str, tag_title: str = "", tag_artist: str = "") -> tuple[str, str]:
    """Best available (artist, title) for a file.

    Tries the title tag first — on a YouTube rip that is where the real
    "Artist - Title" lives — then the filename. Falls back to a cleaned stem
    with no artist, which the radio treats as title-only and still searches on.
    """
    for candidate in (tag_title, Path(path).stem):
        if not candidate:
            continue
        split = split_artist_title(candidate)
        if split is not None:
            return split

    # No separator anywhere. Only trust the artist tag if it doesn't look like
    # a slowed-edit channel.
    artist = ""
    if tag_artist and not any(h in tag_artist.lower() for h in _UPLOADER_HINTS):
        artist = tag_artist.strip()
    title = strip_noise(tag_title or Path(path).stem)
    return artist, title


def youtube_id(path: str) -> str | None:
    """The video id yt-dlp embedded in the filename, if it survived."""
    match = _YT_ID.search(Path(path).stem)
    return match.group(1) if match else None


def _key(title: str) -> str:
    """Loose identity for dedupe: lowercase alphanumerics of the cleaned title."""
    return re.sub(r"[^a-z0-9]+", "", strip_noise(title).lower())


# --- lane 1: YouTube mixes --------------------------------------------------


def mix(video_id: str, limit: int = 20, timeout: int = 90) -> list[SearchResult]:
    """The RD radio playlist YouTube builds around one video. No cookies.

    Cookies must never be attached here. An authenticated YouTube request needs
    a proof-of-origin token, and without one every playable format is filtered
    out — the failure the old Discover panel spent so long working around. This
    request is anonymous by construction, which is the entire point.
    """
    out = _run(
        [
            f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}",
            "--dump-single-json",
            "--flat-playlist",
            "--playlist-items",
            f"1-{max(1, limit)}",
            "--no-warnings",
        ],
        timeout=timeout,
    )
    results: list[SearchResult] = []
    for entry in _entries(out):
        entry_id = entry.get("id")
        # The seed itself is always row 1 of its own mix.
        if not entry_id or entry_id == video_id:
            continue
        duration = entry.get("duration")
        results.append(
            SearchResult(
                video_id=entry_id,
                title=entry.get("title") or "Untitled",
                uploader=entry.get("uploader") or entry.get("channel") or "",
                duration=int(duration) if duration else None,
            )
        )
    return results


def resolve(query: str, timeout: int = 60) -> str | None:
    """One ytsearch hit, for files whose filename lost its video id."""
    out = _run(
        [
            f"ytsearch1:{query}",
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
        ],
        timeout=timeout,
    )
    for entry in _entries(out):
        if entry.get("id"):
            return str(entry["id"])
    return None


# --- lane 2: Deezer related artists -----------------------------------------

_DEEZER_UA = "MP3Player/2.0 (personal music player)"


def _deezer(url: str, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": _DEEZER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        # This lane is a bonus on top of the mixes. If Deezer is unreachable the
        # radio should quietly be a little less varied, not fail.
        return {}


def related_artists(name: str, limit: int = 8) -> list[str]:
    """Artists Deezer considers adjacent to `name`. No API key required."""
    found = _deezer(
        "https://api.deezer.com/search/artist?limit=1&q="
        + urllib.parse.quote(name)
    )
    hits = found.get("data") or []
    if not hits:
        return []
    related = _deezer(
        f"https://api.deezer.com/artist/{hits[0]['id']}/related?limit={max(1, limit)}"
    )
    return [item["name"] for item in (related.get("data") or []) if item.get("name")]


# --- assembling the queue ---------------------------------------------------


@dataclass
class Recommendation:
    result: SearchResult
    reason: str  # shown verbatim in the UI — always explains itself
    lane: str  # "mix" | "artist"


def _diversify(seeds, limit: int):
    """At most one seed per artist, best first.

    Without this the radio seeds every mix from whichever artist you happened
    to play most that week — two Drake tracks produce two Drake mixes and a
    queue that is entirely Drake. One seed per artist is what makes the
    round-robin below actually interleave anything.
    """
    picked = []
    used: set[str] = set()
    for seed in seeds:
        key = (seed.artist or "").strip().lower()
        if key and key in used:
            continue
        if key:
            used.add(key)
        picked.append(seed)
        if len(picked) >= limit:
            break
    return picked


def build(
    seeds,
    exclude_keys: set[str] | None = None,
    per_seed: int = 12,
    max_seeds: int = 4,
    artist_lane: bool = True,
    limit: int = 40,
) -> list[Recommendation]:
    """Turn history seeds into a ranked, deduplicated recommendation list.

    Results are interleaved round-robin across seeds rather than concatenated,
    so the top of the list reflects your whole taste instead of whichever track
    happened to score highest this week.
    """
    exclude = set(exclude_keys or ())
    seen: set[str] = set()
    lanes: list[list[Recommendation]] = []

    chosen = _diversify(seeds, max_seeds)
    # A mix seeded from a track is full of that track's neighbours, including
    # other uploads of the track itself. Excluding every seed up front stops
    # the radio recommending you what you just listened to.
    for seed in chosen:
        exclude.add(_key(seed.title or Path(seed.path).stem))

    for seed in chosen:
        video = youtube_id(seed.path)
        if video is None:
            query = seed.label.replace("—", "-")
            try:
                video = resolve(query)
            except YtDlpError:
                video = None
        if video is None:
            continue

        try:
            rows = mix(video, limit=per_seed)
        except YtDlpError:
            continue

        lane: list[Recommendation] = []
        for result in rows:
            key = _key(result.title)
            if not key or key in exclude or key in seen:
                continue
            seen.add(key)
            lane.append(
                Recommendation(
                    result=result,
                    reason=f"because you played {seed.label}",
                    lane="mix",
                )
            )
        if lane:
            lanes.append(lane)

    if artist_lane:
        artist_lane_results = _artist_lane(chosen, exclude, seen)
        if artist_lane_results:
            lanes.append(artist_lane_results)

    # Round-robin interleave.
    merged: list[Recommendation] = []
    depth = max((len(lane) for lane in lanes), default=0)
    for index in range(depth):
        for lane in lanes:
            if index < len(lane):
                merged.append(lane[index])
                if len(merged) >= limit:
                    return merged
    return merged


def _artist_lane(seeds, exclude: set[str], seen: set[str]) -> list[Recommendation]:
    """One track from each of a few adjacent artists, to widen the net."""
    artists = [seed.artist for seed in seeds if seed.artist]
    if not artists:
        return []

    out: list[Recommendation] = []
    for source_artist in artists[:2]:
        for neighbour in related_artists(source_artist, limit=5):
            try:
                rows = _search_one(neighbour)
            except YtDlpError:
                continue
            for result in rows:
                key = _key(result.title)
                if not key or key in exclude or key in seen:
                    continue
                seen.add(key)
                out.append(
                    Recommendation(
                        result=result,
                        reason=f"{neighbour}, similar to {source_artist}",
                        lane="artist",
                    )
                )
                break
    return out


def _search_one(artist: str) -> list[SearchResult]:
    out = _run(
        [
            f"ytsearch1:{artist} slowed reverb",
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
        ],
        timeout=60,
    )
    results = []
    for entry in _entries(out):
        if not entry.get("id"):
            continue
        duration = entry.get("duration")
        results.append(
            SearchResult(
                video_id=entry["id"],
                title=entry.get("title") or "Untitled",
                uploader=entry.get("uploader") or entry.get("channel") or "",
                duration=int(duration) if duration else None,
            )
        )
    return results


# --- Qt worker --------------------------------------------------------------


class _RadioSignals(QObject):
    finished = Signal(list)  # list[Recommendation]
    error = Signal(str)


class RadioTask(QRunnable):
    """Build a radio queue off the UI thread.

    Every network call in here is blocking and some take tens of seconds, so
    none of it may touch the UI thread. Matches the SearchTask/DownloadTask
    pattern in downloader.py.
    """

    def __init__(
        self,
        seeds,
        exclude_keys: set[str] | None = None,
        limit: int = 40,
        artist_lane: bool = True,
    ) -> None:
        super().__init__()
        self.seeds = list(seeds)
        self.exclude_keys = set(exclude_keys or ())
        self.limit = limit
        self.artist_lane = artist_lane
        self.signals = _RadioSignals()

    def run(self) -> None:
        try:
            picks = build(
                self.seeds,
                exclude_keys=self.exclude_keys,
                limit=self.limit,
                artist_lane=self.artist_lane,
            )
        except YtDlpError as exc:
            self.signals.error.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a worker must never take the app down
            self.signals.error.emit(f"Radio failed: {exc}")
            return
        if not picks:
            self.signals.error.emit(
                "No recommendations yet — play a few tracks through and try again."
            )
            return
        self.signals.finished.emit(picks)
