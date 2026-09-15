"""Where the sample drawing lives.

The drawing used to sit in the repository root and be committed alongside the
code. It is client material, so it is now gitignored and kept in `Drawings/`
with the rest of the set. Four test modules hardcoded the old root path, and
because they guard with `skipif(not SAMPLE_PDF.exists())` they did not fail
when it went away — they quietly stopped running 51 tests, which is the worst
way for coverage to disappear.

So look in every place a copy legitimately turns up. The fallback is the root
path rather than None, so a caller's `.exists()` check still reads false and
skips honestly when there really is no drawing to test against.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "MD-522-8110-EG-CV-LAD-0107_C01.pdf"
SEARCH_DIRS = (ROOT, ROOT / "Drawings", ROOT / "output" / "uploads")


def sample_pdf(name: str = NAME) -> Path:
    """The named drawing, wherever it is sitting."""
    for base in SEARCH_DIRS:
        candidate = base / name
        if candidate.exists():
            return candidate
    return ROOT / name
