"""Prompt templates for the two-stage (plus slab) extraction.

Design rules learned from probing qwen2.5vl:7b against the Maaden A0:

* **Ask for a verbatim copy, not an interpretation.** Every callout prompt makes
  the model echo `raw_text` alongside the parsed fields. The numbers are then
  re-derived from `raw_text` by regex in `callout_grammar.py`. A 7b vision model
  is a good OCR and a poor parser; this split plays to that.
* **Do not stack prohibitions.** Adding "ignore X", "ignore Y" and an explicit
  "return empty if none" collapsed recall to zero on a tile whose callouts the
  same model read perfectly without them (see SUPPRESSIVE_PHRASES below). One
  short rules line is the ceiling. Filtering happens in `callout_grammar.py`,
  not in the prompt: the model should over-report, the parser should reject.
* **Never ask two stages in one call.** Combining title block and callouts
  halved accuracy on both in testing.
* **State the units.** Maaden general note 1 is "ALL DIMENSIONS ARE IN
  MILLIMETERS UNLESS OTHERWISE SPECIFIED"; saying so stops the model
  helpfully converting 600 to 0.6.
"""
from __future__ import annotations

import json

# ---------- Stage 1: title block ----------
TITLE_BLOCK_PROMPT = """\
You are extracting structured data from an engineering drawing title block.

The image is the lower-right corner of a construction drawing sheet. It contains
a ruled title block with labelled cells: DRAWING NUMBER, REV, TITLE, and a table
of DRAWN / CHECKED / ENGINEER / APPROVED names with dates.

Return ONLY valid JSON matching this schema:
{
  "drawing_no": "string",
  "revision": "string",
  "project_name": "string",
  "date": "string",
  "prepared_by": "string"
}

Field rules:
- "drawing_no" is the value in the DRAWING NUMBER cell, copied character for
  character including every hyphen.
- "revision" is the value in the REV cell (e.g. "C01", "A", "0").
- "project_name" is the full multi-line TITLE text, joined with single spaces.
- "date" is the date on the DRAWN row, exactly as printed (e.g. "30/09/25").
- "prepared_by" is the name on the DRAWN row.

If a field is not clearly visible, use an empty string. Do not guess.
"""

# ---------- Stage 2: pedestal callouts ----------
PEDESTAL_PROMPT = """\
You are reading text from a civil engineering foundation drawing.
All dimensions on this drawing are in millimetres.

Find every pedestal callout. They look like:
  "TYP DETAIL OF PEDESTAL P1(600x500) 2Nos"
  "PEDESTAL P2 (500x500) 20Nos"
  "P3 (350x350) 11 Nos"

For each one, copy the callout text VERBATIM into "raw_text", then split it
into fields:
  tag        - the pedestal mark, e.g. "P1"
  length_mm  - the first number inside the parenthesis
  width_mm   - the second number inside the parenthesis
  quantity   - the number immediately before "Nos"

Return ONLY valid JSON in this exact shape:
{"pedestals": [
  {"raw_text": "TYP DETAIL OF PEDESTAL P1(600x500) 2Nos",
   "tag": "P1", "length_mm": 600, "width_mm": 500, "quantity": 2}
]}

Rules: only report a pedestal if you can read its tag, both dimensions in the
parenthesis, and the quantity before "Nos". Omit anything you cannot read.
Do not guess. Do not invent pedestals. If there are none, return
{"pedestals": []}.
"""

# Phrases that measurably suppressed detection on qwen2.5vl:7b. Kept here so the
# regression test in tests/test_qwen_vision.py can assert they never come back.
#
# An earlier version of PEDESTAL_PROMPT added three "be careful" rules:
#   "Ignore FOUNDATION callouts (e.g. 'TYP DETAIL OF FOUNDATION F1') ..."
#   "Ignore plain position marks in the plan view that have no dimensions."
#   "If this image contains no pedestal callouts, return exactly ..."
# On the identical tile image, that prompt returned {"pedestals": []} while the
# prompt above returned both callouts correctly. A 7b model over-applies
# prohibitions: given several reasons to withhold plus a cheap null answer, the
# null answer wins.
#
# The filtering those rules were trying to do is real and still happens — in
# callout_grammar.py, deterministically and under test. "TYP DETAIL OF
# FOUNDATION F1" fails the callout grammar; undimensioned plan marks like a bare
# "P2" fail it too; implausible values fail the envelope. Recall belongs to the
# model, precision belongs to the parser.
SUPPRESSIVE_PHRASES = (
    "Ignore FOUNDATION callouts",
    "Ignore plain position marks",
    "If this image contains no pedestal callouts",
)

# ---------- Stage 3: grade slab ----------
GRADE_SLAB_PROMPT = """\
You are reading the foundation layout plan of a civil engineering drawing.
All dimensions on this drawing are in millimetres.

The plan has a title underneath it, such as:
  "FOUNDATION LAYOUT FOR EXISTING MGA PUMP AREA GRADE SLAB & PIPE SUPPORT
   (TOC EL. 97.550 UNO)"
  "(300 THK)(UNO)"

Read:
  length_mm     - the single largest overall dimension running along the TOP of
                  the plan (the full extent of the slab), e.g. 24430
  width_mm      - the single largest overall dimension running down the LEFT or
                  RIGHT side of the plan (the full extent of the slab), e.g. 8600
  thickness_mm  - the slab thickness, printed near the plan title as
                  "(300 THK)" or "300 THK"
  toc_level     - the top-of-concrete level in the plan title, e.g. "97.550"
  raw_text      - the plan title line, copied verbatim

Return ONLY valid JSON:
{"grade_slab": {"length_mm": 24430, "width_mm": 8600, "thickness_mm": 300,
                "toc_level": "97.550",
                "raw_text": "FOUNDATION LAYOUT ... (TOC EL. 97.550 UNO)"}}

Rules:
- Use only the OVERALL extents. Ignore the many small chained dimensions
  between individual pedestals.
- If a value is not clearly readable, set it to null. Do not guess.
- If this image contains no foundation layout plan, return
  exactly {"grade_slab": null}.
"""


# ---------- Few-shot injection (handoff §5 stage 3: RAG) ----------
def with_examples(base_prompt: str, examples: list[dict], *,
                  max_examples: int = 2) -> str:
    """Prepend verified (drawing -> correct JSON) pairs as few-shot context.

    The examples are text-only: we do not re-send their images. On a 7b model
    the payload cost of a second A0 image outweighs the benefit, whereas the
    JSON alone is enough to pin the output shape and the callout vocabulary of
    this drawing family.
    """
    if not examples:
        return base_prompt

    blocks = []
    for ex in examples[:max_examples]:
        label = ex.get("drawing_no") or "a similar drawing"
        payload = ex.get("verified_extraction", {})
        blocks.append(
            f"On {label}, the verified correct answer was:\n"
            f"{json.dumps(payload, separators=(',', ':'))}"
        )

    return (
        base_prompt
        + "\n\nReference — previously verified extractions from drawings in the "
          "same family. Match this style and vocabulary, but report only what "
          "you can actually read in the CURRENT image:\n"
        + "\n".join(blocks)
        + "\n"
    )


# ---------- Stage 2 (montage strategy): transcribe located text ----------
def transcribe_prompt(n_crops: int) -> str:
    """Ask the model to read every crop in a montage, keyed by its printed number.

    Three details are load-bearing, all found by measurement:

    * **State the crop count.** Without "{n} crops", the model transcribed the
      first crop and stopped — 3 of 25 lines. With it, all 9 came back.
    * **Key by the printed number, not by order.** Asking for crops "in order"
      does not work: on a two-column montage the model returned the top-right
      crop first. Position-based mapping then attributed text to the wrong patch
      of drawing, which is worse than useless when the output is a check print
      telling an engineer where to look. Each crop carries a printed index; the
      model reports it back and the mapping becomes explicit.
    * **One entry per crop.** Makes the count checkable and stops the model
      merging adjacent crops into one line.

    The model does pure OCR over text that `vector_text` already located; every
    element type is then recognised by regex in `callout_grammar.scan_lines`, so
    a new element type costs no extra model calls.
    """
    return f"""\
You are transcribing text from an engineering drawing.

This image is a montage of {n_crops} separate crops taken from the drawing.
Each crop has a NUMBER printed to its left, from 1 to {n_crops}.

Transcribe the text in every one of the {n_crops} crops, exactly as printed.
Copy characters literally, including parentheses, the letter "x" between
dimensions, decimal points, and abbreviations such as THK, Nos, EL, TYP, DIA.

For each crop, report the number printed beside it in "id", and its text lines
in "lines". Do not include the printed number itself in "lines".

Return ONLY valid JSON, one entry per crop, {n_crops} entries in total:
{{"crops": [
  {{"id": 1, "lines": ["TYP DETAIL OF PEDESTAL", "P1(600x500) 2Nos", "Scale:1/25"]}},
  {{"id": 2, "lines": ["SECTION", "Scale:1/25"]}}
]}}
"""
