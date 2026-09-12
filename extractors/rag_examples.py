"""JSON-on-disk example library with perceptual-hash retrieval.

Handoff §5 stage 3: "Simplest RAG store: JSON files on disk, no vector DB
needed for <100 examples." That is the right call — retrieval here is not
semantic search, it is "have I seen this sheet, or a sibling from the same
drawing series, before?". Two cheap signals answer that better than embeddings:

1. **dHash distance** on the rendered sheet — near-zero for a reissue of the
   same drawing at a new revision, small for sheets sharing a border template.
2. **Drawing-number prefix** — Maaden numbers are structured
   (`MD-522-8110-EG-CV-LAD-0107`): project-area-discipline-type-serial. Sheets
   sharing the first four groups share callout conventions.

Retrieved examples are injected as few-shot text into the Stage 2 prompt.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from extractors.pdf_to_image import hamming

EXAMPLES_DIR = Path(__file__).parent / "examples"

# dHash distance under which two sheets are treated as the same drawing family.
# 0 = pixel-identical thumbnail; ~10/64 bits still means "same border, same
# layout"; beyond ~18 the sheets look nothing alike.
SAME_DRAWING_DISTANCE = 6
SAME_FAMILY_DISTANCE = 18


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", (text or "unknown").strip()) or "unknown"


def _prefix(drawing_no: str, groups: int = 4) -> str:
    """First N hyphen groups of a drawing number, e.g. MD-522-8110-EG."""
    return "-".join((drawing_no or "").split("-")[:groups]).upper()


# ---------- Write ----------
def save_example(*, drawing_no: str, revision: str, image_hash: str,
                 verified_extraction: dict[str, Any], verified_by: str = "",
                 source_pdf: str = "", notes: str = "",
                 examples_dir: Path | None = None) -> Path:
    """Persist a human-verified extraction as a few-shot example.

    Called only after the user has reviewed and corrected the extraction — an
    unverified example poisons every later run, so this is never automatic.
    """
    d = Path(examples_dir or EXAMPLES_DIR)
    d.mkdir(parents=True, exist_ok=True)
    stem = _slug(drawing_no or image_hash or "example")
    if revision:
        stem = f"{stem}_{_slug(revision)}"
    path = d / f"{stem}.json"
    path.write_text(json.dumps({
        "drawing_no": drawing_no,
        "revision": revision,
        "image_hash": image_hash,
        "verified_extraction": verified_extraction,
        "verified_by": verified_by,
        "verified_at": date.today().isoformat(),
        "source_pdf": source_pdf,
        "notes": notes,
    }, indent=2), encoding="utf-8")
    return path


# ---------- Read ----------
def load_examples(examples_dir: Path | None = None) -> list[dict]:
    d = Path(examples_dir or EXAMPLES_DIR)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("verified_extraction"):
            data["_path"] = str(p)
            out.append(data)
    return out


def find_similar(image_hash: str, drawing_no: str = "", *, k: int = 2,
                 examples_dir: Path | None = None,
                 exclude_drawing_no: str = "") -> list[dict]:
    """Closest verified examples, best first.

    Ranking: same-drawing hash matches, then same-number-prefix siblings, then
    same-family hash neighbours. Anything further away is dropped — a bad
    few-shot example is worse than none, because the model will happily copy
    pedestal tags out of it.
    """
    scored: list[tuple[int, int, dict]] = []
    for ex in load_examples(examples_dir):
        if exclude_drawing_no and ex.get("drawing_no", "").upper() == exclude_drawing_no.upper():
            continue
        dist = hamming(image_hash, ex.get("image_hash", "")) if image_hash else 64
        same_prefix = bool(drawing_no) and _prefix(drawing_no) == _prefix(ex.get("drawing_no", ""))

        if dist <= SAME_DRAWING_DISTANCE:
            tier = 0
        elif same_prefix:
            tier = 1
        elif dist <= SAME_FAMILY_DISTANCE:
            tier = 2
        else:
            continue
        scored.append((tier, dist, ex))

    scored.sort(key=lambda t: (t[0], t[1]))
    return [ex for _, _, ex in scored[:k]]


def library_stats(examples_dir: Path | None = None) -> dict:
    """Progress against handoff §10: 'example library reaches 10+ verified drawings'."""
    examples = load_examples(examples_dir)
    drawings = {e.get("drawing_no", "") for e in examples if e.get("drawing_no")}
    pedestals = sum(len(e.get("verified_extraction", {}).get("pedestals", []))
                    for e in examples)
    return {
        "files": len(examples),
        "distinct_drawings": len(drawings),
        "verified_pedestals": pedestals,
        "target": 10,
        "target_met": len(drawings) >= 10,
        "dir": str(Path(examples_dir or EXAMPLES_DIR)),
    }
