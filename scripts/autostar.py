#!/usr/bin/env python3
"""
Syncs the 'Liked' (starred) songs in Navidrome with the playlists:

  - Songs that are in at least one playlist      -> starred
  - Starred songs that are in NO playlist anymore -> unstarred

Uses the Subsonic API supported by Navidrome — batch mode for maximum efficiency.

Usage:
    python autostar.py              # sync (star + unstar)
    python autostar.py --dry-run    # only shows what would change
    python autostar.py --no-unstar  # old behaviour: only stars, never unstars

Note: only SONGS are handled. Starred albums/artists are left untouched.
"""

import argparse
import hashlib
import random
import string
import os
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ───────────────────────────────────────────────────────────
SERVER_URL_PRIMARY  = os.getenv("NAVIDROME_URL")
SERVER_URL_FALLBACK = os.getenv("NAVIDROME_URL_2")
USERNAME            = os.getenv("NAVIDROME_USER")
PASSWORD            = os.getenv("NAVIDROME_PASSWORD")
CLIENT              = "autostar-script"
BATCH_SIZE          = 128   # IDs per request (adjustable)

if not all([SERVER_URL_PRIMARY, USERNAME, PASSWORD]):
    raise SystemExit(
        "Missing required .env variables: NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD"
    )

SERVER_URL = SERVER_URL_PRIMARY
# ─────────────────────────────────────────────────────────────────────────────


def make_token(password: str) -> tuple[str, str]:
    salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return token, salt


def base_params() -> dict:
    token, salt = make_token(PASSWORD)
    return {"u": USERNAME, "t": token, "s": salt, "v": "1.16.1", "c": CLIENT, "f": "json"}


def _request(server: str, endpoint: str, params: list | dict) -> requests.Response:
    return requests.get(f"{server}/rest/{endpoint}", params=params, timeout=10)


def api_get(endpoint: str, params: dict = None) -> dict:
    all_params = {**base_params(), **(params or {})}
    r = _try_with_fallback(endpoint, all_params)
    resp = r.json().get("subsonic-response", {})
    if resp.get("status") != "ok":
        raise RuntimeError(f"API error: {resp.get('error', resp)}")
    return resp


def api_get_multi(endpoint: str, key: str, values: list[str]) -> dict:
    """Request with multiple values for the same key (e.g. id=1&id=2&id=3)."""
    token, salt = make_token(PASSWORD)
    params = [
        ("u", USERNAME), ("t", token), ("s", salt),
        ("v", "1.16.1"), ("c", CLIENT), ("f", "json"),
    ] + [(key, v) for v in values]
    r = _try_with_fallback(endpoint, params)
    resp = r.json().get("subsonic-response", {})
    if resp.get("status") != "ok":
        raise RuntimeError(f"API error: {resp.get('error', resp)}")
    return resp


def _try_with_fallback(endpoint: str, params) -> requests.Response:
    """Tries the primary server, and falls back to the secondary URL on failure."""
    global SERVER_URL
    try:
        r = _request(SERVER_URL, endpoint, params)
        r.raise_for_status()
        return r
    except (requests.ConnectionError, requests.Timeout):
        if not SERVER_URL_FALLBACK or SERVER_URL == SERVER_URL_FALLBACK:
            raise
        print(f"  [WARN] {SERVER_URL} unreachable, trying fallback {SERVER_URL_FALLBACK}...")
        SERVER_URL = SERVER_URL_FALLBACK
        r = _request(SERVER_URL, endpoint, params)
        r.raise_for_status()
        return r


def get_playlists() -> list[dict]:
    resp = api_get("getPlaylists")
    pls = resp.get("playlists", {}).get("playlist", [])
    return [pls] if isinstance(pls, dict) else pls


def get_playlist_songs(playlist_id: str) -> list[dict]:
    resp = api_get("getPlaylist", {"id": playlist_id})
    entries = resp.get("playlist", {}).get("entry", [])
    return [entries] if isinstance(entries, dict) else entries


def get_starred_songs() -> dict[str, str]:
    """Returns {song_id: 'Artist – Title'} for every currently starred song."""
    resp = api_get("getStarred2")
    songs = resp.get("starred2", {}).get("song", [])
    if isinstance(songs, dict):
        songs = [songs]
    return {
        s["id"]: f"{s.get('artist', '?')} – {s.get('title', '?')}"
        for s in songs
    }


def star_batch(song_ids: list[str]) -> None:
    api_get_multi("star", "id", song_ids)


def unstar_batch(song_ids: list[str]) -> None:
    api_get_multi("unstar", "id", song_ids)


def run_in_batches(ids: list[str], action, verb: str) -> tuple[int, int]:
    """Calls `action(batch)` for each chunk of BATCH_SIZE ids.
    Returns (ok_count, error_count)."""
    batches = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
    ok = errors = 0
    for i, batch in enumerate(batches, 1):
        try:
            action(batch)
            ok += len(batch)
            print(f"  Batch {i}/{len(batches)} ✓  ({len(batch)} songs {verb})")
        except Exception as e:
            errors += len(batch)
            print(f"  Batch {i}/{len(batches)} ✗  — {e}")
    return ok, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Syncs Navidrome starred songs with the songs in playlists."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only shows what would be starred/unstarred, changes nothing.",
    )
    parser.add_argument(
        "--no-unstar", action="store_true",
        help="Only star songs from playlists; never remove existing stars.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("Connecting to Navidrome...\n")

    playlists = get_playlists()
    if not playlists:
        print("No playlists found.")
        return

    print(f"Playlists found: {len(playlists)}")
    for pl in playlists:
        print(f"  • {pl.get('name')}  ({pl.get('songCount', '?')} songs)")

    # Collect unique IDs from all playlists. If ANY playlist fails to load we
    # must not unstar anything, otherwise its songs would look "orphaned".
    playlist_ids: set[str] = set()
    failed_playlists: list[str] = []
    for pl in playlists:
        try:
            for song in get_playlist_songs(pl["id"]):
                playlist_ids.add(song["id"])
        except Exception as e:
            failed_playlists.append(pl.get("name", pl["id"]))
            print(f"  [WARN] Could not read playlist '{pl.get('name')}': {e}")

    starred = get_starred_songs()

    to_star   = sorted(playlist_ids - starred.keys())
    to_unstar = sorted(starred.keys() - playlist_ids)

    print(f"\nSongs in playlists   : {len(playlist_ids)}")
    print(f"Currently starred    : {len(starred)}")
    print(f"To star (new)        : {len(to_star)}")
    print(f"To unstar (orphaned) : {len(to_unstar)}")

    if args.dry_run:
        print("\n── DRY RUN: nothing will be changed ──")

    # ── Star ────────────────────────────────────────────────────────────────
    starred_ok = star_err = 0
    if to_star and not args.dry_run:
        print()
        starred_ok, star_err = run_in_batches(to_star, star_batch, "starred")

    # ── Unstar ──────────────────────────────────────────────────────────────
    unstarred_ok = unstar_err = 0
    skip_reason = None
    if args.no_unstar:
        skip_reason = "--no-unstar given"
    elif failed_playlists:
        skip_reason = f"could not read {len(failed_playlists)} playlist(s): {', '.join(failed_playlists)}"
    elif not playlist_ids and starred:
        skip_reason = "playlists contain no songs (refusing to unstar everything)"

    if to_unstar:
        if skip_reason:
            print(f"\n⚠️  Skipping unstar: {skip_reason}")
        elif args.dry_run:
            print("\nWould unstar:")
            for sid in to_unstar:
                print(f"  - {starred[sid]}")
        else:
            print()
            unstarred_ok, unstar_err = run_in_batches(to_unstar, unstar_batch, "unstarred")

    if args.dry_run:
        print(f"\n✅ Dry run finished: {len(to_star)} would be starred, "
              f"{0 if skip_reason else len(to_unstar)} would be unstarred.")
    else:
        print(f"\n✅ Done: {starred_ok} starred, {unstarred_ok} unstarred, "
              f"{star_err + unstar_err} errors.")


if __name__ == "__main__":
    main()