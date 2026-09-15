#!/usr/bin/env python3
"""
Marks all songs saved in Navidrome playlists as 'Liked' (starred).
Uses the Subsonic API supported by Navidrome — batch mode for maximum efficiency.
"""

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


def star_batch(song_ids: list[str]) -> None:
    api_get_multi("star", "id", song_ids)


def main():
    print("Connecting to Navidrome...\n")

    playlists = get_playlists()
    if not playlists:
        print("No playlists found.")
        return

    print(f"Playlists found: {len(playlists)}")
    for pl in playlists:
        print(f"  • {pl.get('name')}  ({pl.get('songCount', '?')} songs)")

    # Collect unique IDs
    all_ids: set[str] = set()
    for pl in playlists:
        for song in get_playlist_songs(pl["id"]):
            all_ids.add(song["id"])

    id_list = list(all_ids)
    total   = len(id_list)
    batches = [id_list[i:i+BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]

    print(f"\nUnique songs: {total}  →  {len(batches)} request(s) of up to {BATCH_SIZE} IDs\n")

    starred = 0
    errors  = 0
    for i, batch in enumerate(batches, 1):
        try:
            star_batch(batch)
            starred += len(batch)
            print(f"  Batch {i}/{len(batches)} ✓  ({len(batch)} songs)")
        except Exception as e:
            errors += len(batch)
            print(f"  Batch {i}/{len(batches)} ✗  — {e}")

    print(f"\n✅ Done: {starred} songs marked as Liked, {errors} errors.")


if __name__ == "__main__":
    main()