#!/usr/bin/env python3
"""
Borra del disco las canciones que estén en la playlist "_BORRAR" de Navidrome
y lanza un escaneo de la biblioteca para que desaparezcan de Navidrome.

Por defecto funciona en modo simulación (no borra nada).
Usa --ejecutar para borrar de verdad.

Requisitos:
    pip install requests python-dotenv

Variables usadas del .env:
    NAVIDROME_URL, NAVIDROME_URL_2 (fallback), NAVIDROME_USER,
    NAVIDROME_PASSWORD, MUSIC_PATH
"""

from __future__ import annotations

import argparse
import hashlib
import os
import posixpath
import secrets
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

SERVER_URL_PRIMARY  = os.getenv("NAVIDROME_URL")
SERVER_URL_FALLBACK = os.getenv("NAVIDROME_URL_2")
USERNAME            = os.getenv("NAVIDROME_USER")
PASSWORD            = os.getenv("NAVIDROME_PASSWORD")
CLIENT              = "discogs-wantlist-script"

MUSIC_PATH    = os.getenv("MUSIC_PATH")
EXCLUDED_PATH = os.getenv("EXCLUDED_PATH")
# Raíz de la música tal y como la ve Navidrome (p. ej. "/music" si corre en Docker)
NAVIDROME_ROOT = os.getenv("NAVIDROME_MUSIC_ROOT", "/music")
PLAYLIST_NAME = "_BORRAR"
API_VERSION   = "1.16.1"


class Navidrome:
    def __init__(self, urls, user, password):
        self.user = user
        self.password = password
        self.base_url = self._elegir_url(urls)

    def _auth_params(self):
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {
            "u": self.user,
            "t": token,
            "s": salt,
            "v": API_VERSION,
            "c": CLIENT,
            "f": "json",
        }

    def _get(self, base, endpoint, **params):
        r = requests.get(
            f"{base}/rest/{endpoint}",
            params={**self._auth_params(), **params},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()["subsonic-response"]
        if data.get("status") != "ok":
            err = data.get("error", {})
            raise RuntimeError(f"Error de Navidrome: {err.get('message', data)}")
        return data

    def _elegir_url(self, urls):
        """Prueba cada URL en orden y se queda con la primera que responda."""
        for url in urls:
            url = url.rstrip("/")
            try:
                self._get(url, "ping")
                print(f"Conectado a {url}")
                return url
            except Exception as e:
                print(f"No se pudo usar {url}: {e}")
        sys.exit("No se pudo conectar a ninguna URL de Navidrome.")

    def get(self, endpoint, **params):
        return self._get(self.base_url, endpoint, **params)


def resolver_ruta(music_path: Path, ruta_navidrome: str):
    """
    Convierte la ruta que da Navidrome en una ruta real dentro de MUSIC_PATH.

    Cubre: ruta que ya cuelga de MUSIC_PATH (Navidrome nativo), ruta bajo
    NAVIDROME_MUSIC_ROOT (p. ej. /music en Docker) y rutas relativas a la
    biblioteca. Devuelve None si el resultado no está estrictamente dentro
    de MUSIC_PATH.
    """
    p = posixpath.normpath(ruta_navidrome.replace("\\", "/"))
    music = music_path.as_posix()
    root = NAVIDROME_ROOT.rstrip("/")

    if p == music or p.startswith(music + "/"):
        local = Path(p)
    else:
        if root and (p == root or p.startswith(root + "/")):
            p = p[len(root):]

        # Evita duplicar la carpeta raíz cuando Navidrome devuelve
        # /music/Organizado/... y MUSIC_PATH ya termina en "Organizado".
        relativa = p.lstrip("/")
        nombre = music_path.name
        if relativa == nombre or relativa.startswith(nombre + "/"):
            relativa = relativa[len(nombre):].lstrip("/")

        local = music_path / relativa

    # Colapsa ".." para que ninguna ruta pueda escapar de MUSIC_PATH.
    local = Path(posixpath.normpath(local.as_posix()))

    if music_path not in local.parents:
        return None
    return local


def esta_excluida(excluded: Path | None, ruta: Path) -> bool:
    return excluded is not None and (ruta == excluded or excluded in ruta.parents)


def limpiar_carpetas_vacias(carpeta: Path, raiz: Path):
    """Elimina carpetas vacías hacia arriba, sin tocar la raíz de la música."""
    raiz = raiz.resolve()
    carpeta = carpeta.resolve()
    while carpeta != raiz and raiz in carpeta.parents:
        try:
            carpeta.rmdir()  # solo funciona si está vacía
        except OSError:
            break
        carpeta = carpeta.parent


def main():
    parser = argparse.ArgumentParser(description=f"Borra las canciones de la playlist {PLAYLIST_NAME}")
    parser.add_argument("--ejecutar", action="store_true",
                        help="Borra los archivos de verdad (por defecto solo simula)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="No pedir confirmación")
    args = parser.parse_args()

    urls = [u for u in (SERVER_URL_PRIMARY, SERVER_URL_FALLBACK) if u]

    if not (urls and USERNAME and PASSWORD and MUSIC_PATH):
        sys.exit("Faltan variables en el .env (URL, USER, PASSWORD o MUSIC_PATH).")

    music_path = Path(MUSIC_PATH).expanduser()
    if not music_path.is_dir():
        sys.exit(f"MUSIC_PATH no existe o no es una carpeta: {music_path}")

    nd = Navidrome(urls, USERNAME, PASSWORD)

    # 1) Buscar la playlist
    playlists = nd.get("getPlaylists").get("playlists", {}).get("playlist", [])
    pl = next((p for p in playlists if p.get("name") == PLAYLIST_NAME), None)
    if not pl:
        sys.exit(f'No se encontró la playlist "{PLAYLIST_NAME}".')

    # 2) Obtener sus canciones
    canciones = nd.get("getPlaylist", id=pl["id"]).get("playlist", {}).get("entry", [])
    if not canciones:
        print(f'La playlist "{PLAYLIST_NAME}" está vacía. Nada que borrar.')
        return

    print(f'\nPlaylist "{PLAYLIST_NAME}": {len(canciones)} canciones\n')

    # 3) Localizar los archivos en disco
    excluded = (
        Path(os.path.expandvars(EXCLUDED_PATH)).expanduser() if EXCLUDED_PATH else None
    )

    a_borrar, omitidas = [], []
    for c in canciones:
        etiqueta = f'{c.get("artist", "?")} - {c.get("title", "?")}'
        nav_path = c.get("path", "")
        fichero = resolver_ruta(music_path, nav_path) if nav_path else None

        if fichero is None:
            omitidas.append(etiqueta)
            print(f"  [FUERA DE MUSIC_PATH] {etiqueta} ({nav_path or 'sin ruta'})")
        elif esta_excluida(excluded, fichero):
            omitidas.append(etiqueta)
            print(f"  [EXCLUIDA]            {fichero}")
        elif not fichero.is_file():
            omitidas.append(etiqueta)
            print(f"  [NO ENCONTRADO]       {fichero}")
        else:
            a_borrar.append((etiqueta, fichero))
            print(f"  [borrar]              {fichero}")

    print(f"\nA borrar: {len(a_borrar)} | Omitidas: {len(omitidas)}")

    if not a_borrar:
        return

    if not args.ejecutar:
        print("\nModo simulación: no se ha borrado nada. Usa --ejecutar para borrar.")
        return

    if not args.yes:
        resp = input(f"\n¿Borrar {len(a_borrar)} archivos de forma PERMANENTE? (escribe 'si'): ")
        if resp.strip().lower() not in ("si", "sí", "s", "yes", "y"):
            print("Cancelado.")
            return

    # 4) Borrar archivos
    borrados = 0
    for etiqueta, fichero in a_borrar:
        try:
            carpeta = fichero.parent
            fichero.unlink()
            borrados += 1
            limpiar_carpetas_vacias(carpeta, music_path)
        except OSError as e:
            print(f"  Error borrando {fichero}: {e}")

    print(f"\nArchivos borrados: {borrados}/{len(a_borrar)}")

    # 5) Escanear para que Navidrome elimine las canciones de la biblioteca
    try:
        nd.get("startScan")
        print("Escaneo de Navidrome iniciado.")
    except Exception as e:
        print(f"No se pudo iniciar el escaneo: {e}")


if __name__ == "__main__":
    main()