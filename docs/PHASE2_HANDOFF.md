# Phase 2 Handoff — Qwen2.5-VL:7b Vision Extraction

**Audience:** Claude Code, working on the `civil-estimator` repo after Phase 1 (Steps 1–6) has been completed and merged.

**Reader assumption:** You have full repo access and can read `core/models.py`, `pages/1_Input.py`, `pages/2_Review.py`. If you haven't, do that first.

---

## 1. What Phase 1 already built (context)

- **Structured input model** (`core/models.py`): a Pydantic `Project` class with 15 list fields (`pedestals`, `grade_slabs`, `sumps`, `joints`, `excavations`, etc.). Each list item is a strongly-typed model like `Pedestal(tag, length_m, width_m, height_m, quantity, grade, rebar_coefficient_kg_per_m3)`.
- **Calc engine** (`core/formulas.py`): pure functions that compute volume, formwork area, rebar weight per element. Never touch these — they're tested and stable.
- **BOM builder** (`core/bom_builder.py`): walks a `Project` and produces a flat BOM + rebar BBS. Never touch this.
- **Excel writer** (`core/excel_writer.py`): 10-sheet workbook. Never touch this.
- **Streamlit UI**: `Home.py` + 5 pages. The relevant integration point is `st.session_state.project`, which holds the live `Project` instance across all pages.

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

- [x] Read this document
- [x] Read `core/models.py` (target output schema)
- [x] Read `pages/1_Input.py` (how the UI consumes `Project`)
- [x] Verify Phase 1 tests still pass — 59/59 green, unchanged
- [x] Ollama running, `qwen2.5vl:7b` pulled
- [x] `extractors/pdf_to_image.py` — rasteriser, region crops, tile sweep, dHash
- [x] `extractors/qwen_vision.py` — three-stage orchestration + merge
- [x] `extractors/rag_examples.py` — JSON example store + dHash/prefix retrieval
- [x] `extractors/models.py` — `TitleBlockExtraction`, `PedestalExtraction`,
      `GradeSlabExtraction`, `RegionResponse`, `ExtractionResult`, `MergePlan`
- [x] `pages/0_Extract.py` — Streamlit UI, never auto-merges
- [x] `tests/test_qwen_vision.py` — 105 tests, Ollama fully mocked
- [x] Update this doc — see §12

Added beyond the plan:

- [x] `extractors/callout_grammar.py` — deterministic regex re-parsing +
      plausibility gates (see §12.3)
- [x] `extractors/prompts.py` — the three stage prompts, few-shot injection
- [x] `extractors/ollama_client.py` — stdlib HTTP client, JSON salvage, retries
- [x] `run_pipeline.py` — headless PDF → workbook CLI, stamped UNVERIFIED DRAFT

---

## 12. Phase 2 as built — corrections to this document

Everything below was measured on **MD-522-8110-EG-CV-LAD-0107 Rev C01** with
`qwen2.5vl:7b` on an M-series MacBook Air. Where reality contradicted the plan,
the plan is marked ❌ and the reason given.

### 12.1 §5 Stage 2 geometry was wrong ❌

> "Send the **full drawing** (or top-half where the plan lives)."

The pedestal callouts are **not in the plan**. They sit under the section
details, spread from y≈0.20 to y≈0.60 of the sheet. A top-half crop misses P6
and P7 entirely. The full-drawing option is worse still — see 12.2.

**As built:** an overlapping tile sweep that assumes nothing about where the
draughtsman put the callouts. `thorough` = 6x4 = 24 tiles over the whole sheet;
`fast` = 6x3 = 18 tiles over the detail band only.

### 12.2 §4 resolution guidance was incomplete ❌

> "Suggested resolution: 2000px on longest edge."

Sizing by longest edge is the wrong control, and 2000 px on a full A0 reads
nothing. Two separate effects:

| Image sent | Pixels | Prompt tokens | Time | Pedestals found |
|---|---|---|---|---|
| Full sheet @ 2000 px | 2.83 MP | 2865 | 346 s | **0 of 5** |
| Square tile | 1.20 MP | 1902 | 318 s | 2 of 2 in tile |
| Wide tile / strip | 0.95–1.01 MP | 1520–1606 | **47–51 s** | 3 of 3 in strip |

* **Latency cliff between ~1600 and ~1900 prompt tokens.** 1.3x the tokens costs
  6x the time — the model stops fitting comfortably in memory. Cap every image
  by *total pixels* (`tile_max_pixels`, default 0.95 MP), never longest edge:
  a near-square tile sized by longest edge produces ~4x the pixels of a wide
  strip sized identically.
* **Resolution floor around 4100–4400 effective sheet-px.** Below it the model
  returns empty tiles for callouts that are plainly on the sheet. Downsampling
  an A0 to 2000 px puts a 3 mm callout under two pixels of stroke height.

These pull against each other; the resolution is *more, smaller* tiles. Both
bounds are asserted in `tests/test_qwen_vision.py::TestSweepBudget`, so retuning
the grid cannot silently break either.

A third constraint, found the same way (§12.5): **tile area must be bounded
too**, independently of pixels and resolution.

### 12.3 The model should transcribe, not parse ✚

Not in the original plan, and the single biggest precision win. Every callout
prompt makes the model echo the verbatim callout into `raw_text`;
`callout_grammar.py` then re-derives tag/length/width/quantity from that string
by regex. Where the model's own field split disagrees with the string it just
copied, the string wins and the disagreement is logged.

A 7b vision model is a good OCR and a poor field-splitter — it transposes
`500x350` or reads `11 Nos` as `1`. It very rarely misreads the *string*.

Values then pass a plausibility envelope (150–3000 mm, 1–999 Nos, tag
`^P\d{1,2}$`). That envelope is what rejects `TYP DETAIL OF FOUNDATION F1`,
which is on this sheet directly beside the pedestal details.

### 12.4 Prompt prohibitions collapse recall ❌ (the big one)

The first full sweep found **0 of 5** pedestals across 20 tiles — while the
title block and grade slab came back perfect. The tiles were not the problem;
the crops were crisp and the callouts plainly visible in them.

The cause was three "be careful" rules I had added to the Stage 2 prompt:

```
Ignore FOUNDATION callouts (e.g. "TYP DETAIL OF FOUNDATION F1") ...
Ignore plain position marks in the plan view that have no dimensions.
If this image contains no pedestal callouts, return exactly {"pedestals": []}.
```

A/B on the *identical* tile image, same model, same options:

| Prompt | Result |
|---|---|
| With the three rules | `{"pedestals": []}` |
| Without them | both callouts, correct, in 7 s |

A 7b model over-applies prohibitions: give it several reasons to withhold plus a
cheap null answer, and the null answer wins. This is invisible in testing unless
you check against a sheet you have read yourself — the pipeline reports a clean,
confident, empty result.

**Rule: recall belongs to the model, precision belongs to the parser.** The
filtering those rules attempted is real and still happens, in
`callout_grammar.py`, deterministically and under test — `TYP DETAIL OF
FOUNDATION F1` fails the callout grammar, a bare plan mark `P2` fails it, and
implausible values fail the envelope. None of that needs to be in the prompt.

`extractors/prompts.py::SUPPRESSIVE_PHRASES` records the exact wording and
`tests/test_qwen_vision.py::TestPromptRegressions` fails if any of it returns.

### 12.5 Competing dense text starves a lone callout ✚

After fixing the prompt (§12.4) a 4x5 sweep still found only **2 of 5**. The two
it found, P1 and P2, shared one tile. Each miss sat alone in a tile with a large
block of unrelated text — P3's tile also contained the sheet's 13-line GENERAL
NOTES block, which fills half the image. The callout was crisply legible; the
model read the notes, found no callout pattern among them, and returned empty.

Edge distance did *not* explain it — all five callouts sat 13–16% from the
nearest tile edge, and two of those were found.

Two knobs, both now in `ExtractionConfig`:

* **Smaller tiles** (6x4 rather than 4x5), so the notes block, the key plan and
  the title block land in tiles of their own rather than sharing one with a
  callout. `TestSweepBudget` caps tile area at 8% of the sheet.
* **Overlap 0.15, up from 0.06**, so a callout near a seam appears meaningfully
  in more than one tile. Under the 6x4 grid, P1/P2/P3 each fall inside 2–4
  tiles instead of exactly one.

Smaller tiles are close to free: **cost per call is flat below the latency
cliff** — every sub-cliff call measured 45–55 s whether it carried 0.95 MP or
1.01 MP. The price of a finer grid is only the extra calls, and it buys both
resolution and isolation.

### 12.6 Superseded: locate the text with geometry, then read only the text ✚

§12.1–12.5 describe tuning a *blind tile sweep*. That whole approach is now the
fallback (`profile="sweep"`), because a better one exists.

The text on this sheet is outlined to curves, but an outlined glyph is still a
small compact path sitting in a row of similar paths. Clustering those paths
locates **every text block on an A0 in about one second, with no model calls**
(`extractors/vector_text.py`). Candidate callout blocks are then cropped, scaled
so their glyphs are ~22 px tall, and packed two-column into montage images.

That inverts the economics. The blind sweep spends most of its calls looking at
empty paper and linework; the montage strategy sends only text:

| Strategy | Model calls | Time | Pedestals |
|---|---|---|---|
| Blind sweep, prompt bug (§12.4) | 22 | 1181 s | 0/5 |
| Blind sweep, fixed | 26 | 1258 s | 3/5 |
| **Montage + split-on-truncation** | **10** | **548 s** | **5/5** |

Three things made it work:

* **Cost is per call, not per pixel** below the latency cliff — every sub-cliff
  call measured 45–55 s regardless of size. So packing many crops into one image
  is nearly free, and fewer calls is the only lever that matters.
* **Tell the model how many crops it is looking at.** Without "{n} crops …
  left column first" it transcribed the first crop and stopped (3 of 25 lines).
  With it, all 9 crops came back — and in 15 s rather than 43 s.
* **Retry on truncation.** The model still sometimes stops early, but that is
  now *detectable*: fewer entries returned than crops sent. A short montage is
  re-sent split in half, bounded at two splits. This is precisely what recovered
  P2 and took recall from 4/5 to 5/5.

Because the model now does pure OCR, interpretation moved wholly into
`callout_grammar.scan_lines`, which applies separate grammars for pedestals,
curb walls, sumps, levels, insert plates, epoxy specs, rebar callouts and
thickness notes. **A new element type costs zero extra model calls.** Types the
drawing does not fully dimension are surfaced for review, never merged.

If a sheet is a true scan with no vector text, `find_text_blocks` returns
nothing and the UI says so — that is what `profile="sweep"` is still for.

### 12.7 Measured accuracy — MD-522-8110-EG-CV-LAD-0107 Rev C01

`thorough` (montage) profile, 10 model calls, 548 s.

| Target | Result | Handoff goal | |
|---|---|---|---|
| Title block (drawing no, rev, date) | **3/3 = 100%** | ≥ 90% | MET |
| Project name, drawn-by | correct | — | |
| Pedestal callouts | **5/5 = 100% recall, 100% precision** | ≥ 60% | MET |
| — 100% recall on obvious callouts | **5/5** | 100% | **MET** |
| Grade slab 24430 × 8600 × 300 mm | exact | — | MET |
| Round trip | 548 s | < 30 s | **NOT MET** (§12.8) |
| Phase 1 regressions | 0 (59/59 green) | 0 | MET |
| New tests | 122, model mocked, page executed headlessly | some | MET |
| Example library | 1 drawing | 10+ | in progress |

Zero false positives in any run across all four strategies tried.

Additionally read and surfaced for review (not merged, because the drawing does
not print enough to build the objects): 1 insert plate type, 1 epoxy spec,
3 rebar callouts.

**How it got there**, same drawing and model throughout:

| Run | Strategy | Calls | Time | Pedestals |
|---|---|---|---|---|
| 1 | blind sweep, prompt carrying prohibitions | 22 | 1181 s | 0/5 |
| 2 | blind sweep, prompt fixed (§12.4) | 22 | 1056 s | 2/5 |
| 3 | blind sweep, finer grid (§12.5) | 26 | 1258 s | 3/5 |
| 4 | **montage + split-on-truncation (§12.6)** | **10** | **548 s** | **5/5** |

Each step was diagnosed from evidence, not guessed: run 1 by A/B-ing the prompt
on one tile, run 2 by correlating misses with tile coverage, run 3 by looking at
the failing tile and finding a 13-line notes block beside the callout, run 4 by
noticing that cost is per call rather than per pixel.

### 12.8 §10 "round-trip under 30 seconds" is not achievable ❌

~48 s per model call is the floor on this hardware, and that is for one image.
The montage strategy needs 6 calls on a clean run and 10 when truncation forces
retries — about 9 minutes. The blind `sweep` fallback needs 26 (~21 min).

Locating the text costs no model time at all (~1 s of geometry), and a PDF that
kept its text layer skips Stage 2 entirely. But a single vision call is ~48 s,
and the sheet needs at least a title-block read, so ~2 minutes is the practical
floor for any drawing that must be looked at. 30 s is not reachable on this
hardware.

The only configuration that could hit 30 s is a single full-sheet call, which
reads **zero** callouts. Recall was chosen over speed on the strength of this
document's own rule: *"misses are worse than errors here since user reviews
everything."*

### 12.9 §3 pedestal height vs. the Phase 1 schema ⚠

> "Pedestal height: only if it appears in the same callout string; otherwise
> leave blank for human entry."

`core.models.Pedestal.height_m` is a required `PositiveFloat` — blank is not
representable, and `core/*.py` is off-limits.

**As built:** a callout carrying a third dimension (`P4(600x500x900)`) is used
directly. Otherwise the merge inserts a **0.800 m placeholder** (matching
`output/sample_boq.xlsx`) and declares it three ways: a confidence note, a
merge-plan warning, and a red WARNINGS row at the top of the Excel Summary
sheet. Concrete, formwork and rebar for those pedestals are wrong until a human
enters the real heights in `1_Input`.

### 12.10 §7/§9 review gate vs. an end-to-end pipeline ⚠

`pages/0_Extract.py` honours "never auto-merge" exactly: preview → merge plan →
explicit button.

`run_pipeline.py` additionally offers the headless PDF → workbook run. It cannot
produce a signed BOQ: every workbook opens with an **UNVERIFIED DRAFT** banner
listing each extracted value with its verbatim callout and confidence, a sidecar
`output/<pdf>_extraction.json` records every model call, and `--audit-sheet`
appends a full `Extraction_Log` sheet. The 10 mandated sheets are untouched —
provenance rides on `BOM.warnings`, which `core/excel_writer.py` already renders.

### 12.11 The reference drawing has no text layer ✚

83,981 vector paths, **zero extractable characters** — the text is outlined to
curves. `pdfplumber` returns an empty string. Vision extraction is not the
preferred route here, it is the only one. `page_info()` reports this, and the
Extract page tells the user.

### 12.12 Dependencies ✚

Phase 2 adds **none**. The Ollama client is stdlib `urllib`; the perceptual hash
is a pure-Python dHash; rasterising uses PyMuPDF, already in `requirements.txt`.

---

## 13. Locating a value on the sheet (check print / grid references)

Extracting a number is only half of what a checker needs; the other half is
*where it is*. Three approaches were tried and measured:

| Approach | Result |
|---|---|
| transcript entry *i* ⇒ crop *i* | wrong — the model returned the top-right crop of a two-column montage first |
| same, but only when counts match | still wrong, same cause |
| print an index on each crop, ask for it back | better, but the model repeats a callout under several ids |

The model reported one P1 callout under crop **1** and crop **5** of the same
montage, and reported it again on a montage that does not contain P1. So any
single sighting is a weak claim about location.

**Resolution:** locations are voted on across sightings. Majority wins; a tie
withholds the location and records why. On the reference sheet: 4 of 5 pedestals
located correctly (verified visually against the check print), P1 deliberately
unlocated.

The principle is the same one that governs the rest of this extractor — a wrong
answer delivered confidently is worse than a missing one, because only the
missing one gets checked.

## 14. Environment note — which Python runs the app

`streamlit` on this machine's PATH is the system framework Python 3.12
(Streamlit 1.58) and does **not** have PyMuPDF. The project's dependencies live
in `./venv` (Streamlit 1.39). Running `streamlit run Home.py` therefore imports
the Extract page with no `fitz` and dies at render time:

    ModuleNotFoundError: No module named 'fitz'

Phase 1 pages never imported `fitz`, which is why this only appeared in Phase 2.

Fixed two ways:

* `bin/app.sh` launches `venv/bin/streamlit`, and warns if Ollama is not up.
* `extractors/st_compat.py` resolves the arguments that were renamed between
  1.39 and 1.58 (`st.image(use_column_width=)` → `use_container_width=`), so the
  pages render on either. `tests/test_extract_page.py` executes the page
  headlessly with `AppTest` against whichever Streamlit is installed — these are
  render-time failures that no linter catches.
