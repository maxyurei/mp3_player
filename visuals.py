"""Which looping visual a track gets — dealt from one flat pool.

Picking a GIF per song by hand is a configuration treadmill that dies at about
twenty songs, so a visual is not a property of a track: it is dealt from the
pool, stored, and left alone until you ask for a reshuffle.

There is one pool — every visual under `assets/visuals/`, subfolders included.
Subfolders used to act as per-playlist "moods", which was more machinery than
the idea earned; they are now just places files can sit, and everything in them
joins the same rotation.

## The rule that matters: deal against what is already assigned

The obvious way to hand out visuals is `crc32(name) % len(pool)`. It needs no
state, and it is wrong — it is balls-into-bins, so with 12 tracks and 8 visuals
about 1.6 visuals are expected never to come up while one takes a third of the
playlist.

The subtler failure, and the one that actually bit: dealing round-robin from a
fixed starting offset *without looking at existing assignments*. The first deal
looks perfect. Every deal after it restarts at the same offset, so every track
added later lands on the same visual — measured on this library at 109 tracks
over 26 visuals, one visual had 11 tracks and another 8 where an even spread is
4 or 5. Add tracks one at a time, as downloads arrive, and every single one gets
the identical GIF. The display looks stuck because it *is* stuck.

So assignment is always least-used-first, counted over everything already
assigned, ties broken randomly. New tracks fill the thinnest visuals, and even
coverage is a property of the algorithm rather than of dealing everything at
once. A reshuffle simply throws the assignments away and deals again.
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

VISUAL_SUFFIXES = (".gif", ".webp")

_README = """\
Drop looping visuals in this folder — .gif or .webp.

Any file here joins the rotation automatically; there is nothing to configure.
Subfolders work too — everything under this folder is one shared pool.

Each track keeps whichever visual it was dealt, so a song looks the same every
time you play it. To deal them out again, right-click the ribbon and choose
"Reshuffle visuals".

Keep them small — around 400px and a couple of MB. Frames are scaled to the
panel once on load, and animation stops whenever playback pauses or the window
is hidden, so an idle player costs nothing.
"""

# Directory listings are cached against the folder's mtime: the pool is read on
# every track change, and re-walking the folder each time is pointless.
_cache: tuple[tuple[float, ...], list[Path]] | None = None


def visuals_root() -> Path:
    return Path(__file__).parent / "assets" / "visuals"


def ensure_root() -> Path:
    """Create the pool folder (with its explainer) if it isn't there yet."""
    root = visuals_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        readme = root / "README.txt"
        if not readme.exists():
            readme.write_text(_README, encoding="utf-8")
    except OSError:
        pass  # read-only install: the pool is simply empty
    return root


def _stamps(root: Path) -> tuple[float, ...]:
    """mtimes of the root and every subfolder, for cache invalidation."""
    stamps = []
    try:
        stamps.append(root.stat().st_mtime)
        for child in sorted(root.iterdir()):
            if child.is_dir():
                stamps.append(child.stat().st_mtime)
    except OSError:
        return ()
    return tuple(stamps)


def pool() -> list[Path]:
    """Every visual under the root, subfolders included, sorted and cached."""
    global _cache
    root = visuals_root()
    if not root.is_dir():
        return []

    stamps = _stamps(root)
    if _cache is not None and _cache[0] == stamps:
        return _cache[1]

    # Deduplicate on (name, size). Subfolders here started life as mood pools
    # and are mostly copies of the root files, so walking the tree naively
    # listed 53 files for 27 actual visuals — and a duplicated visual would then
    # be dealt twice as often as any other, quietly undoing the even spread.
    found: list[Path] = []
    seen: set[tuple[str, int]] = set()
    for path in sorted(root.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in VISUAL_SUFFIXES):
            continue
        try:
            key = (path.name.lower(), path.stat().st_size)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    _cache = (stamps, found)
    return found


def relative_name(path: Path) -> str | None:
    """Store assignments relative to the pool root so the folder stays movable."""
    try:
        return str(path.relative_to(visuals_root()))
    except ValueError:
        return None


def deal(track_names: list[str], candidates: list[Path], used: Counter | None = None):
    """Assign each name a visual, always taking one of the least-used.

    `used` counts visuals already handed out elsewhere, so an incremental deal
    of one new track is as even as dealing the whole library at once — see the
    module docstring for why that is the whole ballgame.
    """
    if not candidates or not track_names:
        return {}

    counts = Counter({relative_name(path) or str(path): 0 for path in candidates})
    if used:
        for name, count in used.items():
            if name in counts:
                counts[name] += count

    by_name = {relative_name(path) or str(path): path for path in candidates}
    assignment: dict[str, Path] = {}
    # Shuffle so that ties — which is every visual on a fresh deal — resolve
    # differently each time. This is what makes "reshuffle" actually reshuffle.
    order = list(track_names)
    random.shuffle(order)
    for name in order:
        fewest = min(counts.values())
        pick = random.choice([key for key, n in counts.items() if n == fewest])
        assignment[name] = by_name[pick]
        counts[pick] += 1
    return assignment


def resolve(track_path: Path) -> Path | None:
    """Last-resort pick for a track with no stored assignment."""
    candidates = pool()
    if not candidates:
        return None
    return random.choice(candidates)
