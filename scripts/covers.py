import os
import time
import hashlib
import secrets
import requests
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
NAVIDROME_USER = os.getenv("NAVIDROME_USER")
NAVIDROME_PASS = os.getenv("NAVIDROME_PASSWORD")
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", None)

# Constante de tiempo de espera en segundos entre peticiones a APIs externas
# Se puede configurar desde el archivo .env o usar 1.0 segundo por defecto
WAIT_TIME = float(os.getenv("WAIT_TIME", "1.0"))

HEADERS = {
    "User-Agent": "Covers"
}

def get_subsonic_params() -> dict:
    salt = secrets.token_hex(6)
    token = hashlib.md5((NAVIDROME_PASS + salt).encode("utf-8")).hexdigest()
    return {
        "u": NAVIDROME_USER,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "Covers",
        "f": "json"
    }

def get_albums_from_navidrome() -> list[dict]:
    """Obtiene la lista completa de álbumes usando paginación (offset) para superar el límite de 500."""
    if not NAVIDROME_USER or not NAVIDROME_PASS:
        print("❌ Error: NAVIDROME_USER y NAVIDROME_PASS deben estar configurados en el .env")
        return []

    all_albums = []
    offset = 0
    page_size = 500  # Tamaño del lote por petición

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
            print(f"  📥 Obtenidos {len(all_albums)} álbumes hasta el momento...")

            # Si el lote devuelto es menor que el tamaño pedido, ya hemos llegado al final
            if len(album_batch) < page_size:
                break

            offset += page_size

        except Exception as e:
            print(f"❌ Error al conectar con la API de Navidrome en offset {offset}: {e}")
            break

    return all_albums

def get_cover_musicbrainz(artist: str, album: str) -> str | None:
    try:
        query = f'artist:"{artist}" AND release:"{album}"'
        url = f"https://musicbrainz.org/ws/2/release/?query={urllib.parse.quote(query)}&fmt=json&limit=1"
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        releases = res.json().get("releases", [])
        
        if releases:
            release_id = releases[0]["id"]
            caa_res = requests.get(f"https://coverartarchive.org/release/{release_id}", headers=HEADERS, timeout=10)
            if caa_res.status_code == 200:
                for img in caa_res.json().get("images", []):
                    if img.get("front"):
                        return img.get("image")
    except Exception:
        pass
    return None

def get_cover_itunes(artist: str, album: str) -> str | None:
    try:
        query = f"{artist} {album}"
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=album&limit=1"
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        results = res.json().get("results", [])
        if results:
            raw_url = results[0].get("artworkUrl100", "")
            if raw_url:
                return raw_url.replace("100x100bb.jpg", "3000x3000bb.jpg")
    except Exception:
        pass
    return None

def get_cover_discogs(artist: str, album: str) -> str | None:
    if not DISCOGS_TOKEN:
        return None
    try:
        query_url = f"https://api.discogs.com/database/search?q={urllib.parse.quote(artist + ' ' + album)}&type=release&token={DISCOGS_TOKEN}"
        res = requests.get(query_url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        results = res.json().get("results", [])
        if results and "cover_image" in results[0]:
            return results[0]["cover_image"]
    except Exception:
        pass
    return None

def fetch_cover_cascade(artist: str, album: str) -> tuple[str | None, str]:
    url = get_cover_musicbrainz(artist, album)
    if url:
        return url, "MusicBrainz"

    url = get_cover_itunes(artist, album)
    if url:
        return url, "iTunes"

    url = get_cover_discogs(artist, album)
    if url:
        return url, "Discogs"

    return None, "Ninguna"

def download_image(url: str, dest_path: Path) -> bool:
    try:
        res = requests.get(url, headers=HEADERS, stream=True, timeout=25)
        res.raise_for_status()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception:
        if dest_path.exists():
            dest_path.unlink()
        return False

def main():
    print(f"📡 Conectando a Navidrome en {NAVIDROME_URL}...")
    albums = get_albums_from_navidrome()

    if not albums:
        print("❌ No se obtuvieron álbumes de Navidrome.")
        return

    total = len(albums)
    print(f"🔍 Total de álbumes encontrados en Navidrome: {total}\n")

    downloaded = 0
    skipped = 0
    not_found = 0

    for idx, alb in enumerate(albums, start=1):
        artist = alb.get("artist", "Desconocido")
        title = alb.get("title", alb.get("name", "Desconocido"))
        
        album_path_str = alb.get("path")
        if not album_path_str:
            base_dir = os.getenv("MUSIC_PATH", "Z:/music/baldman/SINFI/Organizado")
            album_dir = Path(base_dir) / artist / title
        else:
            album_dir = Path(album_path_str)

        target_file = album_dir / "cover.jpg"
        label = f"[{idx}/{total}] {artist} – {title}"

        # Si el archivo ya existe localmente, omitimos la petición y NO aplicamos tiempo de espera
        if target_file.exists():
            skipped += 1
            continue

        print(f"🔎 Procesando: {label}")
        image_url, source = fetch_cover_cascade(artist, title)

        if not image_url:
            print("    ❌ No se encontró la portada en ninguna red.")
            not_found += 1
        else:
            print(f"    ⭐ Encontrado en {source}: {image_url}")
            if download_image(image_url, target_file):
                print(f"    ✅ Guardado en {target_file}")
                downloaded += 1
            else:
                print("    ⚠️ Error al descargar el archivo de imagen.")

        # Pausa entre peticiones a APIs externas para evitar bloqueos
        time.sleep(WAIT_TIME)

    print("\n" + "=" * 50)
    print("📊 RESUMEN FINAL:")
    print(f"  • Portadas descargadas: {downloaded}")
    print(f"  • Omitidas (ya existían en disco): {skipped}")
    print(f"  • No encontradas: {not_found}")
    print("=" * 50)

if __name__ == "__main__":
    main()