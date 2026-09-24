"""Artefacts for checking an extraction against the drawing.

An estimator handed a spreadsheet and an A0 has no way to check one against the
other except by hunting. These two artefacts close that gap:

* **Check print** — the sheet rendered with every extracted value boxed, tagged
  and quoted by grid square. Print it, sit it next to the drawing, tick boxes.
* **Verification sheet** — one row per extracted value with its verbatim
  callout, its grid reference, and blank columns for the correct value, who
  checked it and when. Signed off, it is also the record of *why* a number in
  the BOQ is what it is.

Neither invents anything: both only restate what was extracted, with its
provenance.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont

from extractors.models import ExtractionResult
from extractors.sheet_grid import DEFAULT_GRID, SheetGrid

# Colour by how much the value should be trusted, not by element type — the
# checker's question is "which of these is most likely wrong?".
COLOUR_HIGH = (0, 140, 60)
COLOUR_MEDIUM = (200, 110, 0)
COLOUR_LOW = (200, 30, 30)
COLOUR_DERIVED = (90, 90, 200)
_CONF_COLOUR = {"high": COLOUR_HIGH, "medium": COLOUR_MEDIUM, "low": COLOUR_LOW}


@dataclass
class CheckItem:
    """One thing a checker has to confirm against the drawing."""
    kind: str                 # "Pedestal", "Grade slab", "Title block", ...
    label: str                # "P1"
    extracted: str            # "600 x 500 mm, 2 Nos"
    callout: str              # verbatim text read off the sheet
    grid_ref: str
    confidence: str
    rect_norm: list[float]
    note: str = ""


def collect_items(result: ExtractionResult,
                  grid: SheetGrid = DEFAULT_GRID) -> list[CheckItem]:
    """Everything from an extraction that a person should verify."""
    items: list[CheckItem] = []
    tb = result.title_block
    for field, label in (("drawing_no", "Drawing no"), ("revision", "Revision"),
                         ("date", "Date"), ("project_name", "Project"),
                         ("prepared_by", "Drawn by")):
        value = getattr(tb, field, "")
        if value:
            items.append(CheckItem(
                kind="Title block", label=label, extracted=str(value),
                callout=tb.date_raw if field == "date" else "",
                grid_ref="", confidence=tb.confidence, rect_norm=[]))

    for p in result.pedestals:
        height = (f"{p.height_mm:g} mm" if p.height_mm
                  else "height NOT on drawing — enter manually")
        items.append(CheckItem(
            kind="Pedestal", label=p.tag,
            extracted=f"{p.length_mm:g} x {p.width_mm:g} mm, {p.quantity} Nos",
            callout=p.raw_text, grid_ref=p.grid_ref, confidence=p.confidence,
            rect_norm=list(p.source_rect or []), note=height))

    for s in result.grade_slabs:
        if not s.is_usable():
            continue
        items.append(CheckItem(
            kind="Grade slab", label=s.tag,
            extracted=f"{s.length_mm:g} x {s.width_mm:g} x {s.thickness_mm:g} mm",
            callout=s.raw_text, grid_ref="", confidence=s.confidence,
            rect_norm=[],
            note="read off the plan view, not a callout — check the dimension string"))

    for mark, count in (getattr(result, "position_marks", None) or {}).items():
        items.append(CheckItem(
            kind="Position marks", label=mark, extracted=f"{count} printed",
            callout="", grid_ref="", confidence="low", rect_norm=[],
            note="Count of marks printed on the sheet — an element drawn in "
                 "both plan and section is labelled twice, so confirm the real "
                 "quantity. NOT used as a BOQ quantity."))

    labels = {"curb_walls": "Curb wall", "sumps": "Sump", "levels": "Level",
              "insert_plates": "Insert plate", "epoxy": "Epoxy",
              "rebar": "Rebar", "thicknesses": "Thickness"}
    for key, kind in labels.items():
        for row in (result.findings or {}).get(key, []):
            detail = ", ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}"
                               for k, v in row.items()
                               if k not in ("raw_text", "grid_ref", "source_rect")
                               and v is not None)
            items.append(CheckItem(
                kind=kind, label="", extracted=detail,
                callout=row.get("raw_text", ""), grid_ref=row.get("grid_ref", ""),
                confidence="medium", rect_norm=list(row.get("source_rect") or []),
                note="not added to the BOQ — incomplete on the drawing"))
    return items


# ---------- Check print ----------
def build_check_print(page: fitz.Page, result: ExtractionResult, *,
                      out_path: str | Path,
                      grid: SheetGrid = DEFAULT_GRID,
                      width_px: int = 4000) -> Path:
    """Render the sheet with each extracted value boxed and labelled.

    Rendered large (default 4000 px wide) because it is meant to be printed at
    A3 or bigger and marked up by hand.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    zoom = width_px / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    W, H = img.size

    # Fade the drawing so the annotations read on top of dense linework.
    img = Image.blend(img, Image.new("RGB", (W, H), "white"), 0.45)
    draw = ImageDraw.Draw(img)
    font = _load_font(int(W / 120))
    small = _load_font(int(W / 170))

    # Numbered by position in `collect_items`, which is how `verification_rows`
    # numbers the workbook's Verification sheet and how the app's evidence table
    # numbers its rows. Numbering only the boxed ones — as this did — made box
    # "1." on the print row #6 in the sheet, so the two artefacts a checker
    # holds side by side disagreed about which value was which.
    all_items = collect_items(result, grid)
    items = [i for i in all_items if i.rect_norm]
    for n, item in enumerate(all_items, start=1):
        if not item.rect_norm:
            continue
        colour = _CONF_COLOUR.get(item.confidence, COLOUR_MEDIUM)
        x0, y0, x1, y1 = item.rect_norm
        box = [x0 * W - 6, y0 * H - 6, x1 * W + 6, y1 * H + 6]
        draw.rectangle(box, outline=colour, width=max(3, int(W / 900)))
        tag = f"{n}. {item.kind} {item.label}".strip()
        tw = draw.textlength(tag, font=font)
        ty = max(0, box[1] - int(W / 90))
        draw.rectangle([box[0], ty, box[0] + tw + 14, ty + int(W / 95)], fill=colour)
        draw.text((box[0] + 7, ty + 2), tag, fill="white", font=font)
        if item.grid_ref:
            draw.text((box[0] + 7, box[3] + 4), item.grid_ref, fill=colour, font=small)

    _draw_legend(draw, W, H, len(items), result, font, small)
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def _draw_legend(draw, W, H, n_items, result: ExtractionResult, font, small) -> None:
    pad = int(W / 100)
    lines = [
        "EXTRACTION CHECK PRINT — verify each boxed value against the drawing",
        f"{Path(result.source_pdf).name}   ·   {result.model}   ·   "
        f"{n_items} located item(s)",
        "green = high confidence    orange = medium    red = low / conflicting",
        "Pedestal HEIGHTS are not printed on these callouts and are NOT extracted.",
    ]
    box_h = int(W / 95) * len(lines) + pad
    draw.rectangle([pad, pad, pad + int(W / 2.1), pad + box_h],
                   fill="white", outline=(0, 0, 0), width=3)
    y = pad + 6
    for i, text in enumerate(lines):
        draw.text((pad + 10, y), text, fill=(0, 0, 0),
                  font=font if i == 0 else small)
        y += int(W / 95)


def _load_font(size: int):
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


# ---------- Verification sheet rows ----------
VERIFY_HEADERS = ["#", "Item", "Tag", "Extracted value", "Verbatim callout",
                  "Grid ref", "Confidence", "Correct value (if wrong)",
                  "Verified by", "Date", "Notes"]


def verification_rows(result: ExtractionResult,
                      grid: SheetGrid = DEFAULT_GRID) -> list[list]:
    """Rows for the workbook's Verification sheet, blanks left for sign-off."""
    rows = []
    for n, item in enumerate(collect_items(result, grid), start=1):
        rows.append([n, item.kind, item.label, item.extracted, item.callout,
                     item.grid_ref, item.confidence, "", "", "", item.note])
    return rows
