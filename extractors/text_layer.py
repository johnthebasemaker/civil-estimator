"""Read a drawing's own text layer, when it has a usable one.

Not every issued PDF is the same animal. Of the eleven Maaden sheets in this
project, five carry a real text layer (6,000-8,000 characters) and six have had
their text outlined to curves. The vision pipeline was written for the second
kind and, on the first kind, looked straight past thousands of extractable
characters and reported nothing.

Reading the text layer is better in every way when it is there:

    * exact characters — no OCR to second-guess, so no transcription errors;
    * exact coordinates — every span carries its bbox, so grid references and
      the check print are precise rather than voted on;
    * no model calls at all — instant, and the Mac stays cool.

The catch is that a PDF's text layer is a bag of positioned spans, not lines of
prose. "TYP DETAIL OF PEDESTAL" and "P1(600x500) 2Nos" are separate spans on
separate baselines, and reading them in storage order interleaves callouts from
opposite corners of an A0. So spans are regrouped by geometry into visual lines,
and lines that sit directly under one another in a column are stitched into
blocks — which is what the callout grammar expects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

# A handful of stray characters is not a text layer. Two of these sheets report
# 141 and 328 characters — border labels and a stamp — while being entirely
# outlined otherwise. Treating those as "has text" is what made the pipeline
# skip the vision path and return nothing.
MIN_USABLE_CHARS = 1_500
MIN_USABLE_SPANS = 60

# Two spans belong to the same visual line when their baselines are within this
# fraction of the text height.
LINE_TOLERANCE = 0.6
# A line joins the block above it when it starts within this multiple of the
# line height below it, and their horizontal spans overlap.
BLOCK_GAP = 1.9


@dataclass
class TextLine:
    """One visual line of text, with where it sits on the sheet."""
    text: str
    rect: fitz.Rect
    size: float = 0.0
    rotated: bool = False


@dataclass
class TextBlockT:
    """A run of lines stacked directly under one another — e.g. a callout."""
    lines: list[TextLine] = field(default_factory=list)
    rect: fitz.Rect = field(default_factory=fitz.Rect)

    @property
    def texts(self) -> list[str]:
        return [l.text for l in self.lines]

    @property
    def joined(self) -> str:
        return " ".join(l.text for l in self.lines)


def text_stats(page: fitz.Page) -> tuple[int, int]:
    """(characters, spans) in the page's text layer."""
    chars = len(page.get_text().strip())
    spans = sum(1 for b in page.get_text("dict")["blocks"]
                for l in b.get("lines", []) for s in l.get("spans", [])
                if s["text"].strip())
    return chars, spans


def has_usable_text(page: fitz.Page) -> bool:
    """Whether the text layer is worth reading instead of looking at pixels."""
    chars, spans = text_stats(page)
    return chars >= MIN_USABLE_CHARS and spans >= MIN_USABLE_SPANS


def _spans(page: fitz.Page) -> list[tuple[fitz.Rect, str, float, bool]]:
    """Every non-empty span, in display coordinates."""
    m = page.rotation_matrix
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            # dir is the writing direction; anything not left-to-right is a
            # rotated label (section marks, the repeated drawing number down the
            # left edge). Kept, but flagged — the handoff rules rotated text out
            # of scope for callouts, and joining it to horizontal neighbours
            # would corrupt them.
            rotated = abs(line.get("dir", (1.0, 0.0))[0]) < 0.9
            for span in line.get("spans", []):
                text = span["text"].strip()
                if not text:
                    continue
                out.append((fitz.Rect(span["bbox"]) * m, text,
                            float(span.get("size", 0.0)), rotated))
    return out


def lines_from_page(page: fitz.Page) -> list[TextLine]:
    """Regroup spans into visual lines, left to right, top to bottom."""
    spans = _spans(page)
    if not spans:
        return []

    horizontal = [s for s in spans if not s[3]]
    rotated = [s for s in spans if s[3]]

    lines: list[TextLine] = []
    for group in (horizontal, rotated):
        if not group:
            continue
        group.sort(key=lambda s: (round(s[0].y0, 1), s[0].x0))
        current: list[tuple[fitz.Rect, str, float, bool]] = []
        for span in group:
            if not current:
                current = [span]
                continue
            prev = current[-1]
            tol = max(prev[0].height, 1.0) * LINE_TOLERANCE
            if abs(span[0].y0 - prev[0].y0) <= tol:
                current.append(span)
            else:
                lines.append(_merge(current))
                current = [span]
        if current:
            lines.append(_merge(current))

    lines.sort(key=lambda l: (l.rect.y0, l.rect.x0))
    return lines


def _merge(spans: list[tuple[fitz.Rect, str, float, bool]]) -> TextLine:
    spans = sorted(spans, key=lambda s: s[0].x0)
    rect = fitz.Rect(spans[0][0])
    parts = [spans[0][1]]
    for prev, cur in zip(spans, spans[1:]):
        gap = cur[0].x0 - prev[0].x1
        # A wide gap is a column break, not a word break — but the grammar reads
        # a line at a time, so both are joined with a single space and the
        # geometry is kept on the rect for anything that needs it.
        parts.append(cur[1])
        rect |= cur[0]
    text = " ".join(p for p in parts if p)
    return TextLine(text=re.sub(r"\s+", " ", text).strip(), rect=rect,
                    size=spans[0][2], rotated=spans[0][3])


def blocks_from_lines(lines: list[TextLine]) -> list[TextBlockT]:
    """Stitch stacked lines into blocks, so a two-line callout stays together."""
    blocks: list[TextBlockT] = []
    used = [False] * len(lines)
    for i, line in enumerate(lines):
        if used[i] or line.rotated:
            continue
        used[i] = True
        block = TextBlockT(lines=[line], rect=fitz.Rect(line.rect))
        for j in range(i + 1, len(lines)):
            if used[j] or lines[j].rotated:
                continue
            nxt = lines[j]
            gap = nxt.rect.y0 - block.rect.y1
            if gap > max(block.rect.height, 8.0) * BLOCK_GAP:
                break
            overlap = (min(block.rect.x1, nxt.rect.x1)
                       - max(block.rect.x0, nxt.rect.x0))
            if overlap <= 0 or gap < -1.0:
                continue
            used[j] = True
            block.lines.append(nxt)
            block.rect |= nxt.rect
        blocks.append(block)
    return blocks


def read_page(page: fitz.Page) -> tuple[list[TextLine], list[TextBlockT]]:
    lines = lines_from_page(page)
    return lines, blocks_from_lines(lines)


# ---------- Title block from text ----------
# The trailing guard is (?!\d) rather than \b: filenames put the revision
# straight after the number as "…-0107_C01", and "_" is a word character, so
# \b never matches there and the filename fallback silently found nothing.
DRAWING_NO_RE = re.compile(
    r"\b[A-Z]{2}-\d{3}-[0-9A-Z]{4}-[A-Z]{2}-[A-Z]{2}-[A-Z]{3}-\d{3,4}(?!\d)")
REVISION_RE = re.compile(r"\b([A-D]\d{2})\b")
DATE_RE = re.compile(r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})\b")


def title_block_from_text(page: fitz.Page, lines: list[TextLine] | None = None,
                          filename: str = "") -> dict:
    """Title-block fields straight from the text layer.

    Picking the sheet's *own* drawing number is the whole difficulty. A sheet
    names several: the references table lists the drawings it depends on, and
    details point at others. Counting occurrences gets it wrong — on these
    sheets 0102 reported 0101, and 0105 reported 0104, because a referenced
    drawing can appear more often than the sheet's own number.

    Position settles it. The ISO border repeats the drawing number in the
    **top-left corner**, and on all five text-layer sheets here the sheet's own
    number is the occurrence nearest that corner, every time. The filename is
    used as a cross-check, and a disagreement is reported rather than silently
    resolved.
    """
    lines = lines if lines is not None else lines_from_page(page)
    rect = page.rect
    notes: list[str] = []

    found: list[tuple[float, str]] = []
    for line in lines:
        for number in DRAWING_NO_RE.findall(line.text):
            # Distance from the top-left corner, in sheet fractions.
            x = line.rect.x0 / max(rect.width, 1.0)
            y = line.rect.y0 / max(rect.height, 1.0)
            found.append((x + y, number))

    drawing_no, source = "", ""
    if found:
        found.sort(key=lambda t: t[0])
        drawing_no, source = found[0][1], "border repeat (top-left)"

    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    from_name = ""
    if stem:
        m = DRAWING_NO_RE.search(stem.upper())
        from_name = m.group(0) if m else ""

    if not drawing_no and from_name:
        drawing_no, source = from_name, "filename"
    elif drawing_no and from_name and drawing_no != from_name:
        notes.append(f"Sheet border reads {drawing_no} but the file is named "
                     f"{from_name} — check which is right.")

    revision = ""
    if stem:
        m = re.search(r"_([A-D]\d{2})\b", stem.upper())
        if m:
            revision = m.group(1)
    if not revision:
        m = REVISION_RE.search("\n".join(l.text for l in lines)[-2000:])
        revision = m.group(1) if m else ""

    dates = DATE_RE.findall("\n".join(l.text for l in lines))
    return {"drawing_no": drawing_no, "revision": revision,
            "date_raw": dates[-1] if dates else "", "source": source,
            "notes": notes}


# ---------- Position marks ----------
MARK_RE = re.compile(r"^([PF])(\d{1,2})$")


def count_position_marks(page: fitz.Page) -> dict[str, int]:
    """Count standalone element marks (P1, F2 …) printed on the sheet.

    On this drawing set the pedestal *sizes* live on a details sheet while the
    *positions* live on the plan sheets, which carry one mark per element and a
    "FOR DETAILS REFER DWG …" note. Counting the marks is how an estimator gets
    the quantity, so it is worth doing — but only spans whose entire text is a
    mark are counted, because "P1" also occurs inside prose like "FOR DETAILS
    REFER DWG P1".

    The result is an **indication, not a quantity**: a pedestal drawn in both
    plan and section is labelled twice, and an unlabelled one is not counted at
    all. It is reported for review, never merged into the BOQ.
    """
    counts: dict[str, int] = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                if MARK_RE.match(text):
                    counts[text] = counts.get(text, 0) + 1
    return dict(sorted(counts.items(),
                       key=lambda kv: (kv[0][0], int(kv[0][1:]))))


# ---------- Span-level labels, for open-vocabulary discovery ----------
def label_lines(page: fitz.Page) -> list[TextLine]:
    """One line per text entity, exactly as the draughtsman placed it.

    `lines_from_page` joins every span sharing a baseline, which is right for a
    paragraph and wrong for a drawing. On an A0 the same baseline carries labels
    from opposite ends of the sheet: at one y on …-0101 it produced

        "3-D10 ANCHOR REINF 4MM THK ACID RESISTANT 3-D10 ANCHOR REINF"

    and any grammar reading outward from a number then picks up a neighbour's
    words. Measuring the gaps settled it — adjacent spans a twentieth of a line
    height apart are still different labels ("D16 @ 200 C/C" beside "(TYP)"),
    because CAD writes each annotation as its own text entity with its own
    string. There is no horizontal gap threshold that separates them, so the
    honest answer is not to join at all.

    Vertical stacking still means something: a callout's second line belongs to
    its first. That is left to `blocks_from_lines`.
    """
    return sorted(
        (TextLine(text=re.sub(r"\s+", " ", text).strip(), rect=rect,
                  size=size, rotated=rotated)
         for rect, text, size, rotated in _spans(page)
         if text.strip()),
        key=lambda l: (l.rect.y0, l.rect.x0))


def label_blocks(page: fitz.Page) -> list[TextBlockT]:
    """Span-level labels, grouped into the stacks they were drawn as."""
    return blocks_from_lines(label_lines(page))
