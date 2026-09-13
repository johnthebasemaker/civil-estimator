"""Reuse of saved extractions.

The pipeline has always written `output/<drawing>_extraction.json` after every
run, and never once read one back. Re-opening a drawing extracted last week paid
the full model cost again — minutes of GPU time and, on a laptop, minutes of
fan. For a twenty-drawing set reviewed over several sittings that is most of the
time spent.

The cache is content-addressed. The key is a hash of the PDF's bytes plus the
profile, so:

* a **copy** of a drawing into another folder is the same job and hits the
  cache, where a name-and-timestamp key would have missed;
* a **reissue** under the same filename is a different key and is re-read,
  where a name key would have served last revision's numbers with no warning.

Both mistakes are the kind that end up in a priced BOQ, so the extra hashing is
cheap at the price.

Nothing here decides *whether* to use the cache. The caller passes `force` when
the user asks for a fresh read, and the worker records which jobs were served
from cache so the queue can say so out loud.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from extractors.models import ExtractionResult

# Overridable for the same reason the queue path is: the app and the worker are
# separate processes on a server, and both must agree where results live.
CACHE_ENV = "CIVIL_ESTIMATOR_CACHE_DIR"
CACHE_DIR = Path("output/cache")
OUTPUT_DIR = Path("output")


def default_cache_dir() -> Path:
    return Path(os.environ.get(CACHE_ENV) or CACHE_DIR)


def cache_path(fingerprint: str, cache_dir: Path | None = None) -> Path:
    base = Path(cache_dir) if cache_dir else default_cache_dir()
    return base / f"{fingerprint}.json"


def load(fingerprint: str, *, cache_dir: Path | None = None) -> ExtractionResult | None:
    """A previous extraction of exactly this file, or None."""
    path = cache_path(fingerprint, cache_dir)
    if not path.is_file():
        return None
    try:
        return ExtractionResult(**json.loads(path.read_text()))
    except Exception:                                    # noqa: BLE001
        # A truncated or superseded cache entry is not worth a crash: the honest
        # fallback is to extract again, which is exactly what None asks for.
        return None


def store(fingerprint: str, result: ExtractionResult, *,
          cache_dir: Path | None = None,
          also_named: Path | None = None) -> Path:
    """Save an extraction under its fingerprint.

    `also_named` keeps the human-readable `<drawing>_extraction.json` that the
    rest of the tooling globs for — `bin/rebuild_set.py` reads that directory,
    and a folder of hex filenames would be useless to open by hand.
    """
    path = cache_path(fingerprint, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.model_dump_json(indent=2)
    path.write_text(payload)
    if also_named is not None:
        also_named.parent.mkdir(parents=True, exist_ok=True)
        also_named.write_text(payload)
    return path


def backfill(drawings_dirs=("Drawings", "output/uploads", "."), *,
             cache_dir: Path | None = None, output_dir: Path | None = None,
             profile: str = "thorough") -> dict:
    """Index extractions that were saved before the cache existed.

    Matching is by filename stem, then verified by re-hashing the PDF — so an
    old JSON is only adopted if the drawing it names is still byte-for-byte the
    one on disk. A stem whose PDF has since been reissued is skipped rather than
    wired to the wrong numbers.
    """
    from core.jobstore import fingerprint as fp

    out_dir = Path(output_dir or OUTPUT_DIR)
    pdfs: dict[str, Path] = {}
    for folder in drawings_dirs:
        for pdf in sorted(Path(folder).glob("*.pdf")):
            pdfs.setdefault(pdf.stem, pdf)

    linked, skipped = 0, 0
    for saved in sorted(out_dir.glob("*_extraction.json")):
        stem = saved.name[: -len("_extraction.json")]
        pdf = pdfs.get(stem)
        if pdf is None or not pdf.is_file():
            skipped += 1
            continue
        target = cache_path(fp(pdf, profile), cache_dir)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(saved, target)
        linked += 1
    return {"linked": linked, "skipped": skipped, "pdfs_seen": len(pdfs)}


def stats(*, cache_dir: Path | None = None) -> dict:
    folder = Path(cache_dir) if cache_dir else default_cache_dir()
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    return {"entries": len(files),
            "bytes": sum(f.stat().st_size for f in files)}
