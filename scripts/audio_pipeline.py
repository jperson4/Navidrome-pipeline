#!/usr/bin/env python3
"""
Full audio pipeline: (Clean) -> Convert -> Organize -> Rename

Phase 0  (OPTIONAL, --delete-non-audio)
         PERMANENTLY deletes any file in the source that is not audio
         (images, .nfo, .cue, .txt, .url, etc.). Respects excluded
         folders (-e) and never touches the destination.

Phase 1  Converts FLAC / WAV / APE / OGG / etc. to MP3 320 kbps
         while preserving metadata. (ffmpeg, ThreadPoolExecutor)

Phase 2  Reads metadata with ffprobe ONCE per file, organizes into
         Artist/Album and renames in the same move. Files in excluded
         folders (-e) are skipped in this phase. Worse duplicates are
         moved to the "Duplicates" folder inside the destination,
         instead of being deleted. That folder is automatically
         excluded from future scans.
         (ffprobe, ThreadPoolExecutor)

Phase 3  Removes empty folders from source and destination, and also
         folders that contain NO audio file at all, even if they
         aren't empty (e.g. album folders that only have leftover
         .jpg covers, .m3u playlists, .nfo, .cue, etc.).
         (the Duplicates folder is protected and never deleted).

Note:    In Phase 1, the "genre" tag is stripped during conversion
         (via ffmpeg). In Phase 2 it is also stripped from MP3s that
         already came in that format (didn't go through Phase 1),
         using mutagen, before moving them.

Requires: Python 3.8+, ffmpeg and ffprobe available on PATH, mutagen
          (pip install mutagen).

Examples:
    python audio_pipeline.py
    python audio_pipeline.py --source ~/Music --dest ~/Music/Organized
    python audio_pipeline.py --threads 4
    python audio_pipeline.py -e ./DJs/PacoSanz ./No_Tags
    python audio_pipeline.py --source ~/Music --dest ~/Organized -e ~/Music/Podcasts ~/Music/Sets
    python audio_pipeline.py --delete-non-audio        # also deletes everything that isn't audio
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

load_dotenv()

try:
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT2
    MUTAGEN_AVAILABLE = True
except ImportError:  # noqa: BLE001
    MUTAGEN_AVAILABLE = False

# DEFAULT PATHS:
DEFAULT_SOURCE_PATH = os.getenv("DOWNLOADS_PATH", ".")       # --source
DEFAULT_DEST_PATH = os.getenv("MUSIC_PATH", "./Organizado")  # --dest


# ─────────────────────────────────────────────────────────────────────────────
#  COLORS (ANSI)
# ─────────────────────────────────────────────────────────────────────────────

class C:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    DYELLOW = "\033[33m"
    RED = "\033[91m"
    GRAY = "\033[90m"
    WHITE = "\033[97m"
    RESET = "\033[0m"


def banner(text: str) -> None:
    sep = "─" * 54
    print()
    print(f"{C.CYAN}{sep}{C.RESET}")
    print(f"{C.CYAN}  {text}{C.RESET}")
    print(f"{C.CYAN}{sep}{C.RESET}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
#  DUPLICATES FOLDER NAME
# ─────────────────────────────────────────────────────────────────────────────

DUPLICATES_DIRNAME = "Duplicados"

EXCLUDED: List[str] = []  # filled in main() with --exclude and Duplicates
EXCLUDED.append(f"./{DUPLICATES_DIRNAME}")  # Duplicates is always excluded
if os.getenv("EXCLUDED_PATH"):
    EXCLUDED.append(os.getenv("EXCLUDED_PATH"))
CONVERTIBLE_EXTENSIONS = {".flac", ".wav", ".ape", ".aiff", ".aif", ".wv",
                          ".ogg", ".m4a", ".aac", ".wma", ".opus"}

# Everything considered "audio" for Phase 0 purposes (includes .mp3,
# which is the final format, plus other common audio containers you
# may not want to convert but also don't want to delete).
AUDIO_EXTENSIONS = CONVERTIBLE_EXTENSIONS | {
    ".mp3", ".alac", ".dsf", ".dff", ".mka", ".oga", ".webm",
}


# ─────────────────────────────────────────────────────────────────────────────
#  RESULT OF EACH WORKER
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    status: str  # "OK" | "SKIP" | "EXISTS" | "DUPLICATE" | "ERROR"
    file: str
    msg: str = ""


def invoke_pool(worker, items: Sequence, max_threads: int, activity: str,
                 shared_data=None) -> List[Result]:
    """Runs the workers in a ThreadPoolExecutor (equivalent to a RunspacePool)."""
    total = len(items)
    if total == 0:
        return []

    results: List[Result] = []
    done = 0

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = {executor.submit(worker, item, shared_data): item for item in items}

        for future in as_completed(futures):
            done += 1
            item = futures[future]
            try:
                r = future.result()
            except Exception as e:  # noqa: BLE001
                name = getattr(item, "name", str(item))
                r = Result("ERROR", name, str(e))

            if r:
                results.append(r)
                if r.status == "OK":
                    print(f"  {C.GREEN}[OK]     {r.msg}{C.RESET}")
                elif r.status == "SKIP":
                    print(f"  {C.GRAY}[SKIP]   {r.file}{C.RESET}")
                elif r.status == "EXISTS":
                    print(f"  {C.YELLOW}[EXISTS] {r.file}{C.RESET}")
                elif r.status == "DUPLICATE":
                    print(f"  {C.DYELLOW}[DUPLICATE] {r.msg}{C.RESET}")
                elif r.status == "ERROR":
                    print(f"  {C.RED}[ERROR]  {r.file}  ->  {r.msg}{C.RESET}")

            pct = int((done / total) * 100)
            print(f"\r  Progress ({activity}): {done}/{total} ({pct}%)   ", end="", flush=True)

    print()
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  SANITIZE NAMES
# ─────────────────────────────────────────────────────────────────────────────

# Characters invalid in Windows filenames (replicated even when running
# on Linux, to keep compatibility if the library is copied over to a
# Windows system / Rekordbox).
_INVALID_CHARS = '<>:"/\\|?*'


def sanitize(s: str) -> str:
    for ch in _INVALID_CHARS:
        s = s.replace(ch, "_")
    s = re.sub(r"[\x00-\x1f]", "_", s)   # control characters
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ─────────────────────────────────────────────────────────────────────────────
#  ARTIST NAME MATCHING (case/accent-insensitive)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_name(s: str) -> str:
    """Normalizes a name for comparison purposes only: lowercase and with
    diacritics stripped (á -> a, ë -> e, Ñ -> n, etc.). Used just to decide
    whether two artist names are "the same", never to rename anything."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.casefold()


def resolve_folder_name(
    raw_name: str,
    cache: Dict[Tuple[str, str], str],
    lock: threading.Lock,
    fallback: str,
    scope: str = "",
) -> str:
    """
    Returns the folder name to actually use on disk (shared by artist and
    album resolution).

    Comparison is case/accent-insensitive (via normalize_name), but the
    folder name that gets used/stored is always the ORIGINAL (non-normalized)
    one. `cache` maps (scope, normalized_name) -> canonical_folder_name and
    is shared across threads (protected by `lock`):

      - Pre-populated in main() from the folders that already exist in
        `dest` (artists directly, albums inside each artist), so an
        incoming "ANNE" matches a pre-existing "annë" and reuses that same
        folder instead of creating a duplicate.
      - The first time a brand-new name is seen (in this run), its
        sanitized form is registered as the canonical one; any later
        variant (different case/accents) resolves to that same folder,
        i.e. "the one that was already there before" wins.

    `scope` lets the same name be reused independently per context — e.g.
    two different artists can each have an album called "Singles" without
    colliding, since albums are scoped by their (already-resolved) artist
    folder name.
    """
    sanitized = sanitize(raw_name) if raw_name else fallback
    key = (scope, normalize_name(sanitized))
    with lock:
        existing = cache.get(key)
        if existing is not None:
            return existing
        cache[key] = sanitized
        return sanitized


# ─────────────────────────────────────────────────────────────────────────────
#  TITLE CLEANUP (strip redundant tags like "(Original Mix)")
# ─────────────────────────────────────────────────────────────────────────────

# Add more phrases here to also strip them (e.g. "radio edit", "club mix").
# Only trailing tags are removed, so real content earlier in the title is
# never touched.
REDUNDANT_TITLE_TAGS = (
    "original mix",
    "original version",
    "original edit",
    "original",
)

_tag_alt = "|".join(
    re.escape(t) for t in sorted(REDUNDANT_TITLE_TAGS, key=len, reverse=True)
)
_TITLE_TAG_RE = re.compile(
    rf"""
    \s*(?:
        [\(\[]\s*(?:{_tag_alt})\s*[\)\]]   # "(Original Mix)" / "[Original Mix]"
        |
        -\s*(?:{_tag_alt})                  # " - Original Mix"
    )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def clean_title(title: str) -> str:
    """Strips redundant trailing tags (see REDUNDANT_TITLE_TAGS) from a
    track title, case-insensitively. Runs repeatedly in case a title has
    more than one such tag stacked at the end (e.g. "Song (Original Mix) -
    Original Version")."""
    cleaned = title
    while True:
        new = _TITLE_TAG_RE.sub("", cleaned).strip()
        if new == cleaned:
            return new or title  # never return an empty title
        cleaned = new



# ─────────────────────────────────────────────────────────────────────────────
#  UNIQUE PATH INSIDE DUPLICATES (avoids overwriting another same-named duplicate)
# ─────────────────────────────────────────────────────────────────────────────

def unique_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suf = candidate.suffix
    counter = 1
    while True:
        candidate = folder / f"{stem} ({counter}){suf}"
        if not candidate.exists():
            return candidate
        counter += 1


# ─────────────────────────────────────────────────────────────────────────────
#  WORKER - PHASE 0: Delete non-audio files
# ─────────────────────────────────────────────────────────────────────────────

def worker_delete_non_audio(file: Path, _shared=None) -> Result:
    try:
        file.unlink()
        return Result("OK", file.name, f"Deleted (non-audio): {file}")
    except Exception as e:  # noqa: BLE001
        return Result("ERROR", file.name, str(e))


# ─────────────────────────────────────────────────────────────────────────────
#  WORKER - PHASE 1: Convert to MP3 320 kbps
# ─────────────────────────────────────────────────────────────────────────────

def worker_convert(file: Path, _shared=None) -> Result:
    mp3 = file.with_suffix(".mp3")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(file),
                "-ab", "320k", "-map_metadata", "0", "-id3v2_version", "3",
                "-metadata", "genre=",
                str(mp3),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if mp3.exists():
            file.unlink()
            return Result("OK", file.name, f"{file.name}  ->  {mp3.name}")
        return Result("ERROR", file.name, "ffmpeg did not produce the output file")
    except Exception as e:  # noqa: BLE001
        return Result("ERROR", file.name, str(e))


# ─────────────────────────────────────────────────────────────────────────────
#  STRIP GENRE TAG (in-place, no re-encoding)
# ─────────────────────────────────────────────────────────────────────────────

def strip_genre(file: Path) -> None:
    """Removes the genre tag (TCON frame) from an MP3, if present.
    Does nothing if mutagen isn't installed or the file has no ID3 tags."""
    if not MUTAGEN_AVAILABLE:
        return
    try:
        tags = ID3(str(file))
        if "TCON" in tags:
            del tags["TCON"]
            tags.save(str(file))
    except ID3NoHeaderError:
        pass
    except Exception:  # noqa: BLE001
        pass


def write_title_tag(file: Path, new_title: str) -> None:
    """Overwrites the TIT2 (title) ID3 frame with `new_title` (in-place,
    no re-encoding). Does nothing if mutagen isn't installed or the file
    has no ID3 tags — in that case only the filename ends up cleaned."""
    if not MUTAGEN_AVAILABLE:
        return
    try:
        tags = ID3(str(file))
        tags.setall("TIT2", [TIT2(encoding=3, text=new_title)])
        tags.save(str(file))
    except ID3NoHeaderError:
        pass
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  WORKER - PHASE 2: Organize (Artist/Album) + Rename
# ─────────────────────────────────────────────────────────────────────────────

def get_bitrate(path: Path) -> int:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_entries", "format=bit_rate", "--", str(path)],
            capture_output=True, text=True,
        ).stdout
        info = json.loads(out)
        return int(info.get("format", {}).get("bit_rate") or 0)
    except Exception:  # noqa: BLE001
        return 0


@dataclass
class OrganizeContext:
    source: str
    dest: str
    excluded: List[str]
    artist_cache: Dict[Tuple[str, str], str]
    album_cache: Dict[Tuple[str, str], str]
    naming_lock: threading.Lock


def worker_organize(file: Path, ctx: OrganizeContext) -> Result:
    source, dest, excluded = ctx.source, ctx.dest, ctx.excluded

    # ── If the file is in an excluded folder (includes Duplicates) -> SKIP
    for ex in excluded:
        ex_norm = ex.rstrip("/") + "/"
        if str(file).startswith(ex_norm):
            return Result("SKIP", file.name, f"Excluded: {file}")

    # ── Strip genre tag before reading/moving ────────────────────────────────
    strip_genre(file)

    # ── Read metadata with ffprobe ───────────────────────────────────────────
    tags = {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_entries", "format_tags", "--", str(file)],
            capture_output=True, text=True,
        ).stdout
        meta = json.loads(out)
        tags_raw = meta.get("format", {}).get("tags", {}) or {}
        tags = {k.lower(): v for k, v in tags_raw.items()}
    except Exception:  # noqa: BLE001
        pass

    # ── Artist / Album folders ───────────────────────────────────────────────
    # album_artist takes priority so that apps like Jellyfin group
    # compilations and remix albums correctly (each track with a
    # different artist).
    if tags.get("album_artist"):
        raw_artist_name = tags["album_artist"].strip()
    elif tags.get("artist"):
        raw_artist_name = tags["artist"].strip()
    else:
        raw_artist_name = "Unknown Artist"

    artist_folder = resolve_folder_name(
        raw_artist_name, ctx.artist_cache, ctx.naming_lock, fallback="Unknown Artist",
    )

    raw_album_name = tags["album"].strip() if tags.get("album") else "Unknown Album"
    album_folder = resolve_folder_name(
        raw_album_name, ctx.album_cache, ctx.naming_lock,
        fallback="Unknown Album", scope=artist_folder,
    )

    # ── File name ─────────────────────────────────────────────────────────────
    track = ""
    if tags.get("track"):
        t = tags["track"].split("/")[0].strip()
        track = f"{int(t):02d}" if t.isdigit() else t

    artist_raw = tags["artist"].strip() if tags.get("artist") else ""
    raw_title = tags["title"].strip() if tags.get("title") else file.stem
    title = clean_title(raw_title)

    # If the file actually had a title tag and cleaning changed it, write
    # the cleaned version back to the ID3 tag too (not just the filename).
    if tags.get("title") and title != raw_title:
        write_title_tag(file, title)

    if track and artist_raw and title:
        new_name = f"{track} - {artist_raw} - {title}"
    elif artist_raw and title:
        new_name = f"{artist_raw} - {title}"
    elif track and title:
        new_name = f"{track} - {title}"
    else:
        new_name = title

    new_name = sanitize(new_name)

    # ── Create folder and move ───────────────────────────────────────────────
    folder = Path(dest) / artist_folder / album_folder
    folder.mkdir(parents=True, exist_ok=True)

    dest_path = folder / f"{new_name}.mp3"

    if dest_path.exists():
        # ── Resolve duplicate: bitrate -> size -> the incoming one goes to Duplicates
        bitrate_incoming = get_bitrate(file)
        bitrate_existing = get_bitrate(dest_path)

        replace = False
        reason = ""

        if bitrate_incoming > bitrate_existing:
            replace = True
            reason = f"bitrate {bitrate_incoming} > {bitrate_existing}"
        elif bitrate_incoming == bitrate_existing:
            size_incoming = file.stat().st_size
            size_existing = dest_path.stat().st_size
            if size_incoming > size_existing:
                replace = True
                reason = f"same bitrate, size {size_incoming} > {size_existing}"

        duplicates_folder = Path(source) / DUPLICATES_DIRNAME
        duplicates_folder.mkdir(parents=True, exist_ok=True)

        if replace:
            # The one already in dest is the worse one -> sent to Duplicates
            try:
                dup_dest = unique_path(duplicates_folder, dest_path.name)
                shutil.move(str(dest_path), str(dup_dest))
                shutil.move(str(file), str(dest_path))
                return Result(
                    "OK", file.name,
                    f"[REPLACED ({reason})] {artist_folder}/{album_folder}/{new_name}.mp3 "
                    f"(old one -> {DUPLICATES_DIRNAME}/{dup_dest.name})",
                )
            except Exception as e:  # noqa: BLE001
                return Result("ERROR", file.name, str(e))

        # The incoming file is the worse one (or equal) -> sent to Duplicates
        try:
            dup_dest = unique_path(duplicates_folder, file.name)
            shutil.move(str(file), str(dup_dest))
            return Result(
                "DUPLICATE", file.name,
                f"{file.name}  ->  {DUPLICATES_DIRNAME}/{dup_dest.name} "
                f"(already existed: {artist_folder}/{album_folder}/{new_name}.mp3)",
            )
        except Exception as e:  # noqa: BLE001
            return Result("ERROR", file.name, str(e))

    try:
        shutil.move(str(file), str(dest_path))
        return Result("OK", file.name, f"{artist_folder}/{album_folder}/{new_name}.mp3")
    except Exception as e:  # noqa: BLE001
        return Result("ERROR", file.name, str(e))


# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 3: Remove empty folders
# ─────────────────────────────────────────────────────────────────────────────

def contains_music(folder: Path) -> bool:
    """
    True if the folder contains at least one audio file, at any
    level (recursive). If it returns False, the folder only has
    album "leftovers": .jpg covers, .m3u playlists, .nfo, .cue,
    .txt, etc. (or is directly empty).
    """
    return any(
        p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        for p in folder.rglob("*")
    )


def remove_empty_folders(root: Path, protected: Sequence[str]) -> Tuple[int, int]:
    """
    Removes:
      1) Empty folders (as before).
      2) Folders that, without being empty, contain NO audio file
         at all (only jpgs, playlists, .nfo, .cue, etc. that come
         bundled with albums). In this case the whole folder is
         removed along with its contents (shutil.rmtree).

    Processed from deepest folder to shallowest, so that when a
    parent folder is evaluated, its subfolders have already been
    removed (if they had no music) or still exist because they DO
    have music at some level; therefore a parent folder is never
    mistakenly removed if any audio hangs off of it.

    Returns (empty_removed, no_music_removed).
    """
    folders = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(str(p)),
        reverse=True,
    )

    protected_paths = [Path(p) for p in protected]
    empty = 0
    no_music = 0

    for c in folders:
        if not c.exists():
            # Already removed as part of a parent/child folder in this same pass
            continue

        is_protected = any(
            c == p or str(c).startswith(str(p).rstrip("/") + "/")
            for p in protected_paths
        )
        if is_protected:
            continue

        try:
            if not any(c.iterdir()):
                c.rmdir()
                print(f"  {C.GRAY}[REMOVED]            {c}{C.RESET}")
                empty += 1
            elif not contains_music(c):
                shutil.rmtree(c)
                print(f"  {C.GRAY}[REMOVED] (no music) {c}{C.RESET}")
                no_music += 1
        except Exception as e:  # noqa: BLE001
            print(f"  {C.RED}[ERROR]   {c} -> {e}{C.RESET}")

    return empty, no_music


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audio pipeline: (Clean) -> Convert -> Organize -> Rename",
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE_PATH,
                         help="Root folder to search for files (default: MUSIC_PATH from .env, or '.')")
    parser.add_argument("--dest", default=DEFAULT_DEST_PATH,
                         help="Folder where organized MP3s are placed (default: DOWNLOADS_PATH from .env, or './Organizado')")
    parser.add_argument("--threads", type=int, default=max(2, os.cpu_count() or 2),
                         help="Maximum number of simultaneous threads")
    parser.add_argument("-e", "--exclude", nargs="*", default=[],
                         metavar="FOLDER",
                         help="Folders whose files will NOT be organized or renamed")
    parser.add_argument("--delete-non-audio", action="store_true",
                         help="WARNING: PERMANENTLY deletes, before anything else, "
                              "any file in the source that isn't audio "
                              "(images, .nfo, .cue, .txt, .url, etc.)")
    parser.add_argument("--dry-run", action="store_true",
                         help="With --delete-non-audio: only lists what would be deleted, "
                              "without actually deleting anything")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    source_abs = Path(os.path.expanduser(args.source)).resolve()
    dest_abs = Path(os.path.expanduser(args.dest)).resolve()

    global EXCLUDED
    exclude_abs = EXCLUDED
    for ex in args.exclude:
        expanded = Path(os.path.expanduser(ex))
        if expanded.exists():
            exclude_abs.append(str(expanded.resolve()))
        else:
            print(f"  {C.YELLOW}[WARNING] Excluded folder not found: {ex}{C.RESET}")

    # ── The Duplicates folder (inside source) is always excluded ───────────
    # It doesn't need to exist yet: it's created on the first duplicate.
    duplicates_folder_abs = str(source_abs / DUPLICATES_DIRNAME)
    if duplicates_folder_abs not in exclude_abs:
        exclude_abs.append(duplicates_folder_abs)

    banner("Audio Pipeline  ·  (Clean) > Convert > Organize > Rename")
    print(f"  {C.WHITE}Source     : {source_abs}{C.RESET}")
    print(f"  {C.WHITE}Dest       : {dest_abs}{C.RESET}")
    print(f"  {C.WHITE}Duplicates : {duplicates_folder_abs}{C.RESET}")
    print(f"  {C.WHITE}Threads    : {args.threads}{C.RESET}")
    if args.delete_non_audio:
        mode = "DRY-RUN (nothing deleted)" if args.dry_run else "REAL DELETION"
        print(f"  {C.RED}Non-audio cleanup: enabled [{mode}]{C.RESET}")
    if exclude_abs:
        print(f"  {C.DYELLOW}Exclude    : {', '.join(exclude_abs)}{C.RESET}")
    if not MUTAGEN_AVAILABLE:
        print(f"  {C.YELLOW}[WARNING] mutagen not installed (pip install mutagen): "
              f"genre won't be stripped and title tags won't be cleaned of "
              f"redundant tags (only the filename will be).{C.RESET}")

    # ── PHASE 0 (optional): DELETE NON-AUDIO FILES ───────────────────────────
    non_audio_deleted = 0
    non_audio_err = 0
    if args.delete_non_audio:
        banner("PHASE 0 / 3  —  Delete files that are NOT audio")

        to_delete = [
            p for p in source_abs.rglob("*")
            if p.is_file()
            and p.suffix.lower() not in AUDIO_EXTENSIONS
            and not str(p).startswith(str(dest_abs))
            and not any(str(p).startswith(ex.rstrip("/") + "/") for ex in exclude_abs)
        ]

        if to_delete:
            print(f"  {C.RED}Non-audio files detected: {len(to_delete)}{C.RESET}\n")

            if args.dry_run:
                for p in to_delete:
                    print(f"  {C.YELLOW}[DRY-RUN] Would delete: {p}{C.RESET}")
                print(f"\n  {len(to_delete)} files would be deleted (dry-run, nothing removed)")
            else:
                delete_res = invoke_pool(
                    worker_delete_non_audio, to_delete, args.threads, "Phase 0",
                )
                non_audio_deleted = sum(1 for r in delete_res if r.status == "OK")
                non_audio_err = sum(1 for r in delete_res if r.status == "ERROR")
                print(f"\n  ✔ {non_audio_deleted} deleted   ✘ {non_audio_err} errors")
        else:
            print(f"  {C.GRAY}No non-audio files to delete.{C.RESET}\n")

    # ── PHASE 1: CONVERSION ──────────────────────────────────────────────────
    banner("PHASE 1 / 3  —  Convert to MP3 320 kbps")

    to_convert = [
        p for p in source_abs.rglob("*")
        if p.is_file()
        and p.suffix.lower() in CONVERTIBLE_EXTENSIONS
        and not str(p).startswith(str(dest_abs))
        and not any(str(p).startswith(ex.rstrip("/") + "/") for ex in exclude_abs)
    ]

    conv_ok = conv_err = 0
    if to_convert:
        print(f"  Files to convert : {len(to_convert)}\n")
        conv_res = invoke_pool(worker_convert, to_convert, args.threads, "Phase 1")
        conv_ok = sum(1 for r in conv_res if r.status == "OK")
        conv_err = sum(1 for r in conv_res if r.status == "ERROR")
        print(f"\n  ✔ {conv_ok} converted   ✘ {conv_err} errors")
    else:
        print(f"  {C.GRAY}Nothing to convert.{C.RESET}\n")

    # ── PHASE 2: ORGANIZE + RENAME ───────────────────────────────────────────
    banner("PHASE 2 / 3  —  Organize by Artist / Album and rename")

    mp3s = [
        p for p in source_abs.rglob("*.mp3")
        if p.is_file()
        and not str(p).startswith(str(dest_abs))
        and not any(str(p).startswith(ex.rstrip("/") + "/") for ex in exclude_abs)
    ]

    # ── Artist/album name cache (case/accent-insensitive matching) ──────────
    # Pre-populated by scanning the artist/album folders that already exist
    # in `dest`, so an incoming name processed with different case/accents
    # (e.g. "ANNE" vs an existing "annë") reuses the folder that was
    # already there instead of creating a new, duplicate one.
    artist_cache: Dict[Tuple[str, str], str] = {}
    album_cache: Dict[Tuple[str, str], str] = {}
    naming_lock = threading.Lock()
    if dest_abs.exists():
        for artist_dir in sorted(dest_abs.iterdir()):
            if not artist_dir.is_dir():
                continue
            artist_cache.setdefault(("", normalize_name(artist_dir.name)), artist_dir.name)
            for album_dir in sorted(artist_dir.iterdir()):
                if album_dir.is_dir():
                    album_cache.setdefault(
                        (artist_dir.name, normalize_name(album_dir.name)), album_dir.name,
                    )

    org_ok = org_ex = org_err = org_skip = org_dup = 0
    if mp3s:
        print(f"  MP3 files to process : {len(mp3s)}\n")
        ctx = OrganizeContext(
            source=str(source_abs), dest=str(dest_abs), excluded=exclude_abs,
            artist_cache=artist_cache, album_cache=album_cache, naming_lock=naming_lock,
        )
        org_res = invoke_pool(worker_organize, mp3s, args.threads, "Phase 2", ctx)
        org_ok = sum(1 for r in org_res if r.status == "OK")
        org_ex = sum(1 for r in org_res if r.status == "EXISTS")
        org_err = sum(1 for r in org_res if r.status == "ERROR")
        org_skip = sum(1 for r in org_res if r.status == "SKIP")
        org_dup = sum(1 for r in org_res if r.status == "DUPLICATE")
        print(
            f"\n  ✔ {org_ok} moved   ⊘ {org_ex} already existed   "
            f"⧉ {org_dup} duplicates   ⏭ {org_skip} excluded   ✘ {org_err} errors"
        )
    else:
        print(f"  {C.GRAY}No MP3 files to organize.{C.RESET}\n")

    # ── PHASE 3: REMOVE EMPTY AND MUSIC-LESS FOLDERS ─────────────────────────
    banner("PHASE 3 / 3  —  Remove empty folders and folders with no music")

    # Duplicates is also protected here, in addition to exclude_abs.
    protected = list(exclude_abs)

    empty_removed = 0
    no_music_removed = 0
    print(f"  Cleaning source : {source_abs}")
    v, sm = remove_empty_folders(source_abs, protected)
    empty_removed += v
    no_music_removed += sm

    # if dest_abs.exists() and dest_abs != source_abs:
    #     print(f"  Cleaning dest : {dest_abs}")
    #     v, sm = remove_empty_folders(dest_abs, protected)
    #     empty_removed += v
    #     no_music_removed += sm

    print()
    print(f"  {empty_removed} empty folders removed")
    print(f"  {no_music_removed} music-less folders removed (jpgs, playlists, .nfo, .cue, etc.)")

    # ── FINAL SUMMARY ─────────────────────────────────────────────────────────
    sep = "─" * 54
    print()
    print(f"{C.CYAN}{sep}{C.RESET}")
    print(f"{C.CYAN}  FINAL SUMMARY{C.RESET}")
    print(f"{C.CYAN}{sep}{C.RESET}")
    if args.delete_non_audio:
        print(f"{C.RED}  Non-audio deleted    : {non_audio_deleted}{C.RESET}")
    print(f"{C.GREEN}  Converted to MP3     : {conv_ok}{C.RESET}")
    print(f"{C.GREEN}  Organized/moved      : {org_ok}{C.RESET}")
    print(f"{C.GRAY}  Skipped (existed)    : {org_ex}{C.RESET}")
    print(f"{C.DYELLOW}  Duplicates moved     : {org_dup}{C.RESET}")
    print(f"{C.DYELLOW}  Excluded (not moved) : {org_skip}{C.RESET}")
    print(f"{C.GRAY}  Empty folders        : {empty_removed}{C.RESET}")
    print(f"{C.GRAY}  Music-less folders   : {no_music_removed}{C.RESET}")
    print(f"{C.RED}  Total errors         : {conv_err + org_err + non_audio_err}{C.RESET}")
    print(f"{C.CYAN}{sep}{C.RESET}")


if __name__ == "__main__":
    main()