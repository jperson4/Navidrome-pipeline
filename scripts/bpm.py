#!/usr/bin/env python3
"""
bpm_tagger.py

Recursively walks a folder, computes the BPM of each MP3 file
using librosa, and writes the result to the ID3 TBPM tag (mutagen).

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
import essentia as es
from mutagen.id3 import ID3, TBPM
from mutagen.mp3 import MP3
from dotenv import load_dotenv

load_dotenv()

warnings.filterwarnings("ignore")  # librosa/numba throw noisy warnings

EXTENSIONS = {".mp3"}
DEFAULT_MUSIC_PATH = os.getenv("MUSIC_PATH")


def get_existing_bpm(path: Path) -> float | None:
    """Reads the TBPM tag if present. Returns None if no tag or read fails."""
    try:
        tags = ID3(path)
        if "TBPM" in tags:
            value = str(tags["TBPM"].text[0]).strip()
            return float(value) if value else None
    except Exception:
        return None
    return None


def compute_bpm_librosa(path: Path) -> float | None:
    """Analyzes the audio with librosa and returns the estimated BPM."""
    try:
        print(f"Computing BPM with Librosa for {path.name}...")
        # y, sr = librosa.load(str(path), sr=None, mono=True) # inefficient
        y, sr = librosa.load(str(path), sr=22050, mono=True, duration=120)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo) if np.isscalar(tempo) else float(tempo[0])
        return round(bpm, 1)
    except Exception as e:
        print(f"  [ERROR] Could not analyze {path.name}: {e}", file=sys.stderr)
        return None

def compute_bpm_essentia(path: Path) -> float | None:
    """Computes BPM using Essentia (much faster than librosa)."""
    try:
        print(f"Computing BPM with Essentia for {path.name}...")
        loader = es.MonoLoader(filename=str(path))
        audio = loader()

        rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
        bpm, beats, beats_confidence, _, _ = rhythm_extractor(audio)

        return round(float(bpm), 1)

    except Exception as e:
        print(f"  [ERROR] Could not analyze {path.name}: {e}", file=sys.stderr)
        return None

def write_bpm(path: Path, bpm: float) -> bool:
    """Writes the BPM to the TBPM tag of the MP3. Creates the ID3 tag if it doesn't exist."""
    try:
        try:
            tags = ID3(path)
        except Exception:
            # No ID3 header yet, create one
            audio = MP3(path)
            audio.add_tags()
            tags = audio.tags
        tags.delall("TBPM")
        tags.add(TBPM(encoding=3, text=str(round(bpm))))
        tags.save(path)
        return True
    except Exception as e:
        print(f"  [ERROR] Could not write tag to {path.name}: {e}", file=sys.stderr)
        return False


def process_file(args_tuple, bpm_fn=compute_bpm_essentia):
    """Helper function for parallelization: analyzes and optionally writes."""
    path, force, dry_run = args_tuple

    existing_bpm = get_existing_bpm(path)
    if (existing_bpm is not None and existing_bpm >= 1) and not force:
        return (path, existing_bpm, "skipped")

    computed_bpm = bpm_fn(path)
    if computed_bpm is None:
        return (path, None, "failed")

    if dry_run:
        return (path, computed_bpm, "dry-run")

    if write_bpm(path, computed_bpm):
        return (path, computed_bpm, "written")
    else:
        return (path, computed_bpm, "write_failed")


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
    Reusable entry point: can be imported and called directly
    from another Python script, e.g.

        import bpm_tagger
        bpm_tagger.main(folder=str(source_abs), jobs=args.threads)

    Returns a summary dictionary with the count per status
    (written / skipped / failed / write_failed / dry-run).
    """
    exclude = exclude or []

    folder_path = Path(source).expanduser().resolve()
    if not folder_path.is_dir():
        print(f"Error: '{folder_path}' is not a valid folder.", file=sys.stderr)
        sys.exit(1)

    print(f"Searching for MP3 files in: {folder_path}")
    files = find_files(folder_path, exclude)
    print(f"Found {len(files)} files.\n")

    summary = {"written": 0, "skipped": 0, "failed": 0, "write_failed": 0, "dry-run": 0}

    if not files:
        print("Nothing to process.")
        return summary

    tasks = [(path, force, dry_run) for path in files]

    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            futures = {executor.submit(process_file, t): t[0] for t in tasks}
            for future in as_completed(futures):
                path, bpm, status = future.result()
                _print_result(path, folder_path, bpm, status)
                summary[status] = summary.get(status, 0) + 1
    else:
        for task in tasks:
            path, bpm, status = process_file(task)
            _print_result(path, folder_path, bpm, status)
            summary[status] = summary.get(status, 0) + 1

    print("\n--- Summary ---")
    for status, count in summary.items():
        if count > 0:
            print(f"  {status}: {count}")

    return summary


def cli():
    """Entry point when the script is run directly from the terminal."""
    parser = argparse.ArgumentParser(
        description="Computes and writes BPMs into MP3 files recursively."
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


def _print_result(path: Path, base: Path, bpm, status: str):
    rel = path.relative_to(base)
    if status == "skipped":
        print(f"[SKIP]  {rel}  (already has BPM={bpm})")
    elif status == "written":
        print(f"[OK]    {rel}  -> BPM={bpm}")
    elif status == "dry-run":
        print(f"[DRY]   {rel}  -> BPM={bpm} (not written)")
    elif status in ("failed", "write_failed"):
        print(f"[FAIL]  {rel}")


if __name__ == "__main__":
    cli()