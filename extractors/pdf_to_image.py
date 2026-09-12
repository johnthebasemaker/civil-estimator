"""PDF -> PNG rasteriser for the vision extractor.

Qwen2.5-VL is an image model: the PDF never goes to the model, only PNGs do.
This module owns every pixel decision.

Three things an engineering drawing needs that a generic rasteriser does not:

1. **Rotation awareness.** A0 sheets are almost always stored portrait with a
   /Rotate 270 entry (this one is: mediabox 2384x3370, rotation 270). PyMuPDF's
   `page.rect` already reports the *displayed* box, so every region fraction in
   this codebase is expressed against the sheet as a human reads it. Never use
   `mediabox` for region maths.

2. **Region cropping, in page fractions.** Title blocks are dense; sending the
   whole A0 wastes the model's resolution budget on plan geometry. Crops are
   specified as fractions so an A1 or A3 reissue of the same drawing crops to
   the same place.

3. **Constant effective resolution.** What matters for reading a 3 mm-tall
   callout is not the render DPI but *pixels per sheet-millimetre*. A tile
   covering 40% of the sheet width rendered to 2000 px has the same effective
   resolution as a full-sheet render at 5000 px — which is how a 7b model reads
   small callouts it would otherwise miss on a downsampled full sheet.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

from extractors.models import Region

# ---------- Region map (Maaden A0 border, ISO 7200 layout) ----------
# Fractions of the displayed sheet. Verified against
# MD-522-8110-EG-CV-LAD-0107 Rev C01.
TITLE_BLOCK = Region(name="title_block", x0=0.785, y0=0.845, x1=1.0, y1=1.0)

# The foundation layout plan sits top-left on this sheet family. This is the
# one region that is drawing-specific rather than border-specific; override it
# via extract_from_pdf(plan_region=...) for a different layout.
PLAN = Region(name="plan", x0=0.0, y0=0.0, x1=0.50, y1=0.38)

# Right-hand margin strip: key plan, general notes, hold list.
GENERAL_NOTES = Region(name="general_notes", x0=0.855, y0=0.10, x1=1.0, y1=0.62)


@dataclass
class RenderedImage:
    """A PNG ready to hand to the model, plus the numbers needed to reason
    about whether it is worth sending at all."""
    region: Region
    png: bytes
    width: int
    height: int
    ink_ratio: float          # fraction of non-white pixels; 0.0 == blank paper

    @property
    def b64(self) -> str:
        return base64.b64encode(self.png).decode("ascii")

    @property
    def kb(self) -> float:
        return len(self.png) / 1024.0


# ---------- Page access ----------
def open_page(pdf_path: str | Path, page_number: int = 0) -> tuple[fitz.Document, fitz.Page]:
    """Open a PDF and return (doc, page). Caller owns closing the doc."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    doc = fitz.open(pdf_path)
    if page_number < 0 or page_number >= doc.page_count:
        doc.close()
        raise IndexError(f"page {page_number} out of range (PDF has {doc.page_count})")
    return doc, doc[page_number]


def page_info(page: fitz.Page) -> dict:
    """Sheet metrics an engineer would want in the log."""
    r = page.rect
    return {
        "width_pt": round(r.width, 1),
        "height_pt": round(r.height, 1),
        "width_mm": round(r.width / 72 * 25.4),
        "height_mm": round(r.height / 72 * 25.4),
        "rotation": page.rotation,
        "orientation": "landscape" if r.width >= r.height else "portrait",
        "sheet_size": _iso_sheet_name(r.width / 72 * 25.4, r.height / 72 * 25.4),
        "has_text_layer": bool(page.get_text().strip()),
        "vector_paths": len(page.get_drawings()),
    }


_ISO_SIZES = {
    "A0": (1189, 841), "A1": (841, 594), "A2": (594, 420),
    "A3": (420, 297), "A4": (297, 210),
}


def _iso_sheet_name(w_mm: float, h_mm: float, tol: float = 12.0) -> str:
    lo, hi = sorted((w_mm, h_mm))
    for name, (nom_long, nom_short) in _ISO_SIZES.items():
        if abs(hi - nom_long) <= tol and abs(lo - nom_short) <= tol:
            return name
    return f"{round(hi)}x{round(lo)}mm"


# ---------- Rendering ----------
def render_region(page: fitz.Page, region: Region,
                  longest_edge_px: int = 2000,
                  max_pixels: int | None = None) -> RenderedImage:
    """Rasterise one normalised region to a PNG.

    Two independent caps, whichever binds first:

    * `longest_edge_px` — the familiar "no bigger than N px on a side".
    * `max_pixels` — a *total area* budget, and the one that actually matters.

    Inference cost on a vision model tracks the number of image patches, i.e.
    total pixels, not the longest edge. Sizing a near-square tile by its longest
    edge silently produces ~4x the pixels of a wide strip sized the same way.
    Measured on this sheet with qwen2.5vl:7b:

        1.0 MP  ->   51 s
        2.8 MP  ->  346 s

    so the area cap is what keeps a tile sweep affordable. Pass `max_pixels` for
    anything that will be sent to the model.
    """
    if region.width <= 0 or region.height <= 0:
        raise ValueError(f"region {region.name!r} has zero area")

    r = page.rect
    clip = fitz.Rect(
        r.x0 + region.x0 * r.width, r.y0 + region.y0 * r.height,
        r.x0 + region.x1 * r.width, r.y0 + region.y1 * r.height,
    )
    zoom = longest_edge_px / max(clip.width, clip.height)
    if max_pixels:
        area_pt = clip.width * clip.height
        if area_pt > 0:
            zoom = min(zoom, (max_pixels / area_pt) ** 0.5)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    return RenderedImage(
        region=region, png=pix.tobytes("png"),
        width=pix.width, height=pix.height,
        ink_ratio=_ink_ratio(page, clip),
    )


def render_full(page: fitz.Page, longest_edge_px: int = 2000) -> RenderedImage:
    """Rasterise the whole sheet (used by the `fast` profile and the UI preview)."""
    return render_region(page, Region(name="full_sheet", x0=0, y0=0, x1=1, y1=1),
                         longest_edge_px)


def _ink_ratio(page: fitz.Page, clip: fitz.Rect, probe_px: int = 160) -> float:
    """Fraction of non-white pixels in a cheap low-res probe render.

    Used to skip blank tiles: on a sheet where the drawing hugs one side, a full
    grid sweep would otherwise burn ~50 s of model time reading empty paper.
    """
    zoom = probe_px / max(clip.width, clip.height)
    probe = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip,
                            colorspace=fitz.csGRAY, alpha=False)
    data = probe.samples
    if not data:
        return 0.0
    inked = sum(1 for b in data if b < 245)
    return inked / len(data)


# ---------- Tiling ----------
def tile_regions(cols: int = 3, rows: int = 2, overlap: float = 0.06,
                 bounds: Region | None = None) -> list[Region]:
    """Build an overlapping grid of regions covering `bounds` (default: sheet).

    Overlap matters: a callout that straddles a tile seam is otherwise cut in
    half and read by neither call. 6% of a tile on an A0 is ~40 mm of paper,
    comfortably wider than any single callout line. The cost of overlap is
    duplicate hits, which `qwen_vision` reconciles by tag.
    """
    if cols < 1 or rows < 1:
        raise ValueError("cols and rows must be >= 1")
    if not 0.0 <= overlap < 0.5:
        raise ValueError("overlap must be in [0, 0.5)")

    b = bounds or Region(name="sheet", x0=0.0, y0=0.0, x1=1.0, y1=1.0)
    tw, th = b.width / cols, b.height / rows
    ox, oy = tw * overlap, th * overlap

    out: list[Region] = []
    for row in range(rows):
        for col in range(cols):
            x0 = max(b.x0, b.x0 + col * tw - ox)
            y0 = max(b.y0, b.y0 + row * th - oy)
            x1 = min(b.x1, b.x0 + (col + 1) * tw + ox)
            y1 = min(b.y1, b.y0 + (row + 1) * th + oy)
            out.append(Region(name=f"tile_r{row + 1}c{col + 1}",
                              x0=x0, y0=y0, x1=x1, y1=y1))
    return out


def effective_sheet_px(region: Region, longest_edge_px: int,
                       page_aspect: float = 1.414,
                       max_pixels: int | None = None) -> int:
    """Full-sheet-equivalent pixel width this crop achieves.

    The number to reason about when choosing a grid: reading a 3 mm callout
    needs pixels per sheet-millimetre, and a tile covering 25% of the width at
    1100 px reads like a 4400 px render of the whole sheet. Measured floor for
    Maaden callouts with qwen2.5vl:7b is ~4400; below ~3000 the text is gone.
    """
    if region.width <= 0 or region.height <= 0:
        return 0
    span = max(region.width, region.height / page_aspect)
    px = longest_edge_px / span
    if max_pixels:
        # Sheet-equivalent width implied by the area budget.
        area_frac = region.width * (region.height / page_aspect)
        px = min(px, (max_pixels / area_frac) ** 0.5)
    return int(px)


# ---------- Perceptual hash (RAG retrieval key) ----------
def sheet_hash(page: fitz.Page) -> str:
    """64-bit dHash of the whole sheet, as 16 hex chars.

    Difference hash rather than pHash: no numpy/scipy dependency, and it is
    exactly as good at the only job here — "is this the same drawing I have a
    verified example for?" Robust to render scale and mild rescanning, which is
    what distinguishes drawing revisions in practice.
    """
    w, h = 9, 8
    r = page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(w / r.width, h / r.height),
                          colorspace=fitz.csGRAY, alpha=False)
    # Guard: PyMuPDF may round to w+-1 px.
    pw, ph, s = pix.width, pix.height, pix.samples
    bits = []
    for y in range(min(h, ph)):
        for x in range(min(w, pw) - 1):
            left = s[y * pw + x]
            right = s[y * pw + x + 1]
            bits.append("1" if left > right else "0")
    bits = (bits + ["0"] * 64)[:64]
    return f"{int(''.join(bits), 2):016x}"


def hamming(a: str, b: str) -> int:
    """Hamming distance between two hex dHashes; 0 == identical sheet."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return 64


# ---------- Convenience ----------
def png_to_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def save_debug(img: RenderedImage, out_dir: str | Path) -> Path:
    """Dump a rendered region to disk — the fastest way to see what the model saw."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{img.region.name}.png"
    path.write_bytes(img.png)
    return path
