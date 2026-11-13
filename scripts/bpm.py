#!/usr/bin/env python3
"""
bpm_tagger.py

Recursively walks a folder, computes the BPM of each MP3 file
using Essentia (or fallback to librosa), and writes the result to the 
ID3 TBPM tag along with a custom BPM_ENGINE tag (mutagen).

When executed with Essentia available, files previously tagged with 'librosa'
will be automatically re-processed with Essentia and updated.

Usage:
    python bpm_tagger.py                     # uses MUSIC_PATH from .env
    python bpm_tagger.py /path/to/music      # explicit folder overrides .env
    python bpm_tagger.py -f                  # force recalculation even if BPM already set
    python bpm_tagger.py --dry-run           # only show results, don't write
    python bpm_tagger.py -j 4                # 4 parallel processes

Requires:
    pip install librosa mutagen numpy essentia python-dotenv --break-system-packages
"""

import argparse
import os
import sys
import warnings
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import librosa
from mutagen.id3 import ID3, TBPM, error
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from dotenv import load_dotenv

try:
    import essentia.standard as es
    HAS_ESSENTIA = True
except ImportError:
    HAS_ESSENTIA = False

load_dotenv()

warnings.filterwarnings("ignore")  # librosa/numba throw noisy warnings

EXTENSIONS = {".mp3"}
DEFAULT_MUSIC_PATH = os.getenv("ROOT_PATH")

# Registrar la etiqueta personalizada 'bpm_engine' en EasyID3
try:
    EasyID3.RegisterTXXXKey('bpm_engine', 'BPM_ENGINE')
except KeyError:
    pass  # Ya registrada previamente


def get_existing_metadata(path: Path) -> tuple[float | None, str | None]:
    """
    Reads the TBPM tag and BPM_ENGINE tag if present.
    Returns (bpm, engine).
    """
    bpm = None
    engine = None

    try:
        tags = EasyID3(path)
        if "bpm" in tags and tags["bpm"]:
            try:
                bpm = float(str(tags["bpm"][0]).strip())
            except ValueError:
                bpm = None
        if "bpm_engine" in tags and tags["bpm_engine"]:
            engine = str(tags["bpm_engine"][0]).strip().lower()
    except Exception:
        pass

    return bpm, engine


def compute_bpm_essentia(path: Path) -> float | None:
    """Computes BPM using Essentia (much faster and more accurate)."""
    if not HAS_ESSENTIA:
        return None
    try:
        loader = es.MonoLoader(filename=str(path))
        audio = loader()

        rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
        bpm, beats, beats_confidence, _, _ = rhythm_extractor(audio)

        return round(float(bpm), 1)
    except Exception as e:
        print(f"  [ERROR Essentia] Could not analyze {path.name}: {e}", file=sys.stderr)
        return None


def compute_bpm_librosa(path: Path) -> float | None:
    """Analyzes the audio with librosa and returns the estimated BPM."""
    try:
        y, sr = librosa.load(str(path), sr=22050, mono=True, duration=120)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo) if np.isscalar(tempo) else float(tempo[0])
        return round(bpm, 1)
    except Exception as e:
        print(f"  [ERROR Librosa] Could not analyze {path.name}: {e}", file=sys.stderr)
        return None


def compute_bpm(path: Path) -> tuple[float | None, str | None]:
    """
    Tries Essentia first. If unavailable or fails, falls back to Librosa.
    Returns (bpm, engine_name).
    """
    bpm = compute_bpm_essentia(path)
    if bpm is not None:
        return bpm, "essentia"

    bpm = compute_bpm_librosa(path)
    if bpm is not None:
        return bpm, "librosa"

    return None, None


def write_bpm(path: Path, bpm: float, engine: str) -> bool:
    """
    Writes the BPM to the TBPM tag and saves the engine ('essentia'/'librosa')
    in the custom BPM_ENGINE ID3 frame via EasyID3.
    """
    try:
        bpm_str = str(round(bpm))

        try:
            audio_tags = EasyID3(path)
        except error:
            audio_mp3 = MP3(path)
            audio_mp3.add_tags()
            audio_mp3.save()
            audio_tags = EasyID3(path)

        audio_tags['bpm'] = bpm_str
        audio_tags['bpm_engine'] = engine
        audio_tags.save()
        return True
    except Exception as e:
        print(f"  [ERROR] Could not write tag to {path.name}: {e}", file=sys.stderr)
        return False


def process_file(args_tuple):
    """
    Helper function for parallelization: analyzes and optionally writes.
    Also handles re-checking files marked with engine='librosa'.
    """
    path, force, dry_run = args_tuple

    existing_bpm, existing_engine = get_existing_metadata(path)

    # 1. Si ya tiene BPM, no es forzado y NO está marcado como Librosa, omitir.
    if existing_bpm is not None and existing_bpm >= 1 and not force and existing_engine != "librosa":
        return (path, existing_bpm, existing_engine or "unknown", "skipped")

    # 2. Si estaba marcado como 'librosa', intentamos re-procesar con Essentia si está disponible
    if existing_engine == "librosa" and not force:
        if HAS_ESSENTIA:
            new_bpm = compute_bpm_essentia(path)
            if new_bpm is not None:
                if dry_run:
                    return (path, new_bpm, "essentia", "reprocessed_dry")
                if write_bpm(path, new_bpm, "essentia"):
                    return (path, new_bpm, "essentia", "reprocessed")
                else:
                    return (path, new_bpm, "essentia", "write_failed")
            else:
                # Essentia volvió a fallar o no dio resultado; mantenemos el de Librosa
                return (path, existing_bpm, "librosa", "reprocess_failed")
        else:
            # Sin Essentia disponible no podemos re-evaluarlo mejor
            return (path, existing_bpm, "librosa", "skipped")

    # 3. Cálculo estándar si no tenía tag o si force=True
    computed_bpm, engine_used = compute_bpm(path)
    if computed_bpm is None:
        return (path, None, None, "failed")

    if dry_run:
        return (path, computed_bpm, engine_used, "dry-run")

    if write_bpm(path, computed_bpm, engine_used):
        return (path, computed_bpm, engine_used, "written")
    else:
        return (path, computed_bpm, engine_used, "write_failed")


def find_files(folder: Path, exclude: list[str] | None = None) -> list[Path]:
    files = []
    exclude = exclude or []
    for path in folder.rglob("*"):
        if path.suffix.lower() in EXTENSIONS and path.is_file():
            if any(pattern.lower() in str(path).lower() for pattern in exclude):
                continue
            files.append(path)
    return sorted(files)


def main(source: str, jobs: int = 1, force: bool = False,
         dry_run: bool = False, exclude: list[str] | None = None) -> dict:
    """
    Main execution loop.
    Returns a summary dictionary with counts per status.
    """
    exclude = exclude or []

    folder_path = Path(source).expanduser().resolve()
    if not folder_path.is_dir():
        print(f"Error: '{folder_path}' is not a valid folder.", file=sys.stderr)
        sys.exit(1)

    print(f"Searching for MP3 files in: {folder_path}")
    if HAS_ESSENTIA:
        print("Engine status: Essentia ACTIVE (will re-process any 'librosa' tagged files)")
    else:
        print("Engine status: Essentia NOT AVAILABLE (falling back to Librosa)")

    files = find_files(folder_path, exclude)
    print(f"Found {len(files)} files.\n")

    summary = {
        "written": 0,
        "reprocessed": 0,
        "skipped": 0,
        "failed": 0,
        "reprocess_failed": 0,
        "write_failed": 0,
        "dry-run": 0,
        "reprocessed_dry": 0
    }

    if not files:
        print("Nothing to process.")
        return summary

    tasks = [(path, force, dry_run) for path in files]

    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            futures = {executor.submit(process_file, t): t[0] for t in tasks}
            for future in as_completed(futures):
                path, bpm, engine, status = future.result()
                _print_result(path, folder_path, bpm, engine, status)
                summary[status] = summary.get(status, 0) + 1
    else:
        for task in tasks:
            path, bpm, engine, status = process_file(task)
            _print_result(path, folder_path, bpm, engine, status)
            summary[status] = summary.get(status, 0) + 1

    print("\n--- Summary ---")
    for status, count in summary.items():
        if count > 0:
            print(f"  {status}: {count}")

    return summary


def cli():
    """Entry point when the script is run directly from the terminal."""
    parser = argparse.ArgumentParser(
        description="Computes and writes BPMs into MP3 files recursively, marking the algorithm used."
    )
    parser.add_argument(
        "folder",
        type=str,
        nargs="?",
        default=DEFAULT_MUSIC_PATH,
        help="Root folder to process (default: MUSIC_PATH from .env)"
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Recalculate and overwrite even if the file already has a BPM"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show the computed BPMs, don't write anything"
    )
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=1,
        help="Number of parallel processes (default: 1)"
    )
    parser.add_argument(
        "-e", "--exclude",
        action="append",
        default=[],
        help="Path pattern to exclude (can be repeated)"
    )
    args = parser.parse_args()

    if not args.folder:
        print(
            "Error: no folder given and MUSIC_PATH is not set in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    main(
        source=args.folder,
        jobs=args.jobs,
        force=args.force,
        dry_run=args.dry_run,
        exclude=args.exclude,
    )


def _print_result(path: Path, base: Path, bpm, engine: str | None, status: str):
    rel = path.relative_to(base)
    eng_tag = f"[{engine.upper()}]" if engine else ""
    
    if status == "skipped":
        print(f"[SKIP]  {rel}  (already has BPM={bpm} {eng_tag})")
    elif status == "written":
        print(f"[OK]    {rel}  -> BPM={bpm} {eng_tag}")
    elif status == "reprocessed":
        print(f"[RE-OK] {rel}  -> Re-processed from Librosa to Essentia BPM={bpm}")
    elif status == "reprocessed_dry":
        print(f"[RE-DRY]{rel}  -> Would update from Librosa to Essentia BPM={bpm}")
    elif status == "reprocess_failed":
        print(f"[RE-FAIL]{rel} -> Essentia failed, keeping Librosa BPM={bpm}")
    elif status == "dry-run":
        print(f"[DRY]   {rel}  -> BPM={bpm} {eng_tag} (not written)")
    elif status in ("failed", "write_failed"):
        print(f"[FAIL]  {rel}")


if __name__ == "__main__":
    cli()