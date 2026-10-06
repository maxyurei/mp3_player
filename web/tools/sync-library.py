#!/usr/bin/env python3
"""Publish a music folder to the R2 bucket the web player reads.

    python3 web/tools/sync-library.py ~/Music --bucket music
    python3 web/tools/sync-library.py ~/Music --bucket music --dry-run

What it does, in order:
  1. walks the folder for audio files
  2. reads real ID3/MP4/FLAC tags with mutagen (title, artist, album, length)
  3. pulls embedded cover art out, de-duplicated so an album's tracks share one
  4. writes tracks.json, the index the player fetches on launch
  5. uploads audio, covers and the manifest with rclone

The browser cannot do steps 1-3 itself: it can neither list a bucket nor read
tags without downloading whole files. So the index is built here, once, and
shipped next to the audio.

Needs mutagen (already a dependency of the desktop app) and rclone for the
upload. --dry-run and --no-upload both stop before rclone runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".mp4", ".flac", ".ogg", ".oga",
                  ".opus", ".wav", ".webm"}

# Prefixes inside the bucket. The player builds every URL as
# <libraryBase>/<key>, so these are part of the published contract.
AUDIO_PREFIX = "audio"
COVER_PREFIX = "covers"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a music folder to R2 for the web player."
    )
    parser.add_argument("folder", type=Path, help="folder of music to publish")
    parser.add_argument(
        "--bucket", help="R2 bucket name (required unless --no-upload/--dry-run)"
    )
    parser.add_argument(
        "--remote",
        default="r2",
        help="rclone remote name for the bucket (default: r2)",
    )
    parser.add_argument(
        "--staging",
        type=Path,
        default=Path(__file__).resolve().parents[2] / ".library-build",
        help="where tracks.json and covers are assembled before upload",
    )
    parser.add_argument("--no-upload", action="store_true",
                        help="build the manifest but do not run rclone")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete bucket objects no longer in the folder (asks first)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt that --prune would otherwise ask",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen, change nothing")
    return parser.parse_args()


def load_tags(path: Path) -> tuple[dict, bytes | None]:
    """Return (tag fields, cover bytes). Degrades to {} on anything unreadable."""
    try:
        import mutagen
    except ImportError:
        sys.exit(
            "mutagen is not installed.\n"
            "  pip install mutagen       (or use the desktop app's .venv)"
        )

    try:
        audio = mutagen.File(path)
    except Exception:
        return {}, None
    if audio is None:
        return {}, None

    fields: dict = {}
    length = getattr(getattr(audio, "info", None), "length", None)
    if length:
        fields["duration"] = round(float(length), 1)

    def first(*keys):
        for key in keys:
            value = audio.get(key)
            if value:
                text = str(value[0] if isinstance(value, list) else value).strip()
                if text:
                    return text
        return None

    # Tag names differ per container: ID3 frames, MP4 atoms, Vorbis comments.
    fields["title"] = first("TIT2", "\xa9nam", "title")
    fields["artist"] = first("TPE1", "\xa9ART", "artist")
    fields["album"] = first("TALB", "\xa9alb", "album")

    return {k: v for k, v in fields.items() if v}, extract_cover(audio)


def extract_cover(audio) -> bytes | None:
    # ID3
    try:
        frames = audio.tags.getall("APIC") if hasattr(audio.tags, "getall") else []
        if frames:
            return frames[0].data
    except Exception:
        pass
    # MP4 / M4A
    try:
        covers = audio.get("covr")
        if covers:
            return bytes(covers[0])
    except Exception:
        pass
    # FLAC / Vorbis
    try:
        pictures = getattr(audio, "pictures", None)
        if pictures:
            return pictures[0].data
    except Exception:
        pass
    return None


def guess_from_name(stem: str) -> tuple[str, str]:
    """Mirror of the player's filename parsing, for files with no tags."""
    import re

    name = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", stem)
    if " " not in name:
        name = name.replace("_", " ")
    name = re.sub(r"^\s*\d{1,3}\s*[-._)]\s*", "", name).strip()
    if " - " in name:
        artist, _, title = name.partition(" - ")
        if artist.strip() and title.strip():
            return title.strip(), artist.strip()
    return name or stem, "Unknown artist"


def build(folder: Path, staging: Path, dry_run: bool) -> list[dict]:
    files = sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
    )
    # An empty folder is not an error: it is what removing your last song looks
    # like. It is also what a mistyped path looks like, which is why main()
    # asks before publishing one rather than this function refusing outright.

    covers_dir = staging / COVER_PREFIX
    if not dry_run:
        if staging.exists():
            shutil.rmtree(staging)
        covers_dir.mkdir(parents=True, exist_ok=True)

    seen_covers: dict[str, str] = {}
    tracks: list[dict] = []

    for path in files:
        relative = path.relative_to(folder).as_posix()
        tags, cover = load_tags(path)

        title = tags.get("title")
        artist = tags.get("artist")
        if not title or not artist:
            guessed_title, guessed_artist = guess_from_name(path.stem)
            title = title or guessed_title
            artist = artist or guessed_artist

        entry = {
            # Stable across runs because it is derived from the key, so the
            # player can remember things per-track in a later step.
            "id": hashlib.sha1(relative.encode("utf-8")).hexdigest()[:12],
            "key": f"{AUDIO_PREFIX}/{relative}",
            "title": title,
            "artist": artist,
        }
        if tags.get("album"):
            entry["album"] = tags["album"]
        if tags.get("duration"):
            entry["duration"] = tags["duration"]

        if cover:
            digest = hashlib.sha1(cover).hexdigest()[:12]
            if digest not in seen_covers:
                seen_covers[digest] = f"{COVER_PREFIX}/{digest}.jpg"
                if not dry_run:
                    (covers_dir / f"{digest}.jpg").write_bytes(cover)
            entry["cover"] = seen_covers[digest]

        tracks.append(entry)

    manifest = {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tracks": tracks,
    }
    if not dry_run:
        (staging / "tracks.json").write_text(
            json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8"
        )

    print(f"{len(tracks)} tracks, {len(seen_covers)} distinct covers")
    tagged = sum(1 for t in tracks if t["artist"] != "Unknown artist")
    print(f"{tagged} with a real artist tag, {len(tracks) - tagged} guessed from filename")
    return tracks


def upload(folder: Path, staging: Path, remote: str, bucket: str, dry_run: bool) -> None:
    if shutil.which("rclone") is None:
        sys.exit(
            "rclone is not installed — the manifest was built but nothing was "
            "uploaded.\n  sudo pacman -S rclone      then: rclone config"
        )

    includes: list[str] = []
    for suffix in sorted(AUDIO_SUFFIXES):
        includes += ["--include", f"*{suffix}"]

    commands = [
        # Audio mirrors the local folder layout under audio/.
        ["rclone", "copy", str(folder), f"{remote}:{bucket}/{AUDIO_PREFIX}",
         *includes, "--transfers", "8", "--progress"],
        # Covers and the manifest. The manifest goes last so it never advertises
        # a track whose audio has not finished uploading.
        ["rclone", "copy", str(staging), f"{remote}:{bucket}",
         "--transfers", "8", "--progress"],
    ]

    for command in commands:
        print("\n$ " + " ".join(command))
        if dry_run:
            continue
        result = subprocess.run(command)
        if result.returncode != 0:
            sys.exit(f"rclone failed with exit code {result.returncode}")


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        # Not attached to a terminal (a cron job, a pipe). Silence is "no".
        return False


def remote_keys(remote: str, bucket: str, prefix: str) -> set[str]:
    """Paths under one bucket prefix, relative to it."""
    result = subprocess.run(
        ["rclone", "lsf", f"{remote}:{bucket}/{prefix}", "-R", "--files-only"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # A prefix that does not exist yet lists as an error. Treating a failed
        # listing as "empty" is the safe direction: it prunes nothing, where
        # treating it as authoritative would propose deleting everything.
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def prune(tracks: list[dict], remote: str, bucket: str, dry_run: bool,
          assume_yes: bool) -> None:
    """Delete bucket objects the folder no longer contains.

    Opt-in, because the bucket is a backup of sorts and a sync that silently
    deletes is a sync that eventually deletes something irreplaceable.
    """
    wanted = {
        AUDIO_PREFIX: {t["key"].split("/", 1)[1] for t in tracks},
        COVER_PREFIX: {
            t["cover"].split("/", 1)[1] for t in tracks if t.get("cover")
        },
    }

    for prefix, expected in wanted.items():
        orphans = sorted(remote_keys(remote, bucket, prefix) - expected)
        if not orphans:
            print(f"\n{prefix}/: nothing to prune")
            continue

        print(f"\n{prefix}/: {len(orphans)} object(s) no longer in the folder")
        for name in orphans[:20]:
            print(f"  - {name}")
        if len(orphans) > 20:
            print(f"  ... and {len(orphans) - 20} more")

        if dry_run:
            print("  (--dry-run: nothing deleted)")
            continue
        if not confirm(f"  Delete these {len(orphans)} from the bucket?", assume_yes):
            print("  skipped")
            continue

        # --files-from so one rclone call handles the lot and the names never
        # go through a shell, where quoting would break on the first filename
        # containing a space or a quote.
        listing = Path(f"/tmp/sync-library-prune-{prefix}.txt")
        listing.write_text("\n".join(orphans) + "\n", encoding="utf-8")
        try:
            result = subprocess.run(
                ["rclone", "delete", f"{remote}:{bucket}/{prefix}",
                 "--files-from", str(listing)]
            )
            if result.returncode != 0:
                sys.exit(f"rclone delete failed with exit code {result.returncode}")
            print(f"  deleted {len(orphans)}")
        finally:
            listing.unlink(missing_ok=True)


def main() -> None:
    args = parse_args()
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")

    staging = args.staging.expanduser().resolve()
    print(f"Scanning {folder}")
    tracks = build(folder, staging, args.dry_run)

    if not tracks:
        # Reached by removing every song, and equally by pointing at the wrong
        # folder. Publishing empties the library in the player, so say exactly
        # that and let the answer decide.
        print(f"\nNo audio files under {folder}.")
        print("Publishing this would leave the player with an empty library.")
        print("(The audio already in the bucket stays unless you pass --prune.)")
        if not confirm("Publish an empty library?", args.yes):
            print("Nothing changed.")
            return

    if args.no_upload:
        print(f"\nBuilt {staging}/tracks.json — not uploading (--no-upload).")
        return
    if not args.bucket:
        print(f"\nBuilt {staging}/tracks.json — pass --bucket NAME to upload.")
        return

    upload(folder, staging, args.remote, args.bucket, args.dry_run)
    if args.prune:
        # After the upload, so a failed upload cannot leave the bucket pruned
        # against a manifest that never shipped.
        prune(tracks, args.remote, args.bucket, args.dry_run, args.yes)
    if not args.dry_run:
        print("\nDone. Reopen the player to pick up the new library.")


if __name__ == "__main__":
    main()
