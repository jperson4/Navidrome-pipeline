import os
import time
import hashlib
import secrets
import requests
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv

from mutagen.mp3 import MP3, HeaderNotFoundError
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, error

load_dotenv()

NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
NAVIDROME_USER = os.getenv("NAVIDROME_USER")
NAVIDROME_PASS = os.getenv("NAVIDROME_PASS")
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", None)

# Directorio raíz donde residen todas las carpetas principales
MUSIC_BASE_ROOT = Path(os.getenv("MUSIC_BASE_ROOT", "/home/escriva/music/baldman"))

# Lista de carpetas principales conocidas
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

WAIT_TIME = float(os.getenv("WAIT_TIME", "1.0"))

HEADERS = {
    "User-Agent": "NavidromeCoverPipeline/1.0 (https://github.com/jperson4/Navidrome-pipeline)"
}

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
        print("❌ Error: NAVIDROME_USER y NAVIDROME_PASS deben estar configurados en el .env")
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

def resolve_real_album_dir(artist: str, album: str, navidrome_path: str | None) -> Path | None:
    """
    Busca en cuál de las carpetas principales (SINFI, DJ, CIBELES, etc.)
    reside realmente la carpeta del álbum en el disco local.
    """
    # 1. Si la ruta de Navidrome existe directamente en el sistema de archivos
    if navidrome_path:
        p = Path(navidrome_path)
        if p.exists():
            return p

    # 2. Buscar en las carpetas principales conocidas
    for main_folder in MAIN_FOLDERS:
        # Intento A: Ruta estándar con subcarpeta Organizado (ej: SINFI/Organizado/Artista/Album)
        candidate_a = MUSIC_BASE_ROOT / main_folder / "Organizado" / artist / album
        if candidate_a.exists():
            return candidate_a

        # Intento B: Ruta directa (ej: CIBELES/Artista/Album)
        candidate_b = MUSIC_BASE_ROOT / main_folder / artist / album
        if candidate_b.exists():
            return candidate_b

        # Intento C: Búsqueda flexible por nombre de álbum dentro de la carpeta principal
        matches = list((MUSIC_BASE_ROOT / main_folder).glob(f"**/{album}"))
        for m in matches:
            if m.is_dir():
                return m

    return None

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

def update_mp3_metadata(file_path: Path, image_bytes: bytes | None, mime_type: str, extra_tags: dict):
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

        if image_bytes and not mp3_has_embedded_cover(file_path):
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

def main():
    print(f"📡 Conectando a Navidrome en {NAVIDROME_URL}...")
    albums = get_albums_from_navidrome()

    if not albums:
        print("❌ No se obtuvieron álbumes de Navidrome.")
        return

    total = len(albums)
    print(f"🔍 Total de álbumes encontrados en Navidrome: {total}\n")

    downloaded_covers = 0
    updated_files = 0
    not_found_dirs = 0

    for idx, alb in enumerate(albums, start=1):
        artist = alb.get("artist", "Desconocido")
        title = alb.get("title", alb.get("name", "Desconocido"))
        nav_path = alb.get("path")

        label = f"[{idx}/{total}] {artist} – {title}"

        # Resolver dinámicamente la carpeta real (SINFI, CIBELES, DJ, etc.)
        album_dir = resolve_real_album_dir(artist, title, nav_path)

        if not album_dir:
            print(f"⚠️ carpeta no encontrada localmente para: {label}")
            not_found_dirs += 1
            continue

        target_cover = album_dir / "cover.jpg"
        mp3_files = list(album_dir.glob("*.mp3"))

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

        print(f"🔎 Procesando en [{album_dir.parent.name}]: {label} (Fuente: {source})")

        img_bytes, mime_type = (None, "image/jpeg")
        if cover_url:
            img_bytes, mime_type = fetch_image_data(cover_url)

        # 1. Guardar la carátula únicamente en la carpeta del álbum que le corresponde
        if cover_url and img_bytes and not target_cover.exists():
            try:
                with open(target_cover, "wb") as f:
                    f.write(img_bytes)
                print(f"    📁 Guardado {target_cover.name} en: {album_dir}")
                downloaded_covers += 1
            except Exception as e:
                print(f"    ⚠️ Error escribiendo cover.jpg: {e}")

        # 2. Incrustar dentro del MP3 solo si no contiene ya una portada
        for mp3_file in mp3_files:
            cover_added, tags_added = update_mp3_metadata(mp3_file, img_bytes, mime_type, extra_tags)
            if cover_added or tags_added:
                print(f"    🎵 Actualizado {mp3_file.name} (Tags: {tags_added}, Carátula incrustada: {cover_added})")
                updated_files += 1

        time.sleep(WAIT_TIME)

    print("\n" + "=" * 50)
    print("📊 RESUMEN FINAL:")
    print(f"  • Portadas externas guardadas en carpeta: {downloaded_covers}")
    print(f"  • Archivos MP3 actualizados:               {updated_files}")
    print(f"  • Carpetas no localizadas localmente:     {not_found_dirs}")
    print("=" * 50)

if __name__ == "__main__":
    main()