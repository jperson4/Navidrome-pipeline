#!/usr/bin/env python3
"""
fix_tags.py — Corrige artistas/títulos ID3 SOLO donde hace falta (sustituye a clean.py).

Qué arregla respecto a clean.py
  * Ya NO parte dentro de palabras ("Grovskopa", "Alexander", "Lift", "Drift",
    "Depresión", "Sandwell", "Brando"...). Los delimitadores exigen espacios o
    signos y "feat/ft" solo cuentan como palabra completa.
  * Conserva TODOS los artistas ya presentes (clean.py solo leía el primero).
  * "Título - Remixer Remix" pasa a "Título (Remixer Remix)" en vez de
    convertir "Remixer Remix" en el título.
  * Quita basura típica de rips: prefijo/sufijo clave Camelot + BPM
    ("9A - 125 - "), nº de pista dentro del título, marcas www., "VINYL ONLY",
    espacios raros (NBSP) y guiones colgantes.
  * "Artista - Título" dentro del título solo se corrige cuando es seguro
    (el artista ya está en la etiqueta, o la etiqueta es VA/vacía). Lo dudoso
    NO se toca: va al CSV como REVISAR con una sugerencia.
  * Los remixers solo se añaden si el nombre es verosímil ("Original", "Radio",
    "Digital", "Continuous"... se descartan). El título no se modifica por ello.

Los archivos que ya están bien no se tocan (no se reescribe ni su fecha).

Uso
    python fix_tags.py                      # dry-run: solo genera el CSV
    python fix_tags.py /ruta/musica         # carpeta concreta
    python fix_tags.py --apply              # aplica las correcciones AUTO
    python fix_tags.py --apply --include-review   # aplica también las REVISAR
    python fix_tags.py --restore deshacer_XXXX.json   # revierte una ejecución
    python fix_tags.py -e Duplicados -e NO  # carpetas a ignorar (por nombre o ruta)

Requiere: pip install mutagen   (python-dotenv opcional: lee MUSIC_PATH del .env)
"""

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mutagen import MutagenError
from mutagen.easyid3 import EasyID3

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # el .env es opcional
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURACIÓN (edítala a gusto)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_FOLDER = os.getenv("MUSIC_PATH", ".")
DEFAULT_EXCLUDE = ["Duplicados"]          # carpetas que se ignoran por defecto

# Nombres de artista que NO deben separarse aunque lleven "&", "," o "and".
PROTECTED_ARTISTS = [
    "Rhythm & Sound", "Baby Doc And The Dentist", "Blood & Tears",
    "Amadou & Mariam", "Victor Santana & Band", "Earth, Wind & Fire",
    "Tyler, The Creator", "Crosby, Stills, Nash & Young", "Simon & Garfunkel",
    "Hall & Oates", "Florence + The Machine", "Echo & The Bunnymen",
    "Chase & Status",
]

# Etiquetas de artista que en realidad no son un artista.
PLACEHOLDER_ARTISTS = [
    "VA", "Various Artists", "Various", "Varios Artistas", "Varios",
    "Unknown Artist", "Artista desconocido", "Unknown", "Desconocido",
]

# Si al separar sale una de estas "partes", NO se separa (no son artistas).
GENERIC_PARTS = [
    "the", "band", "the band", "orchestra", "his orchestra", "orquesta",
    "conjunto", "ensemble", "choir", "coro", "friends", "co", "company",
    "sons", "family", "crew",
]

# Palabras que describen una versión y nunca son un remixer.
VERSION_WORDS = {
    "original", "extended", "club", "radio", "vocal", "instrumental", "dub",
    "album", "short", "long", "main", "full", "digital", "bonus", "continuous",
    "ambient", "acoustic", "live", "deep", "dark", "tribal", "rave",
    "progressive", "eurowave", "euro", "trance", "house", "techno", "breaks",
    "minimal", "hard", "acid", "electro", "funk", "disco", "jungle",
    "hardcore", "uk", "us", "remastered", "remaster", "edit", "mix", "remix",
    "version", "mixed", "unmixed", "clean", "dirty", "explicit", "tv",
    "video", "intro", "outro", "bootleg", "vip", "reprise", "edition",
    "special", "alternate", "alternative", "alt", "single", "lp", "ep",
    "vinyl", "only", "part", "pt", "process", "main", "master", "mastered",
}
NEUTRAL_WORDS = {"the", "a", "an", "of", "and", "de", "el", "la", "feat"}


# ─────────────────────────────────────────────────────────────────────────────
#  UTILIDADES DE TEXTO
# ─────────────────────────────────────────────────────────────────────────────

def clean_ws(s: str) -> str:
    """Quita espacios raros (NBSP, zero-width...), colapsa y recorta."""
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
    s = re.sub(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def key(s: str) -> str:
    """Clave de comparación: sin mayúsculas, acentos ni símbolos."""
    n = unicodedata.normalize("NFKD", s)
    n = "".join(c for c in n if not unicodedata.combining(c)).casefold()
    return re.sub(r"[\W_]+", "", n) or s.strip().casefold()


PROTECTED_KEYS = {key(a) for a in PROTECTED_ARTISTS}
PLACEHOLDER_KEYS = {key(a) for a in PLACEHOLDER_ARTISTS}
GENERIC_KEYS = {key(a) for a in GENERIC_PARTS}


def depth_map(text: str) -> list:
    dep, d = [], 0
    for ch in text:
        dep.append(d)
        if ch in "([{":
            d += 1
        elif ch in ")]}" and d:
            d -= 1
    return dep


def split_top(text: str, rx: "re.Pattern", maxsplit: int = 0) -> list:
    """Parte por `rx` solo fuera de paréntesis/corchetes."""
    dep = depth_map(text)
    parts, last = [], 0
    for m in rx.finditer(text):
        if dep[m.start()] != 0:
            continue
        parts.append(text[last:m.start()])
        last = m.end()
        if maxsplit and len(parts) >= maxsplit:
            break
    parts.append(text[last:])
    return parts


def dedupe(items) -> list:
    seen, out = set(), []
    for it in items:
        k = key(it)
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  SEPARAR ARTISTAS
# ─────────────────────────────────────────────────────────────────────────────

# Los delimitadores de palabra exigen espacios a ambos lados → no parten
# "Grovskopa", "Alexander", "Bando", etc.
ARTIST_DELIM = re.compile(
    r"""
      \s*[;,]\s*(?:(?:&|and)\s+)?                 # ;  ,  ", &"
    | \s+(?:&|\+|and|vs\.?|b2b|feat\.?|ft\.?|featuring
           |pres\.?|presents|presenta)\s+         # palabras sueltas
    | \s+x\s+(?![&+,;])                           # "A x B" (pero no "Percy X & Mark")
    """,
    re.IGNORECASE | re.VERBOSE,
)


def split_artists(s: str) -> list:
    s = clean_ws(s)
    if not s:
        return []
    if key(s) in PROTECTED_KEYS or re.search(r"\baka\b", s, re.I):
        return [s]                                 # nombre protegido / alias "X aka Y"

    # Enmascara los nombres protegidos que aparezcan DENTRO ("IRAH & Chase & Status")
    masked, saved = s, {}
    for i, name in enumerate(PROTECTED_ARTISTS):
        tok = f"\x00{i}\x00"
        new_s = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", tok, masked, flags=re.I)
        if new_s != masked:
            saved[tok] = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", masked, re.I).group(0)
            masked = new_s

    parts = [clean_ws(p) for p in split_top(masked, ARTIST_DELIM)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return [s]
    for tok, original in saved.items():
        parts = [p.replace(tok, original) for p in parts]
    if any(key(p) in GENERIC_KEYS or len(key(p)) < 2 for p in parts):
        return [s]                                 # "X & Band", "Jad & The"...
    return dedupe(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  PATRONES DE BASURA EN TÍTULO / ARTISTA
# ─────────────────────────────────────────────────────────────────────────────

KEYBPM_PREFIX = re.compile(r"^(?:1[0-2]|[1-9])[AB]\s*-\s*\d{1,3}(?:\.\d+)?\s*-\s*", re.I)
KEYBPM_SUFFIX = re.compile(
    r"\s+-\s+(?:1[0-2]|[1-9])[AB](?:\s+-\s+(?:\d{2,3}(?:\.\d+)?|Energy\s+\d+))?\s*$", re.I)
TRACKNUM = re.compile(r"^\d{1,3}\s*[-.]\s+(?=[^\W\d])|^\d{1,3}-(?=[^\W\d])")
WWW = re.compile(r"\s*\bwww\.[\w-]+(?:\.[\w-]+)+\b", re.I)
VINYL = re.compile(r"\s*\bVINYL ONLY\b\s*$", re.I)
VINYL_SIDE = re.compile(r"^[A-D]\d\s+(?=\S)", re.I)
SIDE_LOOSE = re.compile(r"^[A-H]\d{1,2}\s+(?=\S)", re.I)
DANGLING = re.compile(r"\s+-\s*$")
TITLE_SEP = re.compile(r"\s+(?:-{1,2}|–|—)\s+")
VERSION_RIGHT = re.compile(
    r"^(?:\S.*\s)?(?:remix|rmx|rework|refix|bootleg|mix|edit|dub|version|vip|remaster(?:ed)?)$",
    re.I)

FEAT_PAREN = re.compile(r"\s*[(\[]\s*(?:feat\b\.?|ft\b\.?|featuring\b)\s+([^()\[\]]+?)\s*[)\]]", re.I)
FEAT_BARE = re.compile(
    r"\s+(?:feat\b\.?|ft\b\.?|featuring\b)\s+([^()\[\]]+?)(?=\s+-\s+|\s*[(\[]|$)", re.I)

CERTAIN_KW = r"(?:remix|rmx|rework|re-work|refix|bootleg|remake)"
CERTAIN_RE = re.compile(rf"^(.+?)\s+{CERTAIN_KW}\b", re.I)
SOFT_RE = re.compile(r"^(.+?)\s+(?:mix|edit|dub)$", re.I)
TRAIL_KW = re.compile(rf"\s+(?:{CERTAIN_KW}|mix|edit|dub)$", re.I)
GROUP_RE = re.compile(r"[(\[]([^()\[\]]+)[)\]]")


def name_like(text: str) -> bool:
    """¿Parece un nombre de artista (y no un trozo de título)?"""
    return (1 <= len(text.split()) <= 6 and ":" not in text
            and len(key(text)) > 2                      # descarta "A1", "B", "b1"
            and not re.fullmatch(r"[\W\d_]+", text))


def plausible_remixer(name: str, soft: bool, known: set) -> bool:
    """¿Es verosímil que `name` sea un remixer y no una descripción de la versión?

    "(X Remix/Rework/Bootleg)": se acepta si tiene aspecto de nombre.
    "(X Mix/Edit/Dub)": demasiado ambiguo ("Bass Line Mix", "All Night Mix"),
    así que solo se acepta si X ya existe como artista en tu biblioteca.
    """
    toks = re.findall(r"[^\W_]+", name.casefold())
    if not toks or len(name) < 2:
        return False
    if all(t in VERSION_WORDS or t in NEUTRAL_WORDS or t.isdigit() for t in toks):
        return False
    if key(name) in known:
        return True
    if soft:
        return False
    if '"' in name or re.search(r"\w['’]s\s+\w", name):   # "Midland's Mass Seduction"
        return False
    if any(t in VERSION_WORDS for t in toks):                # "Oscar Mulero Digital Bonus"
        return False
    return len(toks) <= 4


def is_dup_of_artist(left: str, artists: list, joined_key: str) -> bool:
    lk = key(left)
    if not lk:
        return False
    if lk == joined_key:
        return True
    for a in artists:
        ak = key(a)
        if lk == ak or (lk.endswith(ak) and 0 < len(lk) - len(ak) <= 3):  # "M3.Grovskopa"
            return True
    parts = split_artists(left)
    return len(parts) > 1 and all(key(p) in {key(a) for a in artists} for p in parts)


# ─────────────────────────────────────────────────────────────────────────────
#  PLAN DE CAMBIOS PARA UN ARCHIVO
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Context:
    known_keys: set = field(default_factory=set)       # artistas vistos en la biblioteca
    left_counts: Counter = field(default_factory=Counter)  # ("artista","izq") → nº archivos


@dataclass
class Plan:
    artists: list
    title: str
    rules: list
    suggestion: str = ""
    changed: bool = False


def plan(orig_artists: list, orig_title: str, ctx: Context, apply_review: bool = False) -> Plan:
    rules = []

    def hit(r):
        if r not in rules:
            rules.append(r)

    art_in = [a for a in orig_artists if a and a.strip()]
    title = orig_title or ""

    # 1. espacios raros ------------------------------------------------------
    t1 = clean_ws(title)
    a1 = [x for x in (clean_ws(a) for a in art_in) if x]
    if t1 != title or a1 != art_in:
        hit("espacios")
    title, raw_artists = t1, a1

    # 2. basura de rips -------------------------------------------------------
    n = KEYBPM_SUFFIX.sub("", KEYBPM_PREFIX.sub("", title))
    if n != title:
        hit("clave/BPM"); title = n
    stripped = [KEYBPM_PREFIX.sub("", a) for a in raw_artists]
    if stripped != raw_artists:
        hit("clave/BPM"); raw_artists = [a for a in stripped if a]
    n = TRACKNUM.sub("", title)
    if n != title:
        hit("nº pista"); title = n
    n = WWW.sub("", title)
    if n != title:
        hit("web"); title = n
    if VINYL.search(title):
        title = VINYL_SIDE.sub("", VINYL.sub("", title))
        raw_artists = [VINYL_SIDE.sub("", a) for a in raw_artists]
        hit("vinilo")
    n = DANGLING.sub("", title)
    if n != title:
        hit("guion final"); title = n
    title = clean_ws(title)

    # 3. separar artistas ------------------------------------------------------
    artists = dedupe([p for a in raw_artists for p in split_artists(a)])
    if artists != raw_artists:
        hit("artistas separados")
    joined_key = key(" ".join(raw_artists))

    # 4. "Artista - Título" dentro del título ------------------------------------
    def first_split(t):
        p = split_top(t, TITLE_SEP, maxsplit=1)
        if len(p) == 2 and clean_ws(p[0]) and clean_ws(p[1]):
            return clean_ws(p[0]), clean_ws(p[1])
        return None

    while artists:                                     # a) el artista ya está
        sp = first_split(title)
        if sp and is_dup_of_artist(sp[0], artists, joined_key):
            title = sp[1]; hit("artista repetido en título")
        else:
            break

    placeholder = (not artists) or all(key(a) in PLACEHOLDER_KEYS for a in artists)
    sp = first_split(title)
    if placeholder and sp:                             # b) etiqueta vacía / VA
        left = SIDE_LOOSE.sub("", sp[0])
        if name_like(left) and not VERSION_RIGHT.match(sp[1]):
            artists = split_artists(left); title = sp[1]
            hit("artista extraído del título")

    sp = first_split(title)                            # c) "Título - X Remix"
    if sp and len(sp[1].split()) <= 7 and VERSION_RIGHT.match(sp[1]):
        title = f"{sp[0]} ({sp[1]})"; hit("versión tras guion")

    suggestion = ""
    sp = first_split(title)                            # d) dudoso → REVISAR
    if (sp and artists and not placeholder and name_like(sp[0])
            and len(sp[1]) > 1 and not sp[1].isdigit()
            and ctx.left_counts[(joined_key, key(sp[0]))] < 3):   # <3: no es nombre de suite/álbum
        suggestion = f'Artista ← "{sp[0]}", Título ← "{sp[1]}"'
        if apply_review:
            artists = split_artists(sp[0]); title = sp[1]
            hit("artista de título (dudoso)")

    # 5. feat. → artista -------------------------------------------------------
    if artists:
        for rx in (FEAT_PAREN, FEAT_BARE):
            m = rx.search(title)
            if m:
                names = split_artists(m.group(1))
                new_t = clean_ws(title[:m.start()] + title[m.end():])
                if names and new_t:
                    title = new_t
                    artists = dedupe(artists + names)
                    hit("feat → artista")

    # 6. remixer → artista (el título NO se toca) -----------------------------
    if artists:
        have = {key(a) for a in artists}
        for m in GROUP_RE.finditer(title):
            inner = clean_ws(m.group(1))
            mm, soft = CERTAIN_RE.match(inner), False
            if not mm:
                mm, soft = SOFT_RE.match(inner), True
            if not mm:
                continue
            for part in split_artists(mm.group(1)):
                part = TRAIL_KW.sub("", part).strip()
                if part and key(part) not in have and plausible_remixer(part, soft, ctx.known_keys):
                    artists.append(part); have.add(key(part)); hit("remixer → artista")

    # 7. limpieza final ----------------------------------------------------------
    title = clean_ws(re.sub(r"\(\s*\)|\[\s*\]", "", title))
    title = clean_ws(DANGLING.sub("", title))
    if not title:                                      # seguridad: nunca dejar vacío
        title = clean_ws(orig_title or "")
    artists = dedupe(artists)

    changed = (artists != art_in) or (title != (orig_title or ""))
    return Plan(artists, title, rules if changed else [], suggestion, changed)


# ─────────────────────────────────────────────────────────────────────────────
#  LECTURA / ESCRITURA
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Item:
    path: Path
    artists: list
    title: str


def is_excluded(path: Path, excludes: list) -> bool:
    low_parts = {p.casefold() for p in path.parts}
    for ex in excludes:
        exp = Path(os.path.expanduser(ex))
        if len(exp.parts) == 1 and exp.name.casefold() in low_parts:
            return True
        try:
            path.resolve().relative_to(exp.resolve())
            return True
        except (ValueError, OSError):
            pass
    return False


def read_items(folder: Path, excludes: list):
    items, unreadable = [], 0
    for p in sorted(folder.rglob("*")):
        if p.suffix.lower() != ".mp3" or not p.is_file() or is_excluded(p, excludes):
            continue
        try:
            tags = EasyID3(str(p))
        except MutagenError:
            unreadable += 1                            # sin ID3 → nada que corregir
            continue
        items.append(Item(p, list(tags.get("artist", [])), (tags.get("title", [""]) or [""])[0]))
    return items, unreadable


def build_context(items) -> Context:
    ctx = Context()
    for it in items:
        names = [x for a in it.artists for x in split_artists(clean_ws(a))]
        ctx.known_keys.update(key(n) for n in names)
        sp = split_top(clean_ws(it.title), TITLE_SEP, maxsplit=1)
        if len(sp) == 2:
            joined = key(" ".join(clean_ws(a) for a in it.artists))
            ctx.left_counts[(joined, key(sp[0]))] += 1
    return ctx


def write_tags(path: Path, artists: list, title: str):
    audio = EasyID3(str(path))
    if artists:
        audio["artist"] = artists
    audio["title"] = [title]
    audio.save()


def restore(undo_file: str):
    data = json.loads(Path(undo_file).read_text(encoding="utf-8"))
    ok = bad = 0
    for e in data["files"]:
        try:
            audio = EasyID3(e["path"])
            if e["artist"]:
                audio["artist"] = e["artist"]
            else:
                audio.pop("artist", None)
            audio["title"] = [e["title"]]
            audio.save()
            ok += 1
        except Exception as ex:                       # noqa: BLE001
            bad += 1
            print(f"[!] {e['path']}: {ex}")
    print(f"↩️  Restaurados {ok} archivos ({bad} errores).")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Corrige artistas/títulos ID3 solo donde hace falta.")
    ap.add_argument("folder", nargs="?", default=DEFAULT_FOLDER, help="Carpeta a escanear")
    ap.add_argument("--apply", action="store_true", help="Escribe los cambios (sin esto: dry-run)")
    ap.add_argument("--include-review", action="store_true",
                    help="Aplica también las sugerencias marcadas REVISAR")
    ap.add_argument("-e", "--exclude", action="append", default=list(DEFAULT_EXCLUDE),
                    metavar="CARPETA", help="Carpeta a ignorar (nombre o ruta). Repetible")
    ap.add_argument("--csv", default="cambios_etiquetas.csv", help="Informe CSV")
    ap.add_argument("--restore", metavar="JSON", help="Revierte una ejecución con su archivo deshacer_*.json")
    args = ap.parse_args()

    if args.restore:
        restore(args.restore)
        return

    folder = Path(os.path.expanduser(args.folder)).resolve()
    if not folder.is_dir():
        sys.exit(f"'{folder}' no es una carpeta válida.")

    print(f"Escaneando {folder} (ignorando: {', '.join(args.exclude) or '—'})")
    items, unreadable = read_items(folder, args.exclude)
    ctx = build_context(items)
    print(f"{len(items)} MP3 con etiquetas ({unreadable} sin ID3, omitidos).\n")

    rows, auto, review = [], [], 0
    rule_count = Counter()
    for it in items:
        p = plan(it.artists, it.title, ctx, apply_review=args.include_review)
        if p.changed:
            auto.append((it, p))
            rule_count.update(p.rules)
            estado, shown = "AUTO", p
        elif p.suggestion:
            shown = plan(it.artists, it.title, ctx, apply_review=True)
            estado = "REVISAR"; review += 1
        else:
            continue
        rows.append({
            "Estado": estado, "Archivo": it.path.name, "Ruta": str(it.path),
            "Artista Original": " / ".join(it.artists), "Título Original": it.title,
            "Artistas Nuevos": " / ".join(shown.artists), "Título Nuevo": shown.title,
            "Reglas": " + ".join(shown.rules), "Sugerencia (sin aplicar)": p.suggestion,
        })

    if rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)

    print(f"✔ Sin tocar (ya estaban bien): {len(items) - len(auto) - review}")
    print(f"🔧 A corregir (AUTO):           {len(auto)}")
    print(f"❓ Dudosos (REVISAR, sin tocar): {review}")
    for r, n in rule_count.most_common():
        print(f"     {n:5d}  {r}")
    if rows:
        print(f"\n📄 Informe: {args.csv}")

    if not args.apply:
        print("\n🛑 DRY-RUN: no se ha modificado ningún archivo. Usa --apply para aplicar.")
        return
    if not auto:
        return

    undo = Path(f"deshacer_{datetime.now():%Y%m%d_%H%M%S}.json")
    undo.write_text(json.dumps({"files": [
        {"path": str(it.path), "artist": it.artists, "title": it.title} for it, _ in auto
    ]}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n💾 Copia de seguridad de etiquetas: {undo}")

    done = errors = 0
    for it, p in auto:
        try:
            write_tags(it.path, p.artists, p.title)
            done += 1
        except Exception as ex:                       # noqa: BLE001
            errors += 1
            print(f"[!] {it.path.name}: {ex}")
    print(f"✅ {done} archivos corregidos, {errors} errores.")
    print(f"   Para deshacer: python {Path(sys.argv[0]).name} --restore {undo}")


if __name__ == "__main__":
    main()