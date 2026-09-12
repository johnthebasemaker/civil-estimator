#!/usr/bin/env python
"""Generate web-usable logo assets from the master artwork.

The master is a 12 MB transparent TIFF. Browsers cannot render TIFF at all, and
even if they could, shipping 12 MB into a sidebar on every page load is absurd.
This produces three PNGs sized for where they are actually used, and is
re-runnable whenever the artwork changes.

    venv/bin/python bin/build_assets.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "Logo" / "GI_Logo.tiff"
OUT = ROOT / "assets"

# (filename, width in px) — heights follow the master's aspect ratio.
TARGETS = [
    ("gi_logo_sidebar.png", 260),   # sidebar mark
    ("gi_logo_login.png", 420),     # login card
    ("gi_logo_header.png", 150),    # inline beside the page title
]
FAVICON = ("gi_favicon.png", 64)


def build() -> list[Path]:
    if not MASTER.exists():
        raise SystemExit(f"Master artwork not found: {MASTER}")
    OUT.mkdir(parents=True, exist_ok=True)

    master = Image.open(MASTER)
    if master.mode != "RGBA":
        master = master.convert("RGBA")

    written = []
    for name, width in TARGETS + [FAVICON]:
        height = max(1, round(master.height * width / master.width))
        # Transparency is kept: the logo has to sit on both the white sidebar
        # and the tinted login card without a white box around it.
        img = master.resize((width, height), Image.LANCZOS)
        path = OUT / name
        img.save(path, format="PNG", optimize=True)
        written.append(path)
    return written


if __name__ == "__main__":
    for p in build():
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size / 1024:.0f} KB")
