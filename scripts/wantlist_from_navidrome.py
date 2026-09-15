#!/usr/bin/env python3
"""
Adds to the Discogs wantlist the albums present in Navidrome playlists.
Reuses the same song-extraction logic as autostar.py (Subsonic API) to
get the unique albums, and uses the Discogs API v2.0 to search for each
album and add ALL of its versions/editions (vinyl, CD, cassette, etc.)
to the wantlist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import string
import time
import unicodedata
import os
from pathlib import Path
from dotenv import load_dotenv


import requests

load_dotenv()

# ── Configuration ───────────────────────────────────────────────────────────
SERVER_URL_PRIMARY  = os.getenv("NAVIDROME_URL")
SERVER_URL_FALLBACK = os.getenv("NAVIDROME_URL_2")
USERNAME            = os.getenv("NAVIDROME_USER")
PASSWORD            = os.getenv("NAVIDROME_PASSWORD")
CLIENT              = "discogs-wantlist-script"

if not all([SERVER_URL_PRIMARY, USERNAME, PASSWORD]):
    raise SystemExit(
        "Missing required .env variables: NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD"
    )

SERVER_URL = SERVER_URL_PRIMARY


# Playlists that are always ignored (case/accent-insensitive comparison).
# Example: PLAYLIST_BLACKLIST = ["Discard", "Listen later"]
# Can also be added on the fly with --exclude-playlist "Name".
PLAYLIST_BLACKLIST: list[str] = []

# ── Discogs configuration ─────────────────────────────────────────────────────────
# Generate a "Personal access token" at: https://www.discogs.com/settings/developers
DISCOGS_TOKEN    = os.getenv("DISCOGS_TOKEN")
DISCOGS_USERNAME = os.getenv("DISCOGS_USERNAME")
DISCOGS_UA       = os.getenv("DISCOGS_UA")

if not all([DISCOGS_TOKEN, USERNAME, PASSWORD]):
    raise SystemExit(
        "Missing required .env variables: DISCOGS_TOKEN, DISCOGS_USERNAME, DISCOGS_UA"
    )


DRY_RUN       = False  # True -> only simulates, doesn't add anything to the wantlist (or use --dry-run)
REQUEST_DELAY = 1.1    # seconds between requests to Discogs (limit: 60/min authenticated)

# Albums already added to the wantlist in previous runs, so they aren't
# searched or requested from Discogs again. Saved next to the script.
CACHE_FILE = Path(__file__).resolve().parent / "wantlist_cache.json"
# ───────────────────────────────────────────────────────────────────────────────────


# ── Cache of already-added albums ───────────────────────────────────────────────────

def load_cache(path: Path = CACHE_FILE) -> dict:
    if not path.exists():
        return {}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"⚠️  Could not read the cache ({path}), starting fresh.")
        return {}

    # Migration from the old format (a single version per album) to the new
    # format, which stores every version added per album.
    for entry in cache.values():
        if "releases" not in entry and "release_id" in entry:
            entry["releases"] = {str(entry.pop("release_id")): entry.pop("title", "")}
    return cache


def save_cache(cache: dict, path: Path = CACHE_FILE) -> None:
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Subsonic / Navidrome (identical to autostar.py) ─────────────────────────────────

def make_token(password: str) -> tuple[str, str]:
    salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return token, salt


def base_params() -> dict:
    token, salt = make_token(PASSWORD)
    return {"u": USERNAME, "t": token, "s": salt, "v": "1.16.1", "c": CLIENT, "f": "json"}


def api_get(endpoint: str, params: dict = None) -> dict:
    all_params = {**base_params(), **(params or {})}
    r = requests.get(f"{SERVER_URL}/rest/{endpoint}", params=all_params, timeout=30)
    r.raise_for_status()
    resp = r.json().get("subsonic-response", {})
    if resp.get("status") != "ok":
        raise RuntimeError(f"Navidrome API error: {resp.get('error', resp)}")
    return resp


def get_playlists() -> list[dict]:
    resp = api_get("getPlaylists")
    pls = resp.get("playlists", {}).get("playlist", [])
    return [pls] if isinstance(pls, dict) else pls


def get_playlist_songs(playlist_id: str) -> list[dict]:
    resp = api_get("getPlaylist", {"id": playlist_id})
    entries = resp.get("playlist", {}).get("entry", [])
    return [entries] if isinstance(entries, dict) else entries


def filter_blacklisted(playlists: list[dict], blacklist: list[str]) -> tuple[list[dict], list[dict]]:
    """Splits playlists into (included, excluded) according to the blacklist
    (case/accent-insensitive comparison)."""
    blacklist_n = {_normalize(name) for name in blacklist}
    included, excluded = [], []
    for pl in playlists:
        (excluded if _normalize(pl.get("name", "")) in blacklist_n else included).append(pl)
    return included, excluded


def get_unique_albums(playlists: list[dict]) -> dict[str, dict]:
    """Walks the playlists (same as autostar.py) and groups songs by albumId,
    keeping the artist + album of each one, without repeating albums, and
    noting which playlist(s) each album appears in (an album can be in several)."""
    albums: dict[str, dict] = {}
    for pl in playlists:
        pl_name = pl.get("name", "")
        for song in get_playlist_songs(pl["id"]):
            album_id = song.get("albumId")
            artist   = song.get("artist")
            album    = song.get("album")
            if not album_id or not artist or not album:
                continue
            entry = albums.setdefault(
                album_id, {"artist": artist, "album": album, "playlists": []}
            )
            if pl_name and pl_name not in entry["playlists"]:
                entry["playlists"].append(pl_name)
    return albums


# ── Discogs API v2.0 ────────────────────────────────────────────────────────────

DISCOGS_HEADERS = {
    "User-Agent": DISCOGS_UA,
    "Authorization": f"Discogs token={DISCOGS_TOKEN}",
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return text.lower().strip()


def discogs_search_releases(artist: str, album: str) -> list[dict]:
    """Searches for the release on Discogs. Returns a list of {'id', 'title'} with ALL
    the versions/editions that match the searched artist (or, if none clearly
    matches, just the first result as a fallback)."""
    params = {
        "artist": artist,
        "release_title": album,
        "type": "release",
        "per_page": 50,
        "page": 1,
    }
    r = requests.get(
        "https://api.discogs.com/database/search",
        headers=DISCOGS_HEADERS, params=params, timeout=30,
    )
    time.sleep(REQUEST_DELAY)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return []

    # We keep every version whose title contains the searched artist, to reduce
    # false positives when the search returns weak matches.
    artist_n = _normalize(artist)
    matches = [res for res in results if artist_n in _normalize(res.get("title", ""))]
    if not matches:
        matches = results[:1]
    return [{"id": res["id"], "title": res.get("title", "")} for res in matches]


def discogs_add_to_wantlist(release_id: int, notes: str | None = None) -> None:
    """Adds (or updates) a release in the wantlist. If `notes` is given, it's
    saved as a comment on that entry (e.g. the source playlist).

    The JSON body must ALWAYS include 'release_id' in addition to 'notes': this
    is what the official Discogs client does (discogs_client.models.Wantlist.add),
    and without that field the API accepts the request (returns 201) but
    silently ignores the notes."""
    url = f"https://api.discogs.com/users/{DISCOGS_USERNAME}/wants/{release_id}"
    payload = {"release_id": str(release_id)}
    if notes:
        payload["notes"] = notes
    r = requests.put(url, headers=DISCOGS_HEADERS, json=payload, timeout=30)
    time.sleep(REQUEST_DELAY)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Discogs API error ({r.status_code}): {r.text[:200]}")


# ── Main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Adds the albums from Navidrome playlists to the Discogs wantlist."
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=DRY_RUN,
        help="Only simulates, doesn't add anything to the wantlist or touch the cache.",
    )
    parser.add_argument(
        "--reset-cache", action="store_true",
        help="Ignores the existing cache and searches/adds every album again.",
    )
    parser.add_argument(
        "--cache-file", type=Path, default=CACHE_FILE,
        help=f"Path to the cache file (default: {CACHE_FILE}).",
    )
    parser.add_argument(
        "--exclude-playlist", action="append", default=[], metavar="NAME",
        help="Name of a playlist to ignore (can be repeated). Added to PLAYLIST_BLACKLIST.",
    )
    return parser.parse_args()


def main():
    if "PON_AQUI" in DISCOGS_TOKEN or "PON_AQUI" in DISCOGS_USERNAME:
        print("⚠️  Set DISCOGS_TOKEN and DISCOGS_USERNAME at the top of the script before running it.")
        print("    Personal token: https://www.discogs.com/settings/developers")
        return

    args = parse_args()
    cache = {} if args.reset_cache else load_cache(args.cache_file)

    print("Connecting to Navidrome...\n")
    playlists = get_playlists()
    if not playlists:
        print("No playlists found.")
        return

    playlists, excluded = filter_blacklisted(playlists, PLAYLIST_BLACKLIST + args.exclude_playlist)

    print(f"Playlists found: {len(playlists)}")
    for pl in playlists:
        print(f"  • {pl.get('name')}  ({pl.get('songCount', '?')} songs)")
    if excluded:
        print(f"Playlists excluded by blacklist: {len(excluded)}")
        for pl in excluded:
            print(f"  ⊘ {pl.get('name')}")

    if not playlists:
        print("\nAll playlists are blacklisted, nothing to do.")
        return

    albums = get_unique_albums(playlists)
    print(f"\nUnique albums: {len(albums)}\n")

    added, skipped, not_found, errors = 0, 0, 0, 0
    for album_id, info in albums.items():
        artist, album = info["artist"], info["album"]
        label = f"{artist} – {album}"
        source_playlists = ", ".join(info.get("playlists", []))
        notes = f"Via Navidrome, playlist(s): {source_playlists}" if source_playlists else None

        entry = cache.get(album_id, {"artist": artist, "album": album, "releases": {}})
        already = entry["releases"]

        try:
            releases = discogs_search_releases(artist, album)
            if not releases:
                not_found += 1
                print(f"  ? Not found on Discogs: {label}")
                continue

            new_releases = [rel for rel in releases if str(rel["id"]) not in already]
            if not new_releases:
                skipped += 1
                continue

            print(f"  {label}  ({len(new_releases)} new version(s) out of {len(releases)} found)")
            for rel in new_releases:
                if args.dry_run:
                    note_txt = f" — note: «{notes}»" if notes else ""
                    print(f"    · [dry-run] Would add: {rel['title']} (id {rel['id']}){note_txt}")
                    continue

                discogs_add_to_wantlist(rel["id"], notes=notes)
                added += 1
                print(f"    ✓ Added: {rel['title']} (id {rel['id']})")

                # Marked as added and saved immediately, so no progress is lost
                # if the script is interrupted mid-run.
                already[str(rel["id"])] = rel["title"]
                cache[album_id] = entry
                save_cache(cache, args.cache_file)
        except Exception as e:
            errors += 1
            print(f"  ✗ Error with {label}: {e}")

    if skipped:
        print(f"\n(Skipped {skipped} albums whose versions were already in the cache: {args.cache_file})")
    print(f"\n✅ Done: {added} versions added, {not_found} albums not found, {errors} errors.")


if __name__ == "__main__":
    main()