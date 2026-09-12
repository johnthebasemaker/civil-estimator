"""Find text on a drawing sheet without asking the model where it is.

The reference drawing has **no text layer** — every glyph is outlined to Bezier
curves, so `page.get_text()` returns "". But the curves are still there, and a
glyph outline has a very distinctive geometric signature: a small, compact path,
sitting on a baseline with a row of similar-sized siblings.

That signature is enough to locate every block of text on an A0 in under a
second of pure geometry. Which changes the economics of the whole extractor:

* A blind tile sweep spends ~26 model calls looking at mostly empty paper and
  dense linework, and still lets a lone callout share a tile with a 13-line
  notes block.
* Locating the text first means cropping only the text, packing many crops into
  one montage image, and spending ~6 calls — because on this hardware the cost
  is *per call*, not per pixel.

Two paths out of this module:

    text_lines_from_layer(page)   -> exact text when the PDF has a text layer
    text_blocks_from_vectors(page)-> geometric text blocks when it does not

Both return the same shape, so the caller does not care which happened.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import fitz
from PIL import Image, ImageDraw, ImageFont

# A glyph on a drawing sheet: small, compact, not a dimension line.
MIN_GLYPH_PT = 0.4
MAX_GLYPH_PT = 40.0
MIN_GLYPH_AREA_PT2 = 0.2

# Title-sized text. Maaden body notes are ~7 pt; callout and section titles are
# ~14 pt. Filtering to >= 9 pt isolates the titles, which is where the callouts
# live, and drops the notes blocks that were starving them.
DEFAULT_MIN_TEXT_PT = 9.0

# Padding used to glue neighbouring glyphs into one block. Horizontal padding is
# generous (word gaps), vertical padding is tight (do not merge stacked rows of
# unrelated text).
GLUE_X_PT = 26.0
GLUE_Y_PT = 9.0


@dataclass
class TextBlock:
    """A rectangle on the sheet believed to contain text."""
    rect: fitz.Rect
    glyph_count: int = 0
    text_height_pt: float = 0.0
    text: str = ""                 # populated only via the text-layer path
    source: str = "vector"         # "vector" | "text_layer"

    def norm(self, page_rect: fitz.Rect) -> tuple[float, float, float, float]:
        return (self.rect.x0 / page_rect.width, self.rect.y0 / page_rect.height,
                self.rect.x1 / page_rect.width, self.rect.y1 / page_rect.height)


@dataclass
class MontageSlot:
    """Where one block ended up inside a montage image."""
    block: TextBlock
    x: int
    y: int
    w: int
    h: int


@dataclass
class Montage:
    """One packed image plus the map of what went into it."""
    png: bytes
    width: int
    height: int
    slots: list[MontageSlot] = field(default_factory=list)

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1e6


# ---------- Fast path: the PDF actually has text ----------
def text_lines_from_layer(page: fitz.Page) -> list[TextBlock]:
    """Real text spans, when the PDF has a text layer.

    Costs nothing and is exact, so it is always tried first. Most CAD exports
    that were not flattened will land here and skip the model entirely for
    callout reading.
    """
    out: list[TextBlock] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if not text:
                continue
            bbox = fitz.Rect(line["bbox"])
            sizes = [s.get("size", 0.0) for s in line.get("spans", [])]
            out.append(TextBlock(rect=bbox, glyph_count=len(text),
                                 text_height_pt=max(sizes) if sizes else bbox.height,
                                 text=text, source="text_layer"))
    return out


# ---------- Vector path: reconstruct where text is ----------
def glyph_rects(page: fitz.Page, min_height_pt: float = DEFAULT_MIN_TEXT_PT,
                max_height_pt: float = MAX_GLYPH_PT) -> list[fitz.Rect]:
    """Vector paths whose geometry looks like a glyph outline.

    Note the rotation matrix: `get_drawings()` reports coordinates in the
    *unrotated* PDF space. This sheet is stored portrait with /Rotate 270, so
    without the transform every x and y is swapped and the blocks land nowhere
    near the text. That bug is silent — you get plausible-looking boxes.
    """
    m = page.rotation_matrix
    out: list[fitz.Rect] = []
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"] * m)
        w, h = r.width, r.height
        if (min_height_pt <= h <= max_height_pt
                and MIN_GLYPH_PT <= w <= MAX_GLYPH_PT
                and w * h > MIN_GLYPH_AREA_PT2):
            out.append(r)
    return out


def _merge_overlapping(rects: list[fitz.Rect]) -> list[fitz.Rect]:
    """Union every group of overlapping rectangles (iterate to a fixed point)."""
    merged: list[fitz.Rect] = []
    for r in rects:
        box = fitz.Rect(r)
        hits = [m for m in merged if m.intersects(box)]
        for m in hits:
            merged.remove(m)
            box |= m
        merged.append(box)

    changed = True
    while changed:
        changed = False
        out: list[fitz.Rect] = []
        for box in merged:
            box = fitz.Rect(box)
            hits = [m for m in out if m.intersects(box)]
            if hits:
                changed = True
                for m in hits:
                    out.remove(m)
                    box |= m
            out.append(box)
        merged = out
    return merged


def text_blocks_from_vectors(page: fitz.Page,
                             min_text_pt: float = DEFAULT_MIN_TEXT_PT,
                             glue_x: float = GLUE_X_PT,
                             glue_y: float = GLUE_Y_PT) -> list[TextBlock]:
    """Cluster glyph-shaped paths into blocks of text."""
    glyphs = glyph_rects(page, min_text_pt)
    if not glyphs:
        return []
    padded = [fitz.Rect(g.x0 - glue_x, g.y0 - glue_y, g.x1 + glue_x, g.y1 + glue_y)
              for g in glyphs]
    blocks = []
    for box in _merge_overlapping(padded):
        inside = [g for g in glyphs if box.intersects(g)]
        if not inside:
            continue
        heights = sorted(g.height for g in inside)
        blocks.append(TextBlock(
            rect=box, glyph_count=len(inside),
            text_height_pt=heights[len(heights) // 2], source="vector"))
    return blocks


def find_text_blocks(page: fitz.Page,
                     min_text_pt: float = DEFAULT_MIN_TEXT_PT) -> list[TextBlock]:
    """Text-layer path if available, geometric reconstruction otherwise."""
    layer = text_lines_from_layer(page)
    if layer:
        return [b for b in layer if b.text_height_pt >= min_text_pt * 0.5]
    return text_blocks_from_vectors(page, min_text_pt)


# ---------- Candidate selection ----------
# A callout title on a Maaden sheet is a wide, one-to-three-line block, e.g.
# "TYP DETAIL OF PEDESTAL / P1(600x500) 2Nos / Scale:1/25" at ~315x70 pt.
# The bounds are deliberately loose: a block that is not a callout only costs
# montage space, whereas a callout excluded here can never be found.
CANDIDATE_MIN_W_PT = 140.0
CANDIDATE_MAX_W_PT = 520.0
CANDIDATE_MIN_H_PT = 40.0
CANDIDATE_MAX_H_PT = 120.0


def callout_candidates(blocks: list[TextBlock], *,
                       min_w: float = CANDIDATE_MIN_W_PT,
                       max_w: float = CANDIDATE_MAX_W_PT,
                       min_h: float = CANDIDATE_MIN_H_PT,
                       max_h: float = CANDIDATE_MAX_H_PT) -> list[TextBlock]:
    """Blocks shaped like a callout title, in reading order (top-left first)."""
    out = [b for b in blocks
           if min_w <= b.rect.width <= max_w and min_h <= b.rect.height <= max_h]
    out.sort(key=lambda b: (round(b.rect.y0 / 40), b.rect.x0))
    return out


# ---------- Montage packing ----------
TARGET_TEXT_PX = 22.0          # on-screen glyph height; 25 px read reliably
DEFAULT_MONTAGE_WIDTH = 1200
DEFAULT_MONTAGE_BUDGET = 950_000   # stay under the latency cliff


def render_block(page: fitz.Page, block: TextBlock,
                 target_text_px: float = TARGET_TEXT_PX) -> Image.Image:
    """Rasterise one text block, scaled so its glyphs are ~target_text_px tall."""
    height = block.text_height_pt or 14.0
    zoom = max(0.5, min(8.0, target_text_px / height))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=block.rect, alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


# Width reserved down the left of every crop for its printed index number.
LABEL_GUTTER_PX = 46


def _label_font(size: int):
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _pack(images: list[Image.Image], blocks: list[TextBlock],
          width: int, gap: int) -> tuple[Image.Image, list[MontageSlot]]:
    """Two-column shelf packing, each crop stamped with its index.

    The index is the important part. Asking the model to transcribe crops "in
    order" does not work — on a two-column montage it returned the top-*right*
    crop first, so mapping transcript position to crop position silently
    attributed text to the wrong patch of drawing. Printing a number beside each
    crop and asking for it back makes the mapping explicit: order no longer
    matters, and a montage that skips crops still maps the ones it did return.
    """
    col_w = (width - gap * 3) // 2
    inner_w = col_w - LABEL_GUTTER_PX
    cols: list[list[tuple[Image.Image, TextBlock]]] = [[], []]
    heights = [gap, gap]
    for img, blk in zip(images, blocks):
        if img.width > inner_w:
            img = img.resize((inner_w, max(1, round(img.height * inner_w / img.width))),
                             Image.LANCZOS)
        i = 0 if heights[0] <= heights[1] else 1
        cols[i].append((img, blk))
        heights[i] += img.height + gap

    canvas = Image.new("RGB", (width, max(heights)), "white")
    draw = ImageDraw.Draw(canvas)
    font = _label_font(26)

    # Number down the left column first, then the right, so the printed order
    # still reads naturally to a person checking the montage.
    slots: list[MontageSlot] = []
    index = 0
    for ci, col in enumerate(cols):
        x = gap + ci * (col_w + gap)
        y = gap
        for img, blk in col:
            index += 1
            canvas.paste(img, (x + LABEL_GUTTER_PX, y))
            draw.text((x + 4, y + 2), f"{index}", fill=(0, 0, 0), font=font)
            draw.line([(x + LABEL_GUTTER_PX - 8, y),
                       (x + LABEL_GUTTER_PX - 8, y + img.height)],
                      fill=(190, 190, 190), width=2)
            slots.append(MontageSlot(block=blk, x=x + LABEL_GUTTER_PX, y=y,
                                     w=img.width, h=img.height))
            y += img.height + gap
    return canvas, slots


def build_montages(page: fitz.Page, blocks: list[TextBlock], *,
                   width: int = DEFAULT_MONTAGE_WIDTH,
                   budget_px: int = DEFAULT_MONTAGE_BUDGET,
                   gap: int = 10,
                   target_text_px: float = TARGET_TEXT_PX) -> list[Montage]:
    """Pack rendered text blocks into as few images as the budget allows.

    Each montage is one model call. Packing greedily to the pixel budget is what
    turns ~38 candidate blocks into ~4 calls.
    """
    if not blocks:
        return []
    rendered = [render_block(page, b, target_text_px) for b in blocks]

    montages: list[Montage] = []
    cur_imgs: list[Image.Image] = []
    cur_blks: list[TextBlock] = []

    def flush() -> None:
        if not cur_imgs:
            return
        canvas, slots = _pack(cur_imgs, cur_blks, width, gap)
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        montages.append(Montage(png=buf.getvalue(), width=canvas.width,
                                height=canvas.height, slots=slots))

    for img, blk in zip(rendered, blocks):
        trial_imgs = cur_imgs + [img]
        trial_blks = cur_blks + [blk]
        canvas, _ = _pack(trial_imgs, trial_blks, width, gap)
        if canvas.width * canvas.height > budget_px and cur_imgs:
            flush()
            cur_imgs, cur_blks = [img], [blk]
        else:
            cur_imgs, cur_blks = trial_imgs, trial_blks
    flush()
    return montages
