"""The drawing viewer: where every extracted number came from.

An estimator's first question about a machine-read quantity is "where does that
come from on the sheet?", and the answer used to be a 400-pixel picture of an A0
drawing that nobody could read. This module answers it three ways at once:

* **The evidence overlay** — the sheet with every located value boxed, numbered
  and coloured by how it was obtained.
* **The trace** — pick a row and the sheet is *re-rendered from the PDF* around
  that box. Not a zoom into pixels: the region is drawn again from the vectors,
  so the deeper you go the sharper it gets, which is the opposite of what
  enlarging a screenshot does.
* **The numbers** — box 7 on the sheet is row 7 in the table and row 7 on the
  workbook's Verification sheet, so the three artefacts can be laid side by side.

**Why no interactive canvas.** A Plotly or custom-component viewer would add
mouse pan, zoom and clicking a box to select its row. It was not worth it here:
Streamlit's `AppTest` — which every one of this project's UI tests is built on —
cannot see a Plotly chart at all, so those interactions would be unverifiable
and would rot silently. `st.dataframe` selection is native and testable, and a
re-rendered region beats zooming a raster. The cost is that tracing runs one
way, from a row to the sheet; the box numbers carry the other direction.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw

from extractors import pdf_to_image as R
from extractors import verification as VER
from extractors.models import ExtractionResult, Region
from extractors.sheet_grid import DEFAULT_GRID, SheetGrid

# How a value was obtained. Not a confidence score — a provenance, which is what
# a checker actually needs: a figure read from the sheet's own characters is a
# different kind of claim from one a vision model read off pixels.
#
# INCOMPLETE is not "this value was invented". The value was read; the drawing
# simply did not state everything about the thing it describes — a pedestal
# callout gives 600 x 500 and a count but never a height. Colouring the whole
# row "assumed" would libel a figure that was read correctly, so it says what is
# true: read, with something still to supply. Those boxes are exactly where a
# checker has to put a number.
EXACT, MODEL, INCOMPLETE = "exact", "model", "incomplete"

SOURCE_LABEL = {
    EXACT: "Read from the sheet's text",
    MODEL: "Read by the model",
    INCOMPLETE: "Read — the drawing left a figure out",
}
SOURCE_COLOUR = {            # RGB, matching the badge palette in ui/kit.py
    EXACT: (27, 107, 58),
    MODEL: (31, 78, 120),
    INCOMPLETE: (138, 83, 0),
}
SOURCE_ORDER = (EXACT, MODEL, INCOMPLETE)

# Phrases `verification.collect_items` uses in a note when the sheet left a
# figure out. They appear in the note, never in the value itself.
_INCOMPLETE_MARKERS = ("not on drawing", "not stated", "not extracted",
                       "incomplete on the drawing", "placeholder")


@dataclass(frozen=True)
class Evidence:
    """One extracted value, and where it sits on the sheet."""
    number: int                              # matches the Verification sheet
    kind: str
    label: str
    extracted: str
    callout: str
    grid_ref: str
    note: str
    source: str
    rect: tuple[float, float, float, float] | None   # page fractions

    @property
    def traceable(self) -> bool:
        return self.rect is not None

    @property
    def title(self) -> str:
        return f"{self.kind} {self.label}".strip()


def source_of(item: VER.CheckItem, *, used_text_layer: bool) -> str:
    """How this value was obtained.

    A sheet that kept its text layer is read from characters, so every value off
    it is exact; otherwise the model read it. Either way, a value whose note
    says the drawing left something out is flagged, because that is where a
    figure has to come from a person.
    """
    if any(marker in (item.note or "").lower() for marker in _INCOMPLETE_MARKERS):
        return INCOMPLETE
    return EXACT if used_text_layer else MODEL


def evidence(result: ExtractionResult,
             grid: SheetGrid = DEFAULT_GRID) -> list[Evidence]:
    """Every value a person should check, numbered as the workbook numbers it.

    The numbering is `verification_rows`', not a fresh count of the boxed ones:
    the point of a number is that it means the same thing on the sheet, in this
    table and on the Verification sheet.
    """
    out = []
    for n, item in enumerate(VER.collect_items(result, grid), start=1):
        rect = tuple(item.rect_norm) if len(item.rect_norm or []) == 4 else None
        out.append(Evidence(
            number=n, kind=item.kind, label=item.label, extracted=item.extracted,
            callout=item.callout, grid_ref=item.grid_ref, note=item.note,
            source=source_of(item, used_text_layer=bool(result.used_text_layer)),
            rect=rect))
    return out


def traceable(items: list[Evidence]) -> list[Evidence]:
    return [i for i in items if i.traceable]


def ordered(items: list[Evidence]) -> list[Evidence]:
    """Table order: the values with a box first, then by number."""
    return sorted(items, key=lambda i: (not i.traceable, i.number))


# What the overlay needs to draw one box, as plain data. The renderer is cached
# per drawing and per selection, and a cache key has to be hashable — an
# Evidence list is not, and a tuple of numbers is.
Box = tuple[int, str, tuple[float, float, float, float]]


def boxes_of(items: list[Evidence]) -> tuple[Box, ...]:
    return tuple((i.number, i.source, i.rect) for i in traceable(items))


def table_rows(items: list[Evidence]) -> list[dict]:
    """The evidence table. One row per value, boxed ones first.

    A row whose value has no box on the sheet still belongs here — the title
    block and a slab read off the plan view are things to check — so it says so
    rather than being dropped.
    """
    return [{
        "#": i.number,
        "On sheet": "□" if i.traceable else "",
        "Item": i.title,
        "Extracted": i.extracted,
        "Source": SOURCE_LABEL[i.source],
        "Grid": i.grid_ref,
        "Callout": i.callout,
        "Note": i.note,
    } for i in ordered(items)]


def counts(items: list[Evidence]) -> dict[str, int]:
    """How many values came from where, in a fixed order, zeros left out."""
    tally = {source: 0 for source in SOURCE_ORDER}
    for i in items:
        tally[i.source] += 1
    return {k: v for k, v in tally.items() if v}


def crop_region(rect: tuple[float, float, float, float], *,
                pad: float = 0.6, minimum: float = 0.07,
                name: str = "trace") -> Region:
    """The piece of the sheet to redraw around one box.

    Padded by a share of the box's own size so a callout arrives with the
    linework it refers to, and never smaller than `minimum` of the sheet — a
    35-character callout on an A0 sheet is a sliver, and a sliver blown up to
    screen width is unreadable in a different way.
    """
    x0, y0, x1, y1 = rect
    w, h = max(x1 - x0, 0.0), max(y1 - y0, 0.0)
    grow_x = max(w * pad, (minimum - w) / 2, 0.0)
    grow_y = max(h * pad, (minimum - h) / 2, 0.0)
    return Region(name=name,
                  x0=_clamp(x0 - grow_x), y0=_clamp(y0 - grow_y),
                  x1=_clamp(x1 + grow_x), y1=_clamp(y1 + grow_y))


def draw_boxes(png: bytes, boxes: tuple[Box, ...], *,
               selected: int | None = None, fade: float = 0.35) -> bytes:
    """The sheet with every located value boxed, numbered and coloured.

    The drawing is faded first: these sheets are dense linework, and an
    annotation drawn on top of them at full contrast is just more linework.
    """
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if fade:
        img = Image.blend(img, Image.new("RGB", img.size, "white"), fade)
    W, H = img.size
    draw = ImageDraw.Draw(img)
    font = VER._load_font(max(11, int(W / 70)))

    for number, source, rect in boxes:
        colour = SOURCE_COLOUR[source]
        chosen = selected is not None and number == selected
        x0, y0, x1, y1 = rect
        box = [x0 * W - 4, y0 * H - 4, x1 * W + 4, y1 * H + 4]
        draw.rectangle(box, outline=colour,
                       width=max(2, int(W / (260 if chosen else 700))))
        tag = str(number)
        tw = draw.textlength(tag, font=font)
        th = int(W / 60)
        draw.rectangle([box[0], max(0, box[1] - th), box[0] + tw + 10,
                        max(0, box[1] - th) + th], fill=colour)
        draw.text((box[0] + 5, max(0, box[1] - th) + 1), tag, fill="white",
                  font=font)
    return _png(img)


def draw_trace(png: bytes, rect: tuple[float, float, float, float],
               region: Region, source: str) -> bytes:
    """A redrawn region, with the value's own box marked inside it."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = img.size
    span_x = max(region.x1 - region.x0, 1e-9)
    span_y = max(region.y1 - region.y0, 1e-9)
    x0 = (rect[0] - region.x0) / span_x * W
    y0 = (rect[1] - region.y0) / span_y * H
    x1 = (rect[2] - region.x0) / span_x * W
    y1 = (rect[3] - region.y0) / span_y * H
    draw = ImageDraw.Draw(img)
    draw.rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3],
                   outline=SOURCE_COLOUR.get(source, SOURCE_COLOUR[MODEL]),
                   width=max(2, int(W / 220)))
    return _png(img)


def render_sheet(path: str, page_idx: int, longest_edge_px: int) -> bytes:
    doc, page = R.open_page(path, page_idx)
    try:
        return R.render_full(page, longest_edge_px).png
    finally:
        doc.close()


def render_region(path: str, page_idx: int, region: Region,
                  longest_edge_px: int) -> bytes:
    """Redraw one part of the sheet from the PDF, at whatever size is asked.

    This is the zoom. The pixels are made now, from the vectors, so a callout
    that is six pixels tall on the full sheet is legible here.
    """
    doc, page = R.open_page(path, page_idx)
    try:
        return R.render_region(page, region, longest_edge_px).png
    finally:
        doc.close()


def _clamp(v: float) -> float:
    return min(1.0, max(0.0, v))


def _png(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
