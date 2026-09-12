"""Pydantic models for the Phase 2 vision-extraction layer.

These are *extraction* models, deliberately looser than `core.models`:
every field is optional or defaulted, because a vision model reading an A0
drawing will routinely fail to read some of them. Validation and promotion to
the strict `core.models` types happens in `qwen_vision.merge_into_project`.

Units: the drawing speaks millimetres (general note 1 on every Maaden sheet:
"ALL DIMENSIONS ARE IN MILLIMETERS UNLESS OTHERWISE SPECIFIED"). Extraction
models therefore keep *_mm; conversion to metres happens at merge time only.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]


# ---------- Sheet geometry ----------
class Region(BaseModel):
    """A named rectangle on the sheet, in *normalised* page fractions.

    (0,0) is the top-left of the page as displayed (i.e. after the PDF /Rotate
    entry has been applied), (1,1) the bottom-right. Fractions rather than
    points so the same region spec works on A0, A1 and A3 issues of the same
    drawing — ISO 7200 puts the title block bottom-right regardless of size.
    """
    name: str
    x0: float = Field(..., ge=0.0, le=1.0)
    y0: float = Field(..., ge=0.0, le=1.0)
    x1: float = Field(..., ge=0.0, le=1.0)
    y1: float = Field(..., ge=0.0, le=1.0)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


# ---------- Stage 1: title block ----------
class TitleBlockExtraction(BaseModel):
    """Tier 1 targets — structured position in the sheet, >90% expected."""
    drawing_no: str = ""
    revision: str = ""
    project_name: str = ""
    date: str = ""                      # normalised to YYYY-MM-DD when parseable
    date_raw: str = ""                  # exactly what the model read, e.g. "30/09/25"
    prepared_by: str = ""               # the DRAWN row of the title block
    confidence: Confidence = "low"

    def is_empty(self) -> bool:
        return not any((self.drawing_no, self.revision, self.project_name, self.date))


# ---------- Stage 2: pedestal callouts ----------
class PedestalExtraction(BaseModel):
    """Tier 2 target — one 'TYP DETAIL OF PEDESTAL Pn(LxW) qNos' callout.

    `raw_text` is the model's verbatim copy of the callout. It is the anchor for
    deterministic re-parsing: if the regex grammar matches raw_text, the numeric
    fields are overwritten from the regex rather than trusted from the model.
    That converts the LLM from "parser" into "OCR", which is the only role a 7b
    vision model is reliable in.
    """
    tag: str
    length_mm: float
    width_mm: float
    quantity: int
    height_mm: Optional[float] = None    # almost never in the callout — see §3 of handoff
    raw_text: str = ""
    regex_validated: bool = False        # True when raw_text matched the callout grammar
    seen_in_regions: list[str] = []      # which tile(s) produced this callout
    # Where on the sheet this was read, as normalised (x0, y0, x1, y1), plus the
    # drawing-border grid square (e.g. "D-7"). Both are what let a checker find
    # the value on an A0 print instead of hunting for it.
    source_rect: list[float] = []
    grid_ref: str = ""
    confidence: Confidence = "low"
    notes: str = ""


# ---------- Stage 3: grade slab ----------
class GradeSlabExtraction(BaseModel):
    """Tier 2 target — overall slab extent read off the foundation layout plan."""
    tag: str = "GS-01"
    length_mm: Optional[float] = None
    width_mm: Optional[float] = None
    thickness_mm: Optional[float] = None
    toc_level: str = ""                  # e.g. "97.550" from "(TOC EL. 97.550 UNO)"
    raw_text: str = ""
    confidence: Confidence = "low"
    notes: str = ""

    def is_usable(self) -> bool:
        return bool(self.length_mm and self.width_mm and self.thickness_mm)


# ---------- Per-call diagnostics ----------
class TranscriptBlock(BaseModel):
    """One crop of the sheet and the text the model read in it.

    Keeping the crop's rectangle alongside its lines is what makes the check
    print possible: every extracted number can be traced back to the patch of
    drawing it came from.
    """
    region: str = ""
    rect_norm: list[float] = []          # (x0, y0, x1, y1) as page fractions
    grid_ref: str = ""
    lines: list[str] = []


class RegionResponse(BaseModel):
    """One vision call: what we sent, what came back, how long it took."""
    region: str
    stage: str
    px_width: int = 0
    px_height: int = 0
    ink_ratio: float = 0.0               # fraction of non-white pixels; drives blank-tile skip
    skipped: bool = False
    elapsed_s: float = 0.0
    raw_response: str = ""
    error: str = ""


# ---------- Aggregate ----------
class ExtractionResult(BaseModel):
    """Everything one drawing produced. Consumed by merge_into_project."""
    source_pdf: str = ""
    page_number: int = 0
    profile: str = "thorough"
    model: str = ""
    image_hash: str = ""                 # dHash of the whole sheet, for RAG retrieval

    title_block: TitleBlockExtraction = TitleBlockExtraction()
    pedestals: list[PedestalExtraction] = []
    grade_slabs: list[GradeSlabExtraction] = []

    responses: list[RegionResponse] = []
    confidence_notes: list[str] = []
    total_elapsed_s: float = 0.0

    # Montage strategy only: every text line the model read, and everything the
    # grammars recognised in them. `findings` carries element types that cannot
    # be built into a core.models object without data the drawing does not print
    # (a curb wall callout gives thickness and height but never its run length),
    # so they are surfaced for the human rather than merged.
    transcribed_lines: list[str] = []
    transcript_blocks: list[TranscriptBlock] = []
    findings: dict = {}
    text_blocks_found: int = 0
    # Element marks (P1, F2 …) counted on the sheet. An indication of quantity
    # for review, never merged into the BOQ — see text_layer.count_position_marks.
    position_marks: dict[str, int] = {}
    montages_sent: int = 0
    used_text_layer: bool = False

    # Open-vocabulary discovery: every item the sheet specifies, named in the
    # drawing's own words rather than matched against a list of element types we
    # chose in advance. See extractors/discovery.py — a drawing is not obliged
    # to contain a pedestal, and most of this set's sheets do not.
    discovery: dict = {}

    # Kept for the debugging contract named in the handoff §6.
    @property
    def raw_stage1_response(self) -> str:
        return next((r.raw_response for r in self.responses if r.stage == "title_block"), "")

    @property
    def raw_stage2_response(self) -> str:
        return "\n".join(r.raw_response for r in self.responses if r.stage == "pedestals")

    @property
    def raw_stage3_response(self) -> str:
        return next((r.raw_response for r in self.responses if r.stage == "grade_slab"), "")

    def note(self, msg: str) -> None:
        if msg not in self.confidence_notes:
            self.confidence_notes.append(msg)


# ---------- Merge planning ----------
class MergeChange(BaseModel):
    """One field/element the merge would add. Drives the 'Preview merge' UI."""
    kind: Literal["field", "pedestal", "grade_slab"]
    target: str                          # e.g. "drawing_no" or "P1"
    action: Literal["add", "skip_existing", "reject"]
    detail: str = ""


class MergePlan(BaseModel):
    changes: list[MergeChange] = []
    warnings: list[str] = []

    @property
    def additions(self) -> list[MergeChange]:
        return [c for c in self.changes if c.action == "add"]
