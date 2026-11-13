import os
import re
import time
import hashlib
import secrets
import unicodedata
import requests
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv

from mutagen import File as MutagenFile
from mutagen.mp3 import MP3, HeaderNotFoundError
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC

load_dotenv()

NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
NAVIDROME_USER = os.getenv("NAVIDROME_USER")
# Acepta NAVIDROME_PASSWORD (el que usan el resto de scripts) o NAVIDROME_PASS (el antiguo)
NAVIDROME_PASS = os.getenv("NAVIDROME_PASSWORD") or os.getenv("NAVIDROME_PASS")
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", None)

# Directorio raíz que contiene las carpetas principales (bandcamp, BORRAR, DJ, SINFI...).
# Acepta MUSIC_BASE_ROOT o MUSIC_PATH (el mismo que usan los otros scripts).
MUSIC_BASE_ROOT = Path(os.path.expanduser(
    os.getenv("MUSIC_BASE_ROOT") or os.getenv("MUSIC_PATH") or "/home/escriva/music/baldman"
))

# Carpetas principales donde buscar. Por defecto se detectan solas: todas las
# subcarpetas de MUSIC_BASE_ROOT. Para limitarlas: MAIN_FOLDERS="DJ,SINFI,CIBELES"
MAIN_FOLDERS = [f.strip() for f in os.getenv("MAIN_FOLDERS", "").split(",") if f.strip()]

WAIT_TIME = float(os.getenv("WAIT_TIME", "1.0"))

# SKIP_COMPLETE=1 -> no consulta internet para álbumes que ya tienen cover.jpg e imagen incrustada
SKIP_COMPLETE = os.getenv("SKIP_COMPLETE", "0") == "1"

AUDIO_EXT = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".opus", ".aac", ".wma", ".aiff", ".aif", ".ape", ".wv"}

HEADERS = {
    "User-Agent": "NavidromeCoverPipeline/1.0 (https://github.com/jperson4/Navidrome-pipeline)"
}


# ─────────────────────────────────────────────────────────────────────────────
#  NAVIDROME
# ─────────────────────────────────────────────────────────────────────────────

def get_subsonic_params() -> dict:
    salt = secrets.token_hex(6)
    token = hashlib.md5((NAVIDROME_PASS + salt).encode("utf-8")).hexdigest()
    return {
        "u": NAVIDROME_USER,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "NavidromePipeline",
        "f": "json"
    }


def get_albums_from_navidrome() -> list[dict]:
    if not NAVIDROME_USER or not NAVIDROME_PASS:
        print("❌ Error: NAVIDROME_USER y NAVIDROME_PASSWORD deben estar configurados en el .env")
        return []

    all_albums = []
    offset = 0
    page_size = 500

    while True:
        params = get_subsonic_params()
        params.update({
            "type": "alphabeticalByArtist",
            "size": page_size,
            "offset": offset
        })

        url = f"{NAVIDROME_URL}/rest/getAlbumList2"

        try:
            res = requests.get(url, params=params, headers=HEADERS, timeout=15)
            res.raise_for_status()
            data = res.json()

            album_batch = data.get("subsonic-response", {}).get("albumList2", {}).get("album", [])
            if not album_batch:
                break

            all_albums.extend(album_batch)

            if len(album_batch) < page_size:
                break

            offset += page_size
        except Exception as e:
            print(f"❌ Error al conectar con la API de Navidrome: {e}")
            break

    return all_albums


def get_album_songs(session: requests.Session, album_id: str) -> list[dict]:
    """Canciones del álbum según Navidrome (cada una con su 'path' relativo)."""
    params = get_subsonic_params()
    params["id"] = album_id
    res = session.get(f"{NAVIDROME_URL}/rest/getAlbum", params=params, headers=HEADERS, timeout=15)
    res.raise_for_status()
    resp = res.json().get("subsonic-response", {})
    if resp.get("status") != "ok":
        raise RuntimeError(resp.get("error", resp))
    songs = resp.get("album", {}).get("song", [])
    return [songs] if isinstance(songs, dict) else songs


# ─────────────────────────────────────────────────────────────────────────────
#  LOCALIZAR DÓNDE ESTÁN REALMENTE LOS ARCHIVOS
# ─────────────────────────────────────────────────────────────────────────────

def get_search_roots() -> list[Path]:
    """Carpetas principales donde buscar música."""
    if not MUSIC_BASE_ROOT.is_dir():
        print(f"❌ No existe MUSIC_BASE_ROOT: {MUSIC_BASE_ROOT}")
        return []
    if MAIN_FOLDERS:
        roots = []
        for name in MAIN_FOLDERS:
            p = MUSIC_BASE_ROOT / name
            if p.is_dir():
                roots.append(p)
            else:
                print(f"⚠️ Carpeta principal no encontrada, se ignora: {p}")
        return roots
    return sorted(p for p in MUSIC_BASE_ROOT.iterdir() if p.is_dir())


def build_index(roots: list[Path]) -> tuple[dict[str, list[Path]], int]:
    """Recorre las carpetas UNA vez y devuelve {nombre_de_archivo: [rutas donde está]}."""
    index: dict[str, list[Path]] = {}
    total = 0
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() in AUDIO_EXT:
                    index.setdefault(f.casefold(), []).append(Path(dirpath) / f)
                    total += 1
    return index, total


def _norm(s: str) -> str:
    """Compara nombres ignorando mayúsculas, acentos y símbolos."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return re.sub(r"[\W_]+", "", s)


def read_album_tag(path: Path) -> str:
    try:
        if path.suffix.lower() == ".mp3":
            frame = ID3(path).get("TALB")
            return str(frame.text[0]) if frame and frame.text else ""
        f = MutagenFile(path, easy=True)
        return (f.tags.get("album") or [""])[0] if f and f.tags else ""
    except Exception:
        return ""


def locate_song(song: dict, album_name: str, index: dict[str, list[Path]]) -> list[Path]:
    """
    Devuelve dónde está realmente el archivo de una canción.
    1) Por la cola de la ruta que da Navidrome (Artista/Álbum/archivo.mp3), que sirve
       aunque no diga en qué carpeta principal está.
    2) Si el archivo se movió a otro sitio (p. ej. suelto en BORRAR/), por el tag de álbum.
    """
    rel = (song.get("path") or "").replace("\\", "/").strip("/")
    if not rel:
        return []
    parts = rel.split("/")
    cands = index.get(parts[-1].casefold(), [])
    if not cands:
        return []

    for k in range(min(len(parts), 4), 1, -1):          # cola de 4, 3 y 2 componentes
        tail = "/" + "/".join(parts[-k:]).casefold()
        hits = [p for p in cands if p.as_posix().casefold().endswith(tail)]
        if hits:
            return hits

    target = _norm(album_name)
    return [p for p in cands if _norm(read_album_tag(p)) == target]


def locate_album_files(songs: list[dict], album_name: str, index: dict[str, list[Path]]) -> list[Path]:
    seen, files = set(), []
    for song in songs:
        for p in locate_song(song, album_name, index):
            if p not in seen:
                seen.add(p)
                files.append(p)
    return files


def audio_in_dir(folder: Path) -> set[Path]:
    try:
        return {p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT}
    except OSError:
        return set()


def rel_to_root(path: Path) -> str:
    try:
        return str(path.relative_to(MUSIC_BASE_ROOT))
    except ValueError:
        return str(path)


# ─────────────────────────────────────────────────────────────────────────────
#  MP3
# ─────────────────────────────────────────────────────────────────────────────

def mp3_has_embedded_cover(file_path: Path) -> bool:
    """Comprueba si el MP3 ya tiene portada interna APIC."""
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None:
            return False
        for tag in audio.tags.values():
            if isinstance(tag, APIC):
                return True
    except Exception:
        pass
    return False


def update_mp3_metadata(file_path: Path, image_bytes: bytes | None, mime_type: str,
                        extra_tags: dict, embed: bool):
    try:
        try:
            easy_audio = EasyID3(file_path)
        except HeaderNotFoundError:
            mp3_obj = MP3(file_path)
            mp3_obj.add_tags()
            mp3_obj.save()
            easy_audio = EasyID3(file_path)

        tags_updated = False
        for tag_name, tag_value in extra_tags.items():
            if tag_value and (tag_name not in easy_audio or not easy_audio[tag_name]):
                easy_audio[tag_name] = str(tag_value)
                tags_updated = True

        if tags_updated:
            easy_audio.save()

        if image_bytes and embed:
            audio = MP3(file_path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime=mime_type,
                    type=3,
                    desc="Cover",
                    data=image_bytes
                )
            )
            audio.save()
            return True, tags_updated

        return False, tags_updated
    except Exception as e:
        print(f"    ⚠️ Error actualizando metadatos en {file_path.name}: {e}")
        return False, False


# ─────────────────────────────────────────────────────────────────────────────
#  FUENTES DE PORTADAS
# ─────────────────────────────────────────────────────────────────────────────

def get_data_musicbrainz(artist: str, album: str) -> dict | None:
    try:
        query = f'artist:"{artist}" AND release:"{album}"'
        url = f"https://musicbrainz.org/ws/2/release/?query={urllib.parse.quote(query)}&fmt=json&limit=1"
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        releases = res.json().get("releases", [])

        if releases:
            rel = releases[0]
            release_id = rel["id"]

            cover_url = None
            caa_res = requests.get(f"https://coverartarchive.org/release/{release_id}", headers=HEADERS, timeout=10)
            if caa_res.status_code == 200:
                for img in caa_res.json().get("images", []):
                    if img.get("front"):
                        cover_url = img.get("image")
                        break

            date = rel.get("date", "")
            year = date.split("-")[0] if date else None
            label = rel.get("label-info", [{}])[0].get("label", {}).get("name") if rel.get("label-info") else None

            return {
                "cover_url": cover_url,
                "date": year,
                "organization": label,
                "musicbrainz_albumid": release_id,
                "source": "MusicBrainz"
            }
    except Exception:
        pass
    return None


def get_data_itunes(artist: str, album: str) -> dict | None:
    try:
        query = f"{artist} {album}"
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=album&limit=1"
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        results = res.json().get("results", [])
        if results:
            item = results[0]
            raw_url = item.get("artworkUrl100", "")
            cover_url = raw_url.replace("100x100bb.jpg", "3000x3000bb.jpg") if raw_url else None

            release_date = item.get("releaseDate", "")
            year = release_date.split("-")[0] if release_date else None

            return {
                "cover_url": cover_url,
                "date": year,
                "source": "iTunes"
            }
    except Exception:
        pass
    return None


def get_data_discogs(artist: str, album: str) -> dict | None:
    if not DISCOGS_TOKEN:
        return None
    try:
        query_url = f"https://api.discogs.com/database/search?q={urllib.parse.quote(artist + ' ' + album)}&type=release&token={DISCOGS_TOKEN}"
        res = requests.get(query_url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        results = res.json().get("results", [])
        if results:
            item = results[0]
            cover_url = item.get("cover_image")
            year = item.get("year")

            return {
                "cover_url": cover_url,
                "date": str(year) if year else None,
                "source": "Discogs"
            }
    except Exception:
        pass
    return None


def fetch_metadata_cascade(artist: str, album: str) -> dict:
    data = get_data_musicbrainz(artist, album)
    if data:
        return data

    data = get_data_itunes(artist, album)
    if data:
        return data

    data = get_data_discogs(artist, album)
    if data:
        return data

    return {}


def fetch_image_data(url: str) -> tuple[bytes | None, str]:
    try:
        res = requests.get(url, headers=HEADERS, timeout=25)
        res.raise_for_status()
        mime_type = res.headers.get("Content-Type", "image/jpeg")
        return res.content, mime_type
    except Exception:
        return None, "image/jpeg"


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print(f"📡 Conectando a Navidrome en {NAVIDROME_URL}...")
    albums = get_albums_from_navidrome()

    if not albums:
        print("❌ No se obtuvieron álbumes de Navidrome.")
        return

    roots = get_search_roots()
    if not roots:
        return

    total = len(albums)
    print(f"🔍 Total de álbumes encontrados en Navidrome: {total}")
    print(f"📂 Carpetas donde se busca: {', '.join(r.name for r in roots)}")
    print("🗂️  Indexando archivos de audio (una sola pasada)...")
    index, n_files = build_index(roots)
    print(f"   {n_files} archivos indexados\n")

    session = requests.Session()
    downloaded_covers = 0
    updated_files = 0
    not_found_albums = 0
    shared_dirs = 0
    skipped_complete = 0

    for idx, alb in enumerate(albums, start=1):
        artist = alb.get("artist", "Desconocido")
        title = alb.get("title", alb.get("name", "Desconocido"))
        label = f"[{idx}/{total}] {artist} – {title}"

        # 1. Dónde están REALMENTE las canciones de este álbum
        try:
            songs = get_album_songs(session, alb.get("id"))
        except Exception as e:
            print(f"⚠️ No se pudieron leer las canciones de Navidrome para {label}: {e}")
            continue

        files = locate_album_files(songs, title, index)
        if not files:
            print(f"⚠️ canciones no localizadas en disco para: {label}")
            not_found_albums += 1
            continue

        by_dir: dict[Path, list[Path]] = {}
        for f in files:
            by_dir.setdefault(f.parent, []).append(f)

        # 2. Carpetas donde corresponde colocar cover.jpg: solo las que contienen
        #    únicamente canciones de este álbum (si hay audios de otros álbumes, un
        #    cover.jpg sería de uno solo y estaría mal para el resto).
        cover_dirs = []
        for folder, album_files in by_dir.items():
            others = audio_in_dir(folder) - set(album_files)
            if others:
                shared_dirs += 1
                print(f"    ↪ {rel_to_root(folder)} contiene {len(others)} audio(s) de otros álbumes: "
                      f"no se escribe cover.jpg (solo se incrusta en los MP3)")
            elif not (folder / "cover.jpg").exists():
                cover_dirs.append(folder)

        mp3_files = [f for f in files if f.suffix.lower() == ".mp3"]
        need_embed = {f for f in mp3_files if not mp3_has_embedded_cover(f)}

        if SKIP_COMPLETE and not cover_dirs and not need_embed:
            skipped_complete += 1
            continue

        meta = fetch_metadata_cascade(artist, title)

        if not meta:
            time.sleep(WAIT_TIME)
            continue

        source = meta.get("source")
        cover_url = meta.get("cover_url")

        extra_tags = {
            "date": meta.get("date"),
            "organization": meta.get("organization"),
            "musicbrainz_albumid": meta.get("musicbrainz_albumid")
        }

        where = ", ".join(rel_to_root(d) for d in by_dir)
        print(f"🔎 Procesando: {label} (Fuente: {source})")
        print(f"    📍 Ubicación real: {where}")

        # Solo se descarga la imagen si hace falta en algún sitio
        img_bytes, mime_type = (None, "image/jpeg")
        if cover_url and (cover_dirs or need_embed):
            img_bytes, mime_type = fetch_image_data(cover_url)

        # 3. cover.jpg junto a las canciones, donde de verdad están
        if img_bytes:
            for folder in cover_dirs:
                try:
                    with open(folder / "cover.jpg", "wb") as f:
                        f.write(img_bytes)
                    print(f"    📁 Guardado cover.jpg en: {rel_to_root(folder)}")
                    downloaded_covers += 1
                except Exception as e:
                    print(f"    ⚠️ Error escribiendo cover.jpg en {folder}: {e}")

        # 4. Tags e imagen incrustada, solo en los MP3 de ESTE álbum
        for mp3_file in mp3_files:
            cover_added, tags_added = update_mp3_metadata(
                mp3_file, img_bytes, mime_type, extra_tags, embed=mp3_file in need_embed
            )
            if cover_added or tags_added:
                print(f"    🎵 Actualizado {mp3_file.name} (Tags: {tags_added}, Carátula incrustada: {cover_added})")
                updated_files += 1

        time.sleep(WAIT_TIME)

    print("\n" + "=" * 50)
    print("📊 RESUMEN FINAL:")
    print(f"  • Portadas cover.jpg guardadas:            {downloaded_covers}")
    print(f"  • Archivos MP3 actualizados:               {updated_files}")
    print(f"  • Carpetas compartidas (sin cover.jpg):    {shared_dirs}")
    print(f"  • Álbumes ya completos (omitidos):         {skipped_complete}")
    print(f"  • Álbumes no localizados en disco:         {not_found_albums}")
    print("=" * 50)


if __name__ == "__main__":
    main()