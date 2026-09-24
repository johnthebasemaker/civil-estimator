# Session handover — Civil Estimator

**Written 2026-09-24, after Phases 0, 1 and 2. The next session starts at
Phase 3 (priced set BOQ, history, revisions, sign-off).**

This is for whoever picks the project up with no memory of the sessions that
produced it. It records what changed, *why* it changed, the traps found the
hard way, and where Phase 2 plugs in.

---

## 1. Where the project stands

| | |
|---|---|
| Branch | `main` |
| Head | Phase 2 — the drawing viewer (#5) |
| Before it | `a4eb7f6` Phase 1 (#3), `0f40e23` Phase 0 (#2) |
| Tests | **701 passed, 0 failed, 0 skipped** — verified 2026-09-24 (~6.5 min) |
| Repository | `johnthebasemaker/civil-estimator`, **private** |

Both phases are merged. The working tree is clean and nothing is left running.

> **Two things are called "Phase 2" in this repo.** The README's historical
> phases describe how the product was built — Phase 1 was the typed input form,
> Phase 2 the local vision extraction, both long finished. The **roadmap**
> phases below (0, 1, 2, 3) are the UI and feature work agreed on 2026-09-22.
> "Phase 2" in this document always means the **drawing viewer**.

### About the test count

An earlier note put this at "561 passing tests". That was the figure on
2026-09-15, **before** this work. The real sequence:

| When | Result |
|---|---|
| 2026-09-15, after the last session | 561 passed |
| 2026-09-22, `main` as found at the start of Phase 0 | 535 passed, **18 failed, 4 errors**, 4 skipped |
| After Phase 0 | 629 passed, 0 failed, 0 skipped |
| After Phase 1 | 671 passed, 0 failed, 0 skipped |
| After Phase 2 (now) | **701 passed, 0 failed, 0 skipped** |

`main` was genuinely red when Phase 0 began. Every one of those 22 failures
had one cause: the tests find their drawing through the app's drawing list, the
uploads folder had been emptied, and the list could not see `Drawings/`. Phase 0
fixed the cause, which is why the count jumps.

The 561 → 701 difference is +68 new tests in Phase 0, +42 net in Phase 1 and
+30 in Phase 2. Phase 1 also *removed* the per-page gate tests for the four
pages it deleted.

### Running it

```bash
./bin/ce start        # the app and the worker together; ./bin/ce stop, status, logs
venv/bin/python -m pytest -q          # the full suite
venv/bin/python bin/worker.py --once  # drain the queue and exit
```

Use `venv/`, not `.venv/` — this project's dependencies are in `venv/`.

---

## 2. Phase 0 — the fixes (`0f40e23`)

Three things were broken in ways that defeated the app's own design.

### 2.1 The drawing list could not see `Drawings/`

**Symptom.** The set lives in `Drawings/`, but the list only scanned the uploads
folder and the project root. Once the uploads were cleared the app showed one
drawing while eleven sat beside it, and 22 tests went red.

**Fix.** `core/library.py` now owns discovery. It searches the uploads folder,
`Drawings/`, then the project root, in that order, and returns a `Drawing` per
file with its folder label, size and whether it may be deleted.

* **Each file is listed once**, de-duplicated by device and inode rather than by
  path text. On macOS `Drawings` and `drawings` are the same folder spelled two
  ways, and a symlink is the same file under another name; either listed every
  drawing twice.
* **Only uploads are deletable.** A drawing someone put in the project is not
  the app's to remove.
* **The same file name in two folders** gets the folder folded into its label,
  and only then — elsewhere it is noise.
* Both lists move by environment variable (§5), which is what lets the tests use
  their own folders and a server keep drawings on a mounted volume.

### 2.2 A drawing already read did not come back

**Symptom.** Open a drawing that had been read last week and the page said
"Local model unavailable" and disabled the button. Only the batch path consulted
the cache. This was exactly the case that matters when someone opens the app to
look at a drawing the team has already processed.

**Fix.** The drawing view is **cache first**:

1. the newest finished job's reading, **only if the file still has the bytes it
   was read from** — a drawing reissued under the same name is a different
   drawing and must never be shown last revision's numbers;
2. failing that, a saved reading under the chosen profile;
3. failing that, the Read button.

It says which it did: "Opened the saved reading of this drawing — no model time
used" versus "Read and saved". The distinction is real — the session remembers
whether *it* asked for the reading (`_reading_job`).

Fingerprints are hashed once per file version (`common.fingerprint`, cached on
path + mtime), not on every click.

### 2.3 The page ran the model itself

**Symptom.** The single-drawing view called `extract_from_pdf` **inside the
Streamlit process**. That is the freeze the queue and worker were built to
prevent: the tab was held for the whole read, the laptop heated, and the cache
was ignored.

**Fix.** The web app never reads a drawing. "Read this drawing" puts it on the
queue; the worker reads it; the page shows live progress, a Stop button, and
picks the result up when it lands. You can close the tab meanwhile.

`tests/test_pages_never_run_the_model.py` enforces this on the syntax tree —
no page, UI module or entry script may call `extract_from_pdf`, `generate` or
`chat`. A comment mentioning the extractor is fine; a call is not.

**Page numbers.** Jobs now carry a page, so multi-page PDFs still work through
the queue. The column is added to existing databases in place, and **page 1
hashes exactly as before**, so every extraction saved by an older build is still
found. Later pages get their own readable JSON (`_p2_extraction.json`) so they
cannot overwrite page 1's.

### 2.4 Plain language, and polish

* `ui/plain.py` translates problems into a sentence, with the raw message and
  the fix command inside a closed **Technical details** expander. A manager
  reading `<urlopen error [Errno 61] Connection refused>` learns nothing.
* The "nothing is reading the queue" warning waits 12 s. The worker polls every
  2 s, so an immediate warning flashed red on every healthy click.
* Streamlit's Deploy button and developer menu are hidden; crash messages are
  redacted in the browser (`.streamlit/config.toml`).
* Copy fixes: "No text blocks located" no longer appears beside "13 text blocks
  found"; nothing tells the user to go to a file called `1_Input`.

---

## 3. Phase 1 — the workspace (`a4eb7f6`)

### 3.1 What it replaced

Five pages, plus an Extract page **whose shape depended on how many drawings
were ticked**: none showed the queue, one showed a single drawing, two or more
showed a batch. Ticking a second drawing silently discarded the extraction you
were reviewing. The same step numbers meant different things in different modes.

### 3.2 The shape now

One page, four tabs, no page list in the sidebar:

| Tab | What it holds |
|---|---|
| 📄 Drawings | the set with status badges; or one drawing up close |
| ⏳ Queue | progress, pause, stop current, cancel waiting, retry failed |
| 🧾 BOQ | *Combined BOQ* and *Project estimate* (nested tabs) |
| 💰 Pricing | rates per (category × unit), totals |

`pages/0_Extract.py` is a 66-line shell — gate, header, session bar, tabs,
sidebar. It keeps its name because every launcher, test and saved link opens it.
The tabs are modules under `ui/workspace/`.

### 3.3 Ticking selects; Open opens

A tick now only ever **selects** drawings for a batch action (add to the queue,
delete an upload). **Open** shows one drawing, with ← All drawings, ‹ Previous
and Next ›.

The selection is kept in the session (`_picks`), not in the checkboxes, because
**Streamlit forgets a widget's value on any run that does not draw it** — and
the list is not drawn while a drawing is open. Kept only in the boxes, the
selection vanished every time someone opened a drawing and came back.

### 3.4 Status badges

Every drawing carries one: **Not read · Queued · Reading 40% · Needs review ·
Ready · Failed**, and the list summarises the set ("12 drawing(s) — 11 to
review · 1 ready").

* The rules are pure and tested without Streamlit: `core/drawing_status.py`.
* **Needs review** means read, with gaps or assumptions to check — the same
  count the workbook's `Gaps_and_Assumptions` sheet carries. On the current set
  11 of 12 show it, because pedestal heights are never printed on these drawings.
* Work in motion wins over a saved reading; a *failed* attempt wins too (showing
  "Ready" would hide that the re-read someone asked for did not happen); a
  *cancelled* attempt is no news.
* A live strip at the top of the list redraws the page once when a drawing
  changes state, so badges keep up without the whole list polling.

### 3.5 A drawing read before is ready the moment it is added

Adding a selection to the queue files any drawing with a saved reading as
**done, from cache**, immediately. That is bookkeeping, not reading: the result
already exists. It means the combined BOQ can use those drawings **without the
worker running** — verified in the live app with all 12 drawings and no worker.

### 3.6 Input, Review, BOM and Costing folded in

* **BOQ → Project estimate** is what Input, Review and BOM were, in one
  sequence: **1 · Elements** (14 types in four groups, plus the derivation
  rules) → **2 · Wastage** → **3 · Check the bill** (warnings, totals, the full
  bill, rollups, BBS) → **4 · Download**.
* **Pricing** is what Costing was.
* `pages/1_Input.py`, `2_Review.py`, `3_BOM.py` and `4_Costing.py` are deleted.

### 3.7 The rest

* **Button hierarchy.** The filled, coloured button is always the next step of
  the work — Add to the queue, Read this drawing, Generate. Clear session and
  every confirmation are outlined. A test enforces it.
* **Full-width layout**, badge colours, and the old per-page step chips removed
  (the tab bar is the navigation).
* **Each tab runs inside a guard** (`common.guarded`). A failure is reported in
  words *in that tab*, with the traceback under Technical details and in the app
  log, and the other three keep working. `CIVIL_ESTIMATOR_STRICT=1` — set for
  every test — re-raises instead, so a bug is never a quiet pass.

### 3.8 Bugs found and fixed while building it

* Review-grid edits carried onto the next drawing you opened (pending row edits
  were applied to a different drawing's rows).
* Loading a saved project was overwritten by stale values left in the estimate's
  widgets.
* `enqueue` treated `Drawings/x.pdf` and its absolute spelling as two drawings.
* Several tests wrote into the **real** `output/`: one blanked drawing 0107's
  readable extraction on every run, others overwrote `SET_BOQ.xlsx`. Proven
  fixed by snapshotting all 142 files under `output/`, `data/`, `Drawings/` and
  `extractors/examples/` before and after a full run — nothing changed.

---

## 4. The map

| Path | What it is |
|---|---|
| `pages/0_Extract.py` | the workspace shell (gate, header, session bar, tabs, sidebar) |
| `ui/workspace/common.py` | session, Clear session, save/load, model health, the per-tab guard |
| `ui/workspace/drawings_view.py` | the list, badges, upload, delete; and one drawing: sheet → read → review → BOQ |
| `ui/workspace/viewer.py` | the evidence overlay, provenance colours and the redrawn trace |
| `ui/workspace/queue_view.py` | the queue fragment and its controls |
| `ui/workspace/boq_view.py` | combined bill; project estimate (was Input/Review/BOM) |
| `ui/workspace/pricing_view.py` | rates and totals (was Costing) |
| `ui/plain.py` | problems in words; raw detail behind an expander |
| `ui/kit.py` | theme, brand header, readiness panel, badge CSS, login gate |
| `core/library.py` | where drawings are found |
| `core/drawing_status.py` | the badge rules (pure) |
| `core/jobstore.py` | the SQLite queue: claim, heartbeat, cancel, page, fingerprint |
| `core/extract_cache.py` | readings keyed by content hash |
| `core/search.py` | the line-item search rule (was `tests/helpers_search.py`) |
| `bin/worker.py` | the only thing that runs the model |
| `bin/ce` | one command for app + worker |

---

## 5. Environment variables

| Variable | Default | Moves |
|---|---|---|
| `CIVIL_ESTIMATOR_JOBS_DB` | `data/jobs.db` | the queue |
| `CIVIL_ESTIMATOR_CACHE_DIR` | `output/cache` | saved readings |
| `CIVIL_ESTIMATOR_UPLOAD_DIR` | `output/uploads` | where uploads are written |
| `CIVIL_ESTIMATOR_DRAWING_DIRS` | `Drawings:.` | the other folders searched |
| `CIVIL_ESTIMATOR_CLASSIFICATIONS` | `output/classifications.json` | Green/Brown/Repair filing |
| `CIVIL_ESTIMATOR_SECRETS` | `.streamlit/secrets.toml` | the password hash |
| `CIVIL_ESTIMATOR_STRICT` | unset | tests only: a failing tab re-raises |

The app and the worker must agree on the first two or nothing moves.

---

## 6. Traps worth knowing before touching the UI

Each of these cost real debugging time.

* **A tab label is a tab's identity.** Put a live count in one and Streamlit
  snaps the view back to the first tab whenever the count changes. The labels
  are fixed, and a test asserts it.
* **Widget state disappears on any run that does not draw the widget.** That is
  why the drawing selection lives in the session.
* **A widget's `value=` is part of its identity.** Passing the selection as
  `value=` made a *new* checkbox whenever the selection changed, so unticking
  was undone on the next rerun — the original bug in `test_drawing_library.py`,
  re-introduced and caught. Seed the widget's key instead, only when absent.
* **`st.stop()` inside a tab ends the whole script**, so every later tab
  silently disappears. Views `return`. A test walks the syntax tree to enforce
  it. (`st.rerun()` and `st.stop()` are `BaseException`s, so the per-tab guard
  does not swallow them.)
* **In AppTest, `button.type` is the element type** (`"button"`). The style is
  `button.proto.type`. An assertion on `.type` passes without testing anything.
* **Never call the model from the page.** The worker does that.
* **Do not change the existing Excel sheets.** Their structure is fixed by
  agreement with the user; new output goes on new sheets (that is how the
  Location Based Report was added).
* **Tests must not touch real data.** `tests/conftest.py` sandboxes the
  classification store and sets strict mode for every test; page-test fixtures
  redirect the queue, cache, uploads, drawing folders and `OUTPUT_DIR`.

---

## 7. Left for the user (not done, deliberately)

* `output/SET_BOQ.xlsx` was overwritten by a test run on 2026-09-22 and is now
  a 3-drawing test artefact. Rebuild it from the saved readings, or rebuild it
  in the BOQ tab if the original was a hand-picked selection:

  ```bash
  venv/bin/python bin/rebuild_set.py
  ```

* Six leftover test workbooks:

  ```bash
  rm output/MD-522-8110-EG-CV-LAD-0107_20260922_*.xlsx
  ```

* Drawing **0103** is filed as *Brown Field*; it was *Green Field* on
  2026-09-15. No test touches 0103, so it was most likely changed in the app.
  Worth confirming.
* The merged branches `feat/ce-phase0-fixes` and `feat/ce-phase1-workspace` are
  still on GitHub.
* Drawing 0107's readable extraction was restored from the cache after a test
  blanked it; the cache copy was always intact.

### Hosting, for reference

The app is served from the Mac through a Cloudflare quick tunnel. The
`--config` flag matters: without it, cloudflared reads `~/.cloudflared/config.yml`
(the unrelated gi-hub tunnel) whose catch-all returns 404, and the link shows a
blank page.

```bash
cloudflared tunnel --config ~/.cloudflared/empty.yml --no-autoupdate --url http://localhost:8501
```

Quick-tunnel hostnames are ephemeral — one expired while the local process still
looked healthy. A fixed host (the Hetzner plan in `docs/DEPLOYMENT.md`) is the
real answer if management needs this regularly.

---

## 8. Phase 2 — the drawing viewer (delivered)

Agreed scope was: **zoom and pan, an evidence overlay, click-to-trace both ways,
and colours by source.** What shipped, and the one deliberate departure:

* **Evidence overlay** — open a read drawing and the sheet carries a numbered,
  coloured box on every located value (`ui/workspace/viewer.py`).
* **Colours are provenance, not confidence** — green: read from the sheet's
  text; navy: read by the model; amber: read, but the drawing left a figure out.
  The first draft called the amber ones "assumed", which libelled dimensions
  that had been read correctly when only the *height* was missing.
* **Trace, by redrawing rather than zooming** — pick a row and the region is
  rendered again from the PDF's vectors at 1400 px, so it sharpens as you go in.
  Tight / Normal / Wide controls the surrounding context.
* **One-way tracing, deliberately.** Row → sheet works; clicking a box to select
  its row does not. It would need Plotly or a custom component, and `AppTest`
  cannot see a Plotly chart — every such interaction would be untestable in the
  harness this project's 600-plus tests rely on. The box numbers carry the other
  direction: box 7 is row 7 in the table and row #7 on the workbook's
  Verification sheet. **No new dependency; still seven packages.**
* **Bug found and fixed on the way:** the check print numbered only the boxed
  items while the Verification sheet numbered all of them, so box "1." on the
  print was row "#6" in the workbook — the two artefacts a checker holds side by
  side disagreed.

### If the one-way limit ever bites

Clicking the sheet needs a component. Before adding one, weigh it against the
test harness: anything `AppTest` cannot see is a feature that cannot be
regression-tested here. `st.plotly_chart(on_select=…)` does exist in Streamlit
1.39, so the path is open if the trade is worth making.

## 8b. What Phase 2 was built on

Most of the data work already existed — useful to know for Phase 3 too.

* **Positions are already extracted.** `PedestalExtraction.source_rect`
  (`extractors/models.py:84`) and `TranscriptBlock.rect_norm`
  (`extractors/models.py:115`) are `(x0, y0, x1, y1)` as **page fractions**.
  Discovery rows carry `source_rect` too.
* **`extractors/verification.py` already assembles exactly what the overlay
  needs.** `collect_items(result, grid) -> list[CheckItem]`, where `CheckItem`
  is `kind, label, extracted, callout, grid_ref, confidence, rect_norm, note`.
  That is one row per thing a person should verify, with its rectangle.
* **`build_check_print(page, result, ...)`** already draws those boxes onto the
  sheet as a PNG. The interactive overlay is the same idea, on screen.
* **`sheet_grid.detect_grid(page)`** gives the drawing's own grid references
  (e.g. D-7).
* **`drawings_view.sheet_analysis()`** already renders and caches the sheet PNG
  (1500 px) keyed by the file's modification time. The viewer should reuse or
  extend it rather than re-rendering per interaction.

### Where it landed

`ui/workspace/drawings_view.py`: section **1 · Sheet** now shows the plain
preview until a drawing has been read and the evidence viewer afterwards
(`_evidence`, `_overlay`, `_trace`). Loading a saved reading moved ahead of the
sheet (`_load_reading`) so section 1 knows whether it is drawing a picture or
drawing evidence.

### How the decision went

The choice was between a new dependency and the tools already here:

| Option | Gets | Costs |
|---|---|---|
| Plotly image + shapes | mouse zoom and pan, click a box to select its row | an eighth dependency, and **`AppTest` cannot see a Plotly chart**, so none of it could be regression-tested |
| PyMuPDF + `st.dataframe` selection *(chosen)* | any region redrawn from the vectors at any size, native row selection that `AppTest` can drive, no new dependency | tracing runs one way; zoom by control rather than by mouse |

The harness settled it. A feature the 600-plus-test suite cannot see is a
feature that will quietly break.

### How it was verified

* An A0 sheet can be read: the traced region is redrawn from the PDF at
  1400 px, so a callout six pixels tall on the full sheet is legible.
* Every located value is boxed and coloured by provenance; values with no
  single place on the sheet are listed and marked as such.
* A row traces to its box (`tests/test_workspace.py::TestTheDrawingViewer`);
  the numbers match the workbook (`tests/test_viewer.py::TestNumbering`).
* The syntax-tree guards still pass — no page runs the model, no view calls
  `st.stop()` — and a full run still changes none of the real files.

---

## 9. Phase 3 backlog (agreed, not started)

* Priced combined BOQ — as a **new** linked sheet; existing sheets stay as they
  are.
* BOQ history: when each was built, from which drawings, the total; compare two.
* Revision tracking: `_C01` → `_C03` of the same drawing, and what changed.
* Review sign-off: mark lines checked, with a comment.
* A one-page management PDF beside the Excel.
* Later, with hosting: named users and an audit trail.
