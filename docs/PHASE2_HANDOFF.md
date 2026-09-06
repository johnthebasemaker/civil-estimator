# Phase 2 Handoff — Qwen2.5-VL:7b Vision Extraction

**Audience:** Claude Code, working on the `civil-estimator` repo after Phase 1 (Steps 1–6) has been completed and merged.

**Reader assumption:** You have full repo access and can read `core/models.py`, `pages/1_Input.py`, `pages/2_Review.py`. If you haven't, do that first.

---

## 1. What Phase 1 already built (context)

- **Structured input model** (`core/models.py`): a Pydantic `Project` class with 15 list fields (`pedestals`, `grade_slabs`, `sumps`, `joints`, `excavations`, etc.). Each list item is a strongly-typed model like `Pedestal(tag, length_m, width_m, height_m, quantity, grade, rebar_coefficient_kg_per_m3)`.
- **Calc engine** (`core/formulas.py`): pure functions that compute volume, formwork area, rebar weight per element. Never touch these — they're tested and stable.
- **BOM builder** (`core/bom_builder.py`): walks a `Project` and produces a flat BOM + rebar BBS. Never touch this.
- **Excel writer** (`core/excel_writer.py`): 10-sheet workbook. Never touch this.
- **Streamlit UI**: `app.py` + 4 pages. The relevant integration point is `st.session_state.project`, which holds the live `Project` instance across all pages.

**Your job in Phase 2 is to populate `st.session_state.project` from a PDF drawing, then let the human review and edit before generation.**

---

## 2. What the target drawing looks like

The reference drawing in this project is **MD-522-8110-EG-CV-LAD-0107 Rev C01** (Maaden Phosphate 3, Phase 1 — RAK-RAS AL KHAIR AREA A, Existing MGA Pump Area Grade Slab Layout & Pipe Support Details).

Key visual features you'll encounter on similar Maaden drawings:
- **A0 landscape** vector PDF, dense multi-panel layout
- **Title block** in the lower-right: drawing no, revision, project name, date, prepared/checked/approved names
- **Pedestal callouts** like `TYP DETAIL OF PEDESTAL P1(600x500) 2Nos` scattered near their section details
- **Section markers** (A, B, C, D, E, F, G, H, etc.) linking plan positions to elevation details on other parts of the sheet
- **Rebar callouts** in section boxes: `(3)-Ø10 CLOSED LINK`, `(2)-Ø10 @ 100 c/c`, etc.
- **Elevation callouts**: `TOC EL 97.850`, `FGL EL 97.100`
- **General notes** in a boxed area (typical of every Maaden drawing) — 13 numbered notes covering coordinate system, coating specs, blinding, etc.

**Do not try to read handwriting on drawing markups.** Ignore any red-line revision annotations — they're inconsistent and low-value.

---

## 3. Extraction targets — realistic accuracy

### Tier 1 — high confidence (>90% expected)
- Drawing number (from title block, structured position)
- Revision (from title block)
- Project name / area (from title block)
- Drawing date (from title block)

### Tier 2 — medium confidence (50–70% expected, always human-review)
- Pedestal callouts: tag (P1, P2…), length_m, width_m, quantity
- Pedestal height: **only if** it appears in the same callout string; otherwise leave blank for human entry
- Grade slab overall dimensions: length_m, width_m (from foundation layout plan)

### Tier 3 — do not attempt in Phase 2
- Rebar bar-by-bar details (cut length, bends, hooks) — needs cross-section reading, LLM cannot do this reliably
- Concrete grades — usually not on foundation layout drawings, live on general notes drawings
- Joint layouts and lengths — visual, requires plan interpretation
- Sump geometry — needs section+plan cross-referencing
- Embedment schedule — needs table extraction from separate schedule
- Excavation extents — implicit in slab extent + depth from section (requires cross-referencing)
- Handwritten quantity annotations
- Any callout inside a rotated text block

**Ship with Tier 1 + Tier 2 only. Tier 3 stays manual entry in `pages/1_Input.py`.**

---

## 4. Runtime — Ollama setup

```bash
# One-time setup on user's machine
ollama pull qwen2.5vl:7b

# Verify
curl http://localhost:11434/api/tags
```

Ollama serves an HTTP API on `localhost:11434` by default. Call `/api/generate` with:
```json
{
  "model": "qwen2.5vl:7b",
  "prompt": "...",
  "images": ["<base64-encoded-png>"],
  "format": "json",
  "stream": false
}
```

**PDF → image conversion:** use PyMuPDF (already in requirements.txt) to rasterise each PDF page to PNG at ~200 DPI. Do NOT send the raw PDF; Qwen2.5-VL is an image model.

Suggested resolution: 2000px on longest edge. Higher = better small-text reading, but slower on 7b at CPU/MPS. Test with the Maaden A0 at 1500 / 2000 / 2500 and pick the fastest that reads title-block text reliably on the user's MacBook Air.

---

## 5. Prompt strategy — two-stage extraction

### Stage 1: Title block (high confidence)

Send only the **lower-right quadrant** of the drawing (crop before sending — dramatically improves accuracy since title blocks are dense and Qwen wastes tokens on irrelevant plan geometry).

System prompt:
You are extracting structured data from an engineering drawing title block.
Return ONLY valid JSON matching this schema:
{
"drawing_no": "string",
"revision": "string",
"project_name": "string",
"date": "YYYY-MM-DD or empty string"
}
If a field is not clearly visible, use an empty string. Do not guess.

### Stage 2: Pedestal callouts (medium confidence)

Send the **full drawing** (or top-half where the plan lives). Prompt:

You are extracting pedestal callouts from a civil foundation drawing.
Look for text patterns like:
"TYP DETAIL OF PEDESTAL P1(600x500) 2Nos"
"PEDESTAL P2 (500x500) 20Nos"
"P3 (350x350) 11 Nos"

For each pedestal type, extract:

tag (e.g. "P1")
length_mm (first dimension in the parenthesis)
width_mm (second dimension in the parenthesis)
quantity (number before "Nos")

Return ONLY valid JSON:
{
"pedestals": [
{"tag": "P1", "length_mm": 600, "width_mm": 500, "quantity": 2},
...
]
}

If you cannot read a value clearly, omit that pedestal entry entirely.
Do not guess dimensions or quantities.


**Convert mm → m before writing to the Project model** (`length_m = length_mm / 1000`).

### Stage 3: RAG example retrieval (optional refinement)

Once you have 5+ verified extractions in `extractors/examples/`, add a retrieval step:
1. Compute a simple embedding of the current drawing image (e.g., pHash or CLIP)
2. Find the closest 1–2 verified examples
3. Include their (prompt, correct-JSON) pairs as few-shot examples in the Stage 2 prompt

Simplest RAG store: JSON files on disk, no vector DB needed for <100 examples.

Example file `extractors/examples/MD-522-8110-EG-CV-LAD-0107.json`:
```json
{
  "drawing_no": "MD-522-8110-EG-CV-LAD-0107",
  "revision": "C01",
  "image_hash": "<pHash>",
  "verified_extraction": {
    "pedestals": [
      {"tag": "P1", "length_mm": 600, "width_mm": 500, "quantity": 2},
      {"tag": "P2", "length_mm": 500, "width_mm": 500, "quantity": 20},
      {"tag": "P3", "length_mm": 350, "width_mm": 350, "quantity": 11},
      {"tag": "P6", "length_mm": 450, "width_mm": 450, "quantity": 6},
      {"tag": "P7", "length_mm": 500, "width_mm": 500, "quantity": 4}
    ]
  },
  "verified_by": "Johnson Andrew",
  "verified_at": "2026-09-06"
}
```

Every time the user manually corrects an extraction and generates a BOQ, prompt them: "Save this drawing's extraction as a training example?" → append to `extractors/examples/`.

---

## 6. Module skeleton — `extractors/qwen_vision.py`

Suggested public API (Claude Code should refine):

```python
def extract_from_pdf(pdf_path: Path, page_number: int = 0) -> ExtractionResult: ...

# Where ExtractionResult wraps:
#   title_block: TitleBlockExtraction (Pydantic)
#   pedestals: list[PedestalExtraction] (Pydantic)
#   raw_stage1_response: str  # for debugging
#   raw_stage2_response: str
#   confidence_notes: list[str]

def merge_into_project(project: Project, result: ExtractionResult) -> Project: ...
# Non-destructive merge: never overwrites existing values.
# If project.drawing_no is already set, extraction's drawing_no is ignored.
# Existing pedestals with matching tags are left alone.
```

---

## 7. New Streamlit page — `pages/0_Extract.py`

Suggested flow:

1. PDF file uploader (same as home page, or reuses `project.pdf_source_path` if already attached)
2. Page selector if multi-page PDF (default page 1)
3. Preview: show the rasterised page image
4. "Extract with Qwen" button → runs `extract_from_pdf`
5. Show extracted JSON in an expandable section
6. "Preview merge" — show which fields would be added to `project`
7. "Merge into project" button → updates `st.session_state.project`, redirects to `1_Input`
8. Bottom: link to save the extraction to the RAG library after user has verified it

**Never auto-merge.** Always require the user to click "Merge into project" after reviewing.

---

## 8. Verification workflow (user-facing)

The user's workflow with Phase 2 enabled:

1. Upload PDF on **0_Extract** page
2. Click **Extract with Qwen**
3. Review extracted JSON side-by-side with the drawing preview
4. If good → click **Merge into project**, go to **1_Input**, add anything Qwen missed (rebar, joints, embedments, sump), verify pedestal heights (Qwen usually can't get these)
5. Continue normal flow: 2_Review → 3_BOM → download
6. After downloading, prompt: **"Save this extraction as a training example?"** → adds to `extractors/examples/`

---

## 9. What NOT to build in Phase 2

- **No fine-tuning.** LoRA on a 7b vision model needs 24GB+ GPU and 100+ labelled examples. Skip entirely.
- **No auto-generation of BOQ from raw PDF without human review.** Every extraction is a starting draft, never final.
- **No changes to `core/*.py`** except adding `extractors/` as a new package. Phase 1 stays stable.
- **No cloud APIs.** Local Ollama only. User is offshore in KSA, latency and cost matter.

---

## 10. Success criteria for Phase 2

- Title block extraction ≥ 90% correct on 5 test Maaden drawings
- Pedestal extraction ≥ 60% correct on Maaden-style callouts, with 100% recall on obvious callouts (misses are worse than errors here since user reviews everything)
- Round-trip time under 30 seconds per drawing on M1/M2 MacBook Air
- Zero regressions in the 59 existing Phase 1 tests
- New extractor tests (mocked Ollama responses) added under `tests/test_qwen_vision.py`
- Example library reaches 10+ verified drawings before Phase 2 is called "done"

---

## 11. Handoff checklist for Claude Code

- [ ] Read this document
- [ ] Read `core/models.py` (target output schema)
- [ ] Read `pages/1_Input.py` (how the UI consumes `Project`)
- [ ] Verify Phase 1 tests still pass: `pytest tests/ -v`
- [ ] Install Ollama and pull `qwen2.5vl:7b`
- [ ] Build `extractors/pdf_to_image.py` (PyMuPDF rasteriser + optional crop)
- [ ] Build `extractors/qwen_vision.py` (two-stage prompt)
- [ ] Build `extractors/rag_examples.py` (JSON-based example store + retrieval)
- [ ] Add `extractors/models.py` (`TitleBlockExtraction`, `PedestalExtraction`, `ExtractionResult`)
- [ ] Add `pages/0_Extract.py` (Streamlit UI)
- [ ] Add `tests/test_qwen_vision.py` (mocked Ollama)
- [ ] Update this doc when done — mark completed items, note real-world accuracy numbers