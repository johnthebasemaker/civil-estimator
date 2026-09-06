# Civil Estimator

Standalone Streamlit tool for civil construction takeoff and BOQ generation from structural drawings.

**Current state: Phase 1 complete.** Structured input form → 10-sheet Excel workbook with formulas, wastage, and SAR costing.

**Next phase (in progress): Claude Code + local Qwen2.5-VL:7b vision model** to auto-extract pedestal callouts and title-block data from vector PDF drawings, pre-filling the input form for human review.

---

## Phase 1 architecture

### Stack
- Python 3.11+ · Streamlit · Pydantic v2 · openpyxl · SQLite
- No cloud services, no auth, runs on your laptop

### Repo layout

civil-estimator/
├── app.py # Home: project metadata, PDF attach, save/load
├── requirements.txt
├── core/
│ ├── models.py # Pydantic models for 12+ BOM element types
│ ├── formulas.py # Pure calc functions (volumes, formwork, rebar wt)
│ ├── bom_builder.py # Walks Project → produces BOM lines + BBS
│ ├── excel_writer.py # 10-sheet workbook writer (openpyxl)
│ ├── rate_library.py # SQLite rate persistence (Step 6)
│ └── filename.py # Output filename builder
├── extractors/
│ └── init.py # ← Phase 2 lands here (Qwen2.5-VL)
├── pages/ # Streamlit multi-page
│ ├── 1_Input.py # 14-tab element entry (data_editor grids)
│ ├── 2_Review.py # Live BOM preview, wastage overrides
│ ├── 3_BOM.py # KPIs, full BOM, Excel download
│ └── 4_Costing.py # Rate entry, SQLite persist, live totals
├── data/
│ ├── rebar_weights.json # IS 1786 kg/m by diameter
│ ├── wastage_defaults.json
│ └── rates.db # SQLite, created at runtime
├── output/
│ ├── uploads/ # Attached PDFs
│ ├── projects/ # Saved project JSON files
│ └── *.xlsx # Generated workbooks
├── tests/ # 59 pytest tests, all green
└── docs/
└── PHASE2_HANDOFF.md # ← Read this before 
starting Phase 2

### Excel output — 10 sheets
1. **Summary** — flat BOQ, all lines, wastage formulas in-cell
2. **Pedestals** — per-pedestal breakdown (P1–P7, concrete, formwork, rebar)
3. **Slab** — per-slab breakdown
4. **Sump** — per-sump breakdown
5. **Joints** — expansion/contraction/construction + accessories
6. **Coating** — epoxy areas × coats
7. **Embedments** — insert plates, anchor bolts, dowels
8. **Rebar_BBS** — bar-by-bar (coefficient rows + manual BBS rows)
9. **Assumptions** — editable wastage %, rebar unit weights (reference), source drawing filename, generated timestamp
10. **Costing** — rates × quantities, SUMIF subtotals by category, grand total

Every sheet's title block shows: project name, drawing no, revision, prepared-by, created timestamp (identical across all sheets in one workbook run).

### Output filename format
`{sanitised_drawing_no}_{YYYYMMDD}_{HHMM}.xlsx`
Example: `MD-522-8110-EG-CV-LAD-0107_20260906_1430.xlsx`

### BOM element types supported
1. Excavation (with soil type)
2. PCC / Blinding
3. Pedestals (rectangular, any grade, any coefficient)
4. Grade Slab
5. Sump / Pit (base + 4 walls)
6. Curb Wall / Dyke Wall
7. Formwork (auto-derived + loose entries)
8. Rebar — coefficient path (kg/m³) OR manual BBS bars (both allowed, warns on double-count)
9. HDPE Liner
10. Compacted Soil / Fill
11. Epoxy / Acid-Resistant Coating (with coats)
12. Joints (expansion, contraction, construction + accessories)
13. Embedments (insert plate, anchor bolt, dowel, sleeve)
14. Waterstop (standalone runs)
15. Sump Ancillaries (gratings, drain pipes, covers)

### Wastage defaults (editable per run)
- Concrete 3%, Rebar 5%, Formwork 10%, HDPE 8%, Epoxy 15%, PCC 5%, Soil 10%
- Earthwork excavation and Embedments = 0% (procured discrete)
- Joint accessories inherit joint length (0% wastage on run length)

---

## Run

```bash
git clone <repo-url>
cd civil-estimator
python -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

App opens at `http://localhost:8501`.

## Test

```bash
pytest tests/ -v
```

Expect 59 tests passing.

---

## Workflow (Phase 1)

1. **Home page** — enter project name, drawing no, revision, prepared-by. Attach the PDF drawing (reference only in Phase 1). Optionally save project state to JSON for cross-session persistence.
2. **1_Input** — 14 tabs, one per element type. Use `st.data_editor` grids to add/edit/delete rows.
3. **2_Review** — adjust wastage % if needed. See live BOM preview, warnings (e.g., rebar double-counting), and rollup totals.
4. **3_BOM** — see KPIs (total concrete/rebar/formwork/excavation), full BOM table, and rebar BBS. Click **Generate Excel BOQ** → download workbook.
5. **4_Costing** — enter unit rates per (category × unit). Rates persist in `data/rates.db` and pre-fill on future projects. Excel Costing sheet uses saved rates.

---

# Phase 2 — Handoff to Claude Code

**Objective:** wire a local Qwen2.5-VL:7b vision model (via Ollama) into the extractors layer so users can upload a drawing PDF and get pedestal callouts + title-block data pre-filled into the input form.

**Read `docs/PHASE2_HANDOFF.md` for full context, extraction targets, prompt strategy, and the RAG example library design.**

### Quick summary for Claude Code

- Phase 1 is untouched and shippable. Phase 2 adds an optional pre-fill layer.
- Target extraction: **title block (drawing no, revision, project name, date) at ~95% accuracy, pedestal callouts (P1, P2, dimensions, quantity) at ~60% accuracy on Maaden-style drawings.**
- Explicitly out of scope for Phase 2: rebar details, cross-section correlations, handwritten annotations, non-rectangular pedestals, concrete grades (usually on general notes drawing).
- Target extractor module: `extractors/qwen_vision.py`
- Model runtime: **Ollama**, endpoint `http://localhost:11434/api/generate`, model `qwen2.5vl:7b`
- Strategy: **prompt engineering + RAG example retrieval** (no fine-tuning — not practical on laptop)
- Output schema: partially-populated `Project` Pydantic instance, merged into `st.session_state.project`. User always reviews before saving.

### Suggested Claude Code first actions

1. Read `docs/PHASE2_HANDOFF.md` end to end
2. Read `core/models.py` — this is the output schema Qwen must map to
3. Read `pages/1_Input.py` — understand how the form consumes the Project model
4. Install Ollama + pull the model: `ollama pull qwen2.5vl:7b`
5. Build `extractors/qwen_vision.py` with the two-stage prompt: title block first (high confidence), then pedestal callouts (lower confidence, always requires review)
6. Add a new Streamlit page `pages/0_Extract.py` with: PDF upload → page selector → "Extract with Qwen" button → preview extracted JSON → "Merge into project" button
7. Build the RAG example library in `extractors/examples/` — one JSON per verified extraction from a real drawing

---

## Contributing / conventions

- **Zero regression rule**: any change that breaks an existing test needs explicit approval before commit
- **Targeted snippets only**: modify existing files with `str_replace`-style patches, never dump full files unless creating new ones
- **Git**: commits accumulate locally, never push without explicit approval
- **Concise formatting**: bullets over prose in docs and comments
- **Ask before executing**: no assumptions on architecture or scope

---

## Author

Built by Johnson Andrew (KSA) for MPC3P1-CNCEC project. Designed to slot into the broader GI_Hub_Project ERP later, but standalone-runnable today.