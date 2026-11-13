#!/usr/bin/env python3
"""
move_playlist.py

Moves the songs of Navidrome playlists into a specific folder per playlist.
Edit PLAYLIST_FOLDERS below to configure which playlist goes where.

Navidrome returns each song's path relative to its library folder
(e.g. "Artist/Album/01 - Song.mp3" or "/music/Organizado/Artist/Album/01 - Song.mp3")
but NOT which of the subfolders of MUSIC_ROOT it lives in (bandcamp, BORRAR, DJ, ...).
The subfolders are discovered automatically from MUSIC_ROOT and each one is tried until the
file is found. If the same relative path exists in several subfolders, the
file size reported by Navidrome is used to pick the right one; if it's still
ambiguous the song is SKIPPED (never guessed).

Only the Navidrome credentials come from .env:
    NAVIDROME_URL, NAVIDROME_URL_2 (optional fallback), NAVIDROME_USER, NAVIDROME_PASSWORD

Usage:
    python move_playlist.py --list                # list Navidrome playlists
    python move_playlist.py --dry-run             # simulate
    python move_playlist.py                       # move for real
    python move_playlist.py -p "_BORRAR"          # only that playlist (repeatable)
    python move_playlist.py --scan                # trigger a Navidrome scan afterwards
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
import string
import unicodedata
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ═════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  (edit this block)
# ═════════════════════════════════════════════════════════════════════════════

# Folder that contains all your main subfolders (bandcamp, BORRAR, DJ, SINFI, etc.).
# Reads MUSIC_BASE_ROOT or MUSIC_PATH, falling back to /home/escriva/music/baldman
MUSIC_ROOT = os.getenv("ROOT_PATH") or os.getenv("MUSIC_PATH", "/home/escriva/music/baldman")

# "Playlist name in Navidrome": "Destination folder"
#   - A relative folder is taken relative to MUSIC_ROOT ("BORRAR" -> MUSIC_ROOT/BORRAR)
#   - An absolute path is used as is
# Playlist names are matched ignoring case and accents.
PLAYLIST_FOLDERS = {
    "_BORRAR":  "BORRAR",
    "_CIBELES": "CIBELES",
    "_BRAKES":  "BRAKES",
}

# Carpetas principales conocidas dentro de MUSIC_ROOT
MAIN_FOLDERS = [
    "bandcamp",
    "BORRAR",
    "BRAKES",
    "CIBELES",
    "DESCARTES",
    "DJ",
    "ingresos",
    "SINFI"
]

# True  -> keep the Artist/Album/... structure inside the destination
# False -> put every file directly in the destination folder
KEEP_STRUCTURE = True

# ═════════════════════════════════════════════════════════════════════════════

SERVER_URL_PRIMARY  = os.getenv("NAVIDROME_URL")
SERVER_URL_FALLBACK = os.getenv("NAVIDROME_URL_2")
USERNAME            = os.getenv("NAVIDROME_USER")
PASSWORD            = os.getenv("NAVIDROME_PASSWORD")
CLIENT              = "move-playlist-script"

if not all([SERVER_URL_PRIMARY, USERNAME, PASSWORD, MUSIC_ROOT]):
    raise SystemExit(
        "Missing required .env variables: NAVIDROME_URL, NAVIDROME_USER, "
        "NAVIDROME_PASSWORD, MUSIC_PATH or MUSIC_BASE_ROOT"
    )

SERVER_URL = SERVER_URL_PRIMARY


# ── Subsonic / Navidrome ─────────────────────────────────────────────────────

def make_token(password: str) -> tuple[str, str]:
    salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return hashlib.md5((password + salt).encode()).hexdigest(), salt


def base_params() -> dict:
    token, salt = make_token(PASSWORD)
    return {"u": USERNAME, "t": token, "s": salt, "v": "1.16.1", "c": CLIENT, "f": "json"}


def _get(server: str, endpoint: str, params: dict) -> requests.Response:
    r = requests.get(f"{server}/rest/{endpoint}", params=params, timeout=15)
    r.raise_for_status()
    return r


def api_get(endpoint: str, params: dict | None = None) -> dict:
    """Tries the primary server, falls back to the secondary one if unreachable."""
    global SERVER_URL
    all_params = {**base_params(), **(params or {})}
    try:
        r = _get(SERVER_URL, endpoint, all_params)
    except (requests.ConnectionError, requests.Timeout):
        if not SERVER_URL_FALLBACK or SERVER_URL == SERVER_URL_FALLBACK:
            raise
        print(f"  [WARN] {SERVER_URL} unreachable, trying {SERVER_URL_FALLBACK}...")
        SERVER_URL = SERVER_URL_FALLBACK
        r = _get(SERVER_URL, endpoint, all_params)

    resp = r.json().get("subsonic-response", {})
    if resp.get("status") != "ok":
        raise RuntimeError(f"Navidrome API error: {resp.get('error', resp)}")
    return resp


def get_playlists() -> list[dict]:
    pls = api_get("getPlaylists").get("playlists", {}).get("playlist", [])
    return [pls] if isinstance(pls, dict) else pls


def get_playlist_songs(playlist_id: str) -> list[dict]:
    entries = api_get("getPlaylist", {"id": playlist_id}).get("playlist", {}).get("entry", [])
    return [entries] if isinstance(entries, dict) else entries


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return text.lower().strip()


def get_roots(music_root: Path) -> list[Path]:
    """Descubre todas las subcarpetas principales conocidas y cualquier subdirectorio
    existente bajo MUSIC_ROOT para resolver las rutas relativas de Navidrome."""
    roots = []
    # 1. Carpetas principales declaradas y sus subdirectorios "Organizado"
    for folder in MAIN_FOLDERS:
        p = music_root / folder
        if p.is_dir() and p not in roots:
            roots.append(p)
            org = p / "Organizado"
            if org.is_dir() and org not in roots:
                roots.append(org)

    # 2. Cualquier otra subcarpeta directa de MUSIC_ROOT
    for p in sorted(music_root.iterdir()):
        if p.is_dir() and p not in roots:
            roots.append(p)
            org = p / "Organizado"
            if org.is_dir() and org not in roots:
                roots.append(org)

    # 3. La propia raíz al final
    roots.append(music_root)
    return roots


def resolve_dest(folder: str, music_root: Path) -> Path:
    p = Path(os.path.expanduser(folder))
    return (p if p.is_absolute() else music_root / p).resolve()


def clean_rel_path(rel_path: str) -> list[str]:
    """Limpia prefijos montados por el contenedor (ej: /music/, /music/Organizado/)
    y devuelve varias alternativas de ruta relativa para probar en disco."""
    p = rel_path.replace("\\", "/").strip("/")
    
    candidates = [p]
    
    # Si Navidrome devuelve prefijos virtuales como music/Organizado/... o music/...
    prefixes = ["music/Organizado/", "music/", "Organizado/"]
    for prefix in prefixes:
        if p.lower().startswith(prefix.lower()):
            candidates.append(p[len(prefix):])

    # Eliminar duplicados manteniendo el orden
    seen = set()
    return [x for x in candidates if not (x in seen or seen.add(x))]


def find_song_file(rel_path: str, size: int | None, roots: list[Path]) -> tuple[Path | None, str]:
    """Returns (file, status) where status is "ok" | "missing" | "ambiguous"."""
    rel_variants = clean_rel_path(rel_path)
    found: list[Path] = []

    for rel in rel_variants:
        rel_p = Path(rel)
        for root in roots:
            candidate = root / rel_p
            if candidate.is_file():
                resolved = candidate.resolve()
                if resolved not in found:
                    found.append(resolved)

    if not found:
        return None, "missing"
    if len(found) == 1:
        return found[0], "ok"

    # Same relative path in several subfolders -> disambiguate by file size
    if size:
        by_size = [p for p in found if p.stat().st_size == size]
        if len(by_size) == 1:
            return by_size[0], "ok"
    return None, "ambiguous"


def unique_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    n = 1
    while True:
        candidate = folder / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def is_inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Moves the songs of Navidrome playlists into the folders "
                    "configured in PLAYLIST_FOLDERS."
    )
    p.add_argument("-p", "--playlist", action="append", default=[], metavar="NAME",
                   help="Only process this playlist from PLAYLIST_FOLDERS (repeatable).")
    p.add_argument("--dry-run", action="store_true",
                   help="Only show what would be moved, don't touch anything.")
    p.add_argument("--scan", action="store_true",
                   help="Trigger a Navidrome library scan when finished.")
    p.add_argument("--list", action="store_true",
                   help="List the Navidrome playlists and exit.")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print("Connecting to Navidrome...\n")
    playlists = get_playlists()

    if args.list:
        for pl in playlists:
            print(f"  • {pl.get('name')}  ({pl.get('songCount', '?')} songs)")
        return

    music_root = Path(os.path.expanduser(MUSIC_ROOT)).resolve()
    if not music_root.is_dir():
        raise SystemExit(f"MUSIC_ROOT does not exist: {music_root}")
    roots = get_roots(music_root)

    # Which (playlist -> folder) pairs to process
    pairs = PLAYLIST_FOLDERS
    if args.playlist:
        wanted = {_normalize(n) for n in args.playlist}
        pairs = {k: v for k, v in PLAYLIST_FOLDERS.items() if _normalize(k) in wanted}
        unknown = wanted - {_normalize(k) for k in PLAYLIST_FOLDERS}
        if unknown:
            print(f"  ⚠ Not in PLAYLIST_FOLDERS: {', '.join(sorted(unknown))}")
    if not pairs:
        raise SystemExit("Nothing to do: PLAYLIST_FOLDERS is empty (or no -p matched).")

    by_name = {_normalize(pl.get("name", "")): pl for pl in playlists}

    print(f"Music root : {music_root}")
    print(f"Subfolders : {', '.join(r.name for r in roots if r != music_root)}")
    if args.dry_run:
        print("Mode       : DRY-RUN (nothing will be moved)")
    print()

    total_moved = total_already = total_missing = total_amb = total_err = 0
    assigned: dict[str, Path] = {}   # song id -> destination it was assigned to

    for pl_name, folder in pairs.items():
        pl = by_name.get(_normalize(pl_name))
        if pl is None:
            print(f"✗ Playlist not found in Navidrome: {pl_name}\n")
            continue

        dest = resolve_dest(folder, music_root)
        songs = get_playlist_songs(pl["id"])
        print(f"━━ {pl['name']}  →  {dest}  ({len(songs)} songs)")

        if not args.dry_run:
            dest.mkdir(parents=True, exist_ok=True)

        moved = already = missing = ambiguous = errors = 0
        for song in songs:
            sid = song["id"]
            rel_path = song.get("path", "")
            label = f"{song.get('artist', '?')} - {song.get('title', rel_path)}"

            # Song in two playlists with different destinations: first one wins
            if sid in assigned:
                if assigned[sid] != dest:
                    print(f"  ⚠ Also in another playlist -> {assigned[sid].name}, skipped: {label}")
                continue
            assigned[sid] = dest

            src, status = find_song_file(rel_path, song.get("size"), roots)
            if status == "missing":
                missing += 1
                print(f"  ? Not found on disk: {rel_path}")
                continue
            if status == "ambiguous":
                ambiguous += 1
                print(f"  ⚠ Ambiguous (exists in several folders), skipped: {rel_path}")
                continue

            if is_inside(src, dest):   # already where it should be
                already += 1
                continue

            rel_variants = clean_rel_path(rel_path)
            rel_clean_str = rel_variants[-1]
            
            # Evitar anidar duplicando la carpeta de destino si ya viene en la ruta relativa
            parts = Path(rel_clean_str).parts
            if parts and _normalize(parts[0]) == _normalize(folder):
                rel_clean = Path(*parts[1:])
            else:
                rel_clean = Path(rel_clean_str)

            target_dir = dest / rel_clean.parent if KEEP_STRUCTURE else dest
            target = unique_path(target_dir, src.name)

            try:
                if args.dry_run:
                    print(f"  [dry-run] {src}  ->  {target}")
                else:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(target))
                    print(f"  ✓ {label}  ->  {target}")
                moved += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"  ✗ Error moving {src}: {e}")

        verb = "would be moved" if args.dry_run else "moved"
        print(f"  → {moved} {verb}, {already} already in place, "
              f"{missing} not found, {ambiguous} ambiguous, {errors} errors\n")

        total_moved += moved
        total_already += already
        total_missing += missing
        total_amb += ambiguous
        total_err += errors

    verb = "would be moved" if args.dry_run else "moved"
    print(f"✅ Total: {total_moved} {verb}, {total_already} already in place, "
          f"{total_missing} not found, {total_amb} ambiguous, {total_err} errors.")

    if args.scan and not args.dry_run and total_moved:
        api_get("startScan")
        print("🔄 Navidrome scan started.")


if __name__ == "__main__":
    main()