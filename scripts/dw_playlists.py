#!/usr/bin/env python3
"""
dw_playlists.py — Syncs Navidrome playlists (fast version):

  1. Downloads each song ONCE into SONGS_DIR (in parallel), no matter how many
     playlists it appears in.
  2. Generates one .m3u per playlist in RKB_DIR, in the same order as in
     Navidrome (only rewrites the ones that changed).

Usage: python3 dw_playlists.py [--fix-existing]

  Every downloaded MP3 is checked with ffprobe. If it is not a real MP3
  (e.g. AAC/OGG with an .mp3 extension) or uses a sample rate the CDJ/XDJ
  can't play, it is re-encoded to MP3 CBR 320 kbps / 44.1 kHz with ffmpeg.
  Requires ffmpeg + ffprobe in the PATH.

  --fix-existing   also check/convert the files already in _SONGS
                   (run it once to repair a previous sync).

Configuration comes from environment variables. A .env file next to this
script (or in the current directory) is loaded too; real environment
variables take precedence.

  NAVIDROME_URL, NAVIDROME_URL_2   candidate servers; the first reachable one is used
  NAVIDROME_USER, NAVIDROME_PASSWORD
  DW_PLAYLIST_PATH                 destination root folder
"""

import os
import re
import sys
import json
import time
import shutil
import subprocess
import unicodedata
import hashlib
import secrets
import threading
import http.client
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ─────────────────────────────────────────────
# CONFIGURATION (from environment / .env)
# ─────────────────────────────────────────────

def load_env_file(path: Path):
    """Minimal .env loader (KEY=VALUE, optional quotes, # comments).
    Existing environment variables are never overridden."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"  ✗ Missing required environment variable: {name}")
        sys.exit(1)
    return value


load_env_file(Path(__file__).resolve().with_name(".env"))
load_env_file(Path.cwd() / ".env")

SERVER_URLS = [u.strip() for u in (os.environ.get("NAVIDROME_URL", ""),
                                   os.environ.get("NAVIDROME_URL_2", "")) if u.strip()]
if not SERVER_URLS:
    print("  ✗ Set NAVIDROME_URL (and optionally NAVIDROME_URL_2).")
    sys.exit(1)

USERNAME = require("NAVIDROME_USER")
PASSWORD = require("NAVIDROME_PASSWORD")
DEST_DIR = Path(require("DW_PLAYLIST_PATH"))

SONGS_DIR = DEST_DIR / "_SONGS"   # all songs, each downloaded only once
RKB_DIR   = DEST_DIR / "_RKB"     # one .m3u per playlist

API_WORKERS   = 8     # parallel getPlaylist requests
DL_WORKERS    = 4     # parallel downloads (lower it if the server struggles)
RETRIES       = 3
PROBE_TIMEOUT = 3     # seconds to wait when checking which server is reachable

# CDJ/XDJ compatibility (rekordbox export error [2])
FFMPEG        = shutil.which("ffmpeg")
FFPROBE       = shutil.which("ffprobe")
MP3_RATES     = {32000, 44100, 48000}   # sample rates CDJ/XDJ accept for MP3
NATIVE_EXT    = {"mp3", "wav", "aif", "aiff", "flac", "m4a", "aac"}
STRIP_ACCENTS = False
FIX_EXISTING  = "--fix-existing" in sys.argv

CLIENT  = "navidrome-sync"
VERSION = "1.16.1"

blackList = ['_DESCARTES', '_BORRAR']


# ─────────────────────────────────────────────
# Subsonic API (persistent connection per thread)
# ─────────────────────────────────────────────

def _make_base() -> dict:
    """Token/salt generated once per run (Subsonic allows reusing them)."""
    salt = secrets.token_hex(8)
    return {
        "u": USERNAME,
        "t": hashlib.md5(f"{PASSWORD}{salt}".encode()).hexdigest(),
        "s": salt,
        "v": VERSION,
        "c": CLIENT,
        "f": "json",
    }


BASE    = _make_base()
SERVER  = None        # chosen server URL
_u      = None        # its parsed form
_PREFIX = ""
_local  = threading.local()


def _conn_class(scheme: str):
    return http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection


def _probe(url: str) -> bool:
    """True if the server answers a Subsonic ping successfully."""
    u = urllib.parse.urlsplit(url)
    conn = _conn_class(u.scheme)(u.hostname, u.port, timeout=PROBE_TIMEOUT)
    try:
        conn.request("GET", f"{u.path.rstrip('/')}/rest/ping?{urllib.parse.urlencode(BASE)}")
        data = json.loads(conn.getresponse().read())
        return data.get("subsonic-response", {}).get("status") == "ok"
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        conn.close()


def select_server():
    """Use the first reachable URL from SERVER_URLS."""
    global SERVER, _u, _PREFIX
    for url in SERVER_URLS:
        if _probe(url):
            SERVER, _u = url, urllib.parse.urlsplit(url)
            _PREFIX = _u.path.rstrip("/")
            return
        print(f"   ⚠ Not reachable: {url}")
    print("  ✗ No Navidrome server is reachable.")
    sys.exit(1)


def _reset_conn():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None


def _open(path: str):
    """GET reusing the thread's TCP connection. Returns the unread response."""
    for attempt in (0, 1):
        conn = getattr(_local, "conn", None)
        if conn is None:
            conn = _local.conn = _conn_class(_u.scheme)(_u.hostname, _u.port, timeout=60)
        try:
            conn.request("GET", path)
            return conn.getresponse()
        except (http.client.HTTPException, OSError):
            _reset_conn()
            if attempt:
                raise


def api_get(endpoint: str, extra: dict = None) -> dict:
    q = urllib.parse.urlencode({**BASE, **(extra or {})})
    r = _open(f"{_PREFIX}/rest/{endpoint}?{q}")
    body = r.read()
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status} on {endpoint}")
    root = json.loads(body).get("subsonic-response", {})
    if root.get("status") != "ok":
        err = root.get("error", {})
        raise RuntimeError(f"API error ({err.get('code')}): {err.get('message')}")
    return root


# ─────────────────────────────────────────────
# File names
# ─────────────────────────────────────────────

_ILLEGAL = re.compile(r'[\\/*?:"<>|]')
_SPACES  = re.compile(r'\s+')


def safe_name(name: str) -> str:
    name = _ILLEGAL.sub("_", name.strip())
    name = _SPACES.sub(" ", name)
    return name[:200]


def strip_accents(text: str) -> str:
    """Removes diacritics only (non-Latin scripts are left untouched)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def song_filename(song: dict) -> str:
    """'Artist - Title.ext' (the real order is given by the .m3u).
    Formats a CDJ/XDJ can't play (ogg, opus, wma...) are stored as .mp3
    and transcoded after downloading."""
    ext = song.get("suffix", "mp3").lower()
    if ext not in NATIVE_EXT:
        ext = "mp3"
    title  = safe_name(song.get("title", song["id"]))
    artist = safe_name(song.get("artist", ""))
    if STRIP_ACCENTS:
        title, artist = strip_accents(title), strip_accents(artist)
    base   = f"{artist} - {title}" if artist else title
    return f"{base}.{ext}"


# ─────────────────────────────────────────────
# Step 1: playlists (parallel) and song index
# ─────────────────────────────────────────────

def fetch_playlists() -> dict:
    """{pl_name: [song, ...]} in real playlist order, excluding blackList."""
    playlists = api_get("getPlaylists").get("playlists", {}).get("playlist", [])
    wanted = [(safe_name(pl["name"]), pl["id"]) for pl in playlists
              if safe_name(pl["name"]) not in blackList]

    def one(item):
        name, pid = item
        return name, api_get("getPlaylist", {"id": pid}).get("playlist", {}).get("entry", [])

    with ThreadPoolExecutor(API_WORKERS) as ex:
        return dict(ex.map(one, wanted))   # map preserves order


def build_song_index(playlists_songs: dict) -> dict:
    """{song_id: filename}. Disambiguates name collisions (case-insensitive,
    like Windows) by appending part of the song id."""
    id_to_filename: dict = {}
    used: set = set()

    for songs in playlists_songs.values():
        for song in songs:
            sid = song["id"]
            if sid in id_to_filename:
                continue
            fname = song_filename(song)
            if fname.lower() in used:
                stem, ext = os.path.splitext(fname)
                fname = f"{stem} [{sid[:8]}]{ext}"
            used.add(fname.lower())
            id_to_filename[sid] = fname
    return id_to_filename


# ─────────────────────────────────────────────
# Step 2: parallel downloads / orphan cleanup
# ─────────────────────────────────────────────

def is_cdj_compatible(path: Path) -> bool:
    """For .mp3 files: the audio stream must be real MP3 at 32/44.1/48 kHz.
    Other extensions are not checked."""
    if path.suffix.lower() != ".mp3":
        return True
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate", "-of", "json", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        return False                      # unreadable / corrupt
    try:
        stream = json.loads(r.stdout)["streams"][0]
        return stream["codec_name"] == "mp3" and int(stream["sample_rate"]) in MP3_RATES
    except (KeyError, IndexError, ValueError):
        return False


def convert_to_mp3(src: Path, dst: Path):
    """Re-encodes to MP3 CBR 320 kbps / 44.1 kHz, keeping the tags
    (embedded artwork is dropped). Returns None on success, else the error."""
    r = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", str(src),
         "-map", "0:a:0", "-map_metadata", "0", "-vn",
         "-c:a", "libmp3lame", "-b:a", "320k", "-ar", "44100",
         "-id3v2_version", "3", "-f", "mp3", str(dst)],
        capture_output=True, text=True)
    if r.returncode != 0:
        dst.unlink(missing_ok=True)
        return (r.stderr.strip() or "ffmpeg failed")[-200:]
    return None


def finalize(tmp: Path, dest: Path):
    """Moves tmp to dest, converting first if it isn't CDJ-compatible.
    Returns (error, converted)."""
    if not (FFMPEG and FFPROBE) or is_cdj_compatible(tmp):
        os.replace(tmp, dest)
        return None, False
    out = dest.with_name(dest.name + ".conv")
    err = convert_to_mp3(tmp, out)
    tmp.unlink(missing_ok=True)
    if err:
        return f"conversion failed: {err}", False
    os.replace(out, dest)
    return None, True


def fix_existing_files(names):
    """Checks/converts already-downloaded .mp3 files in place."""
    mp3s = [n for n in names if n.lower().endswith(".mp3")]
    print(f"── Checking {len(mp3s)} existing MP3(s) ──")

    def one(name):
        path = SONGS_DIR / name
        if is_cdj_compatible(path):
            return name, None, False
        out = path.with_name(name + ".conv")
        err = convert_to_mp3(path, out)
        if err:
            return name, err, False
        os.replace(out, path)
        return name, None, True

    fixed = failed = 0
    with ThreadPoolExecutor(DL_WORKERS) as ex:
        for name, err, converted in ex.map(one, mp3s):
            if err:
                failed += 1
                print(f"   ✗ {name}: {err}")
            elif converted:
                fixed += 1
                print(f"   🔧 Converted: {name}")
    print(f"   → {fixed} converted, {failed} failed.\n")


def download_song(sid: str, dest: Path):
    """Streaming download. Returns (error or None, converted)."""
    tmp  = dest.with_name(dest.name + ".part")
    path = f"{_PREFIX}/rest/download?{urllib.parse.urlencode({**BASE, 'id': sid})}"
    last = None
    for attempt in range(RETRIES):
        try:
            r = _open(path)
            ctype = r.getheader("Content-Type", "")
            if r.status != 200 or "json" in ctype or "xml" in ctype:
                r.read()
                raise RuntimeError(f"HTTP {r.status} ({ctype})")
            with open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return finalize(tmp, dest)
        except Exception as e:
            last = e
            _reset_conn()
            tmp.unlink(missing_ok=True)
            time.sleep(1.5 * (attempt + 1))
    return str(last), False


def list_dir(directory: Path) -> dict:
    """{name: size} using a single scandir."""
    if not directory.exists():
        return {}
    with os.scandir(directory) as it:
        return {e.name: e.stat().st_size for e in it if e.is_file()}


def remove_orphans(directory: Path, present: dict, expected: set):
    for name in present:
        if name not in expected:
            print(f"   🗑  Removing: {name}")
            (directory / name).unlink()


# ─────────────────────────────────────────────
# Step 3: .m3u files (only when they change)
# ─────────────────────────────────────────────

def write_m3u(pl_name: str, songs: list, id_to_filename: dict) -> bool:
    lines = ["#EXTM3U"]
    for song in songs:
        fname   = id_to_filename[song["id"]]
        title   = song.get("title", Path(fname).stem)
        artist  = song.get("artist", "")
        display = f"{artist} - {title}" if artist else title
        lines.append(f"#EXTINF:{song.get('duration', -1)},{display}")
        lines.append(str(SONGS_DIR / fname))

    content  = "\n".join(lines) + "\n"
    m3u_path = RKB_DIR / f"{pl_name}.m3u"
    if m3u_path.exists() and m3u_path.read_text(encoding="utf-8") == content:
        return False
    m3u_path.write_text(content, encoding="utf-8")
    return True


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def main():
    t0 = time.perf_counter()
    print("🎵 Navidrome Playlist Sync")

    if not (FFMPEG and FFPROBE):
        print("   ⚠ ffmpeg/ffprobe not found in PATH: MP3 compatibility check disabled.")
    select_server()
    print(f"   Server          : {SERVER}")
    print(f"   Songs           : {SONGS_DIR}")
    print(f"   Playlists (m3u) : {RKB_DIR}\n")

    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    RKB_DIR.mkdir(parents=True, exist_ok=True)

    try:
        playlists_songs = fetch_playlists()
    except Exception as e:
        print(f"  ✗ {e}")
        sys.exit(1)
    if not playlists_songs:
        print("No playlists found.")
        return
    print(f"{len(playlists_songs)} playlist(s) (after blackList).")

    id_to_filename = build_song_index(playlists_songs)
    print(f"Unique songs: {len(id_to_filename)}\n")

    # ── Downloads ──
    existing = list_dir(SONGS_DIR)
    if FIX_EXISTING and FFMPEG and FFPROBE:
        fix_existing_files(existing)
    todo = [(sid, fname) for sid, fname in id_to_filename.items()
            if existing.get(fname, 0) == 0]          # missing or empty
    print(f"── Downloads: {len(todo)} pending, "
          f"{len(id_to_filename) - len(todo)} already present ──")

    ok = fail = 0
    if todo:
        with ThreadPoolExecutor(DL_WORKERS) as ex:
            futs = {ex.submit(download_song, sid, SONGS_DIR / fname): fname
                    for sid, fname in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                err, converted = fut.result()
                if err is None:
                    ok += 1
                    tag = " (converted to MP3 320)" if converted else ""
                    print(f"   ↓ [{i}/{len(todo)}] {futs[fut]}{tag}")
                else:
                    fail += 1
                    print(f"   ✗ [{i}/{len(todo)}] {futs[fut]}: {err}")
    print(f"   → {ok} downloaded, {fail} failed.")

    remove_orphans(SONGS_DIR, list_dir(SONGS_DIR), set(id_to_filename.values()))

    # ── m3u ──
    print("\n── Playlists (.m3u) ──")
    changed = 0
    for pl_name, songs in playlists_songs.items():
        if write_m3u(pl_name, songs, id_to_filename):
            changed += 1
            print(f"   📝 {pl_name}.m3u ({len(songs)} songs)")
    print(f"   → {changed} updated, {len(playlists_songs) - changed} unchanged.")

    remove_orphans(RKB_DIR, list_dir(RKB_DIR), {f"{n}.m3u" for n in playlists_songs})

    print(f"\n✅ Sync completed in {time.perf_counter() - t0:.1f}s.")


if __name__ == "__main__":
    main()
