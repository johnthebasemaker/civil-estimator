"""Drawing → BOQ, on one page.

Upload a drawing PDF, read it with the local vision model, correct what it got
wrong, download the Excel workbook. The other pages stay available for manual
entry and detailed work, but nothing here requires leaving this page.

Two rules from the project's safety design are kept:
  * nothing is merged into the live Project until the user presses a button;
  * pedestal heights are not on the drawing, so the grid below asks for them
    before a workbook can be called finished.
"""
from __future__ import annotations

# Repair sys.path before anything heavy is imported: `streamlit run Home.py`
# uses the framework Python, which has no PyMuPDF. See sitepath.py.
import sitepath  # noqa: F401  (import first — it fixes the import path)

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from ui import kit

# PyMuPDF is the one dependency the framework Python does not carry, and this
# is the page that needs it. sitepath has already tried to supply it from the
# venv; if it still is not importable the honest thing is a repair instruction,
# not a traceback in the middle of a drawing review.
try:
    from core.bom_builder import build_bom
    from core.excel_writer import write_workbook
    from core.filename import build_output_path
    from core.models import GradeSlab, Pedestal, Project
    from extractors import pdf_to_image as R
    from extractors import qwen_vision as QV
    from extractors import rag_examples
    from extractors import st_compat as SC
    from extractors import vector_text as VT
    from extractors.models import ExtractionResult
    from extractors.ollama_client import OllamaClient
    from extractors.qwen_vision import ExtractionConfig, PROFILES
    from extractors import sheet_grid as SG
    from extractors import verification as VER
    from extractors import workbook_extras as WE
    from core.derivation import (
        apply_derived, default_rules, derive, rules_from_findings,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - environment repair path
    st.set_page_config(page_title="Civil Estimator", layout="wide")
    st.error(f"This page cannot start: {exc}")
    st.code(sitepath.explain() or f"Install {exc.name} and restart the app.",
            language="text")
    st.stop()

UPLOAD_DIR = Path("output/uploads")
OUTPUT_DIR = Path("output")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Drawing → BOQ", layout="wide", page_icon=kit.favicon())

# Each page is its own script, so each carries the gate: a login on the
# home page alone would be bypassed by navigating straight to a page URL.
kit.require_login()

if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")
project: Project = st.session_state.project

kit.page_header("Drawing → BOQ", project,
                "Upload a drawing, read it with the local model, correct it, "
                "download the workbook.", step="Drawing → BOQ")

CONCRETE_GRADES = ["PCC_M15", "RCC_M25", "RCC_M30", "RCC_M40"]


# ======================================================================
# 1 · Drawing
# ======================================================================
# Checked once, before anything that might need it: the batch panel in section 1
# runs the model directly, so the health result cannot live further down.
client = OllamaClient()
ok, msg = client.health()


def _rule_toggles(rules, *, key_prefix: str, disabled: bool = False) -> None:
    """Derivation rule checkboxes with select-all / clear.

    Same widget-driving trick as the drawing list: a button writes the checkbox
    keys, then reruns.
    """
    s1, s2, _ = st.columns([1, 1, 4])
    if s1.button("Select all", key=f"{key_prefix}_all", use_container_width=True,
                 disabled=disabled):
        for r in rules:
            st.session_state[f"{key_prefix}{r.key}"] = True
        st.rerun()
    if s2.button("Clear", key=f"{key_prefix}_none", use_container_width=True,
                 disabled=disabled):
        for r in rules:
            st.session_state[f"{key_prefix}{r.key}"] = False
        st.rerun()

    cols = st.columns(4)
    for i, r in enumerate(rules):
        with cols[i % 4]:
            r.enabled = st.checkbox(r.label, key=f"{key_prefix}{r.key}",
                                    help=r.formula, disabled=disabled)


def _batch_panel(drawings: list[Path]) -> None:
    """Run several drawings end to end and roll the quantities up.

    Deliberately separate from the single-drawing flow above: that flow exists
    so a person can correct what the model read before anything is written, and
    there is no honest way to offer that for twenty sheets at once. A batch is
    therefore explicitly a first pass — every workbook it writes still carries
    the UNVERIFIED DRAFT banner and its own Verification sheet to be marked up.
    """
    st.header("2 · Batch run")
    st.caption(f"{len(drawings)} drawings ticked. Each is extracted, written to "
               f"its own workbook, and summed into a roll-up.")

    minutes = len(drawings) * 5
    st.warning(f"This takes roughly **{minutes} minutes** "
               f"(~5 min per A0 sheet on this machine). The tab must stay open.",
               icon="⏱")

    st.markdown("**Derive the quantities the drawings do not print**")
    if "batch_rules" not in st.session_state:
        st.session_state.batch_rules = default_rules()
    _rule_toggles(st.session_state.batch_rules, key_prefix="br_")

    o1, o2 = st.columns(2)
    with o1:
        want_audit = st.checkbox("Extraction_Log sheet", value=True, key="b_audit")
    with o2:
        want_check = st.checkbox("Check print per drawing", value=True,
                                 key="b_check")

    if not st.button(f"🧾 Run {len(drawings)} drawings", type="primary",
                     use_container_width=True, disabled=not ok):
        return

    import run_pipeline as RP

    bar = st.progress(0.0, text="starting…")
    log = st.container()
    outcomes: list = []

    for i, pdf_path in enumerate(drawings):
        bar.progress(i / len(drawings), text=f"[{i + 1}/{len(drawings)}] {pdf_path.name}")
        try:
            res = QV.extract_from_pdf(pdf_path, client=client)
            proj = Project(project_name="", drawing_no="")
            plan = QV.plan_merge(proj, res)
            QV.merge_into_project(proj, res)

            items = derive(proj, st.session_state.batch_rules)
            if items:
                apply_derived(proj, items)

            if not proj.drawing_no:
                from core.filename import sanitise
                proj.drawing_no = sanitise(pdf_path.stem)

            if not (proj.pedestals or proj.grade_slabs):
                # Still belongs in the set: a sections sheet carries no
                # quantities, a plan sheet marks positions and references sizes
                # elsewhere. Both contribute what they have.
                marks = ", ".join(f"{k}x{v}" for k, v in res.position_marks.items())
                note = "no BOQ quantities" + (f"; marks {marks}" if marks else "")
                outcomes.append(RP.RunOutcome(pdf_path, True, proj, res,
                                              notes=[note]))
                log.warning(f"{pdf_path.name} — {note}")
                continue

            proj.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            bom = build_bom(proj)
            bom.warnings = QV.extraction_warnings(res, plan) + bom.warnings
            written = write_workbook(proj, bom,
                                     build_output_path(proj.drawing_no, OUTPUT_DIR))

            doc_g, page_g = R.open_page(pdf_path, 0)
            try:
                grid = SG.detect_grid(page_g)
                WE.append_verification_sheet(written, res, grid)
                if items:
                    WE.append_derivation_sheet(written, items)
                WE.append_discovery_sheet(written, res)
                if want_audit:
                    WE.append_audit_sheet(written, res, plan)
                check = None
                if want_check:
                    check = VER.build_check_print(
                        page_g, res, grid=grid,
                        out_path=OUTPUT_DIR / f"{pdf_path.stem}_check.png")
            finally:
                doc_g.close()

            placeholders = {p.tag for p in proj.pedestals
                            if abs(p.height_m - QV.PLACEHOLDER_HEIGHT_M) < 1e-9}
            notes = []
            if res.used_text_layer:
                notes.append("read from the sheet's own text layer (no model calls)")
            if res.position_marks:
                notes.append("marks counted: " + ", ".join(
                    f"{k}x{v}" for k, v in res.position_marks.items()))
            outcomes.append(RP.RunOutcome(
                pdf_path, True, proj, res, written, check,
                derived_tags={getattr(i.element, "tag", "") for i in items},
                placeholder_tags=placeholders, notes=notes))
            log.success(f"{pdf_path.name} — {len(bom.lines)} BOQ lines → "
                        f"{written.name}")
        except Exception as exc:               # noqa: BLE001 — one bad sheet
            outcomes.append(RP.RunOutcome(pdf_path, False,
                                          error=f"{type(exc).__name__}: {exc}"))
            log.error(f"{pdf_path.name} — {exc}")

    bar.progress(1.0, text="done")
    from core import set_workbook as SW
    entries = [SW.build_entry(o.project.drawing_no, o.project,
                              source_pdf=str(o.pdf), derived_tags=o.derived_tags,
                              placeholder_tags=o.placeholder_tags, notes=o.notes,
                              position_marks=(o.result.position_marks
                                              if o.result else {}),
                              discovery=(o.result.discovery
                                         if o.result else {}))
               for o in outcomes if o.ok and o.project]
    consolidated = (SW.write_set_workbook(entries, OUTPUT_DIR / "SET_BOQ.xlsx",
                                          project_name=project.project_name)
                    if entries else None)
    rollup = RP.write_rollup(outcomes, OUTPUT_DIR / "SET_ROLLUP.xlsx")
    st.session_state["batch_outcomes"] = [
        {"drawing": o.pdf.name, "ok": o.ok,
         "workbook": str(o.workbook) if o.workbook else "",
         "error": o.error} for o in outcomes]

    good = sum(1 for o in outcomes if o.ok)
    st.success(f"{good} of {len(outcomes)} drawing(s) produced a workbook.")
    st.dataframe(pd.DataFrame(st.session_state["batch_outcomes"]),
                 hide_index=True, use_container_width=True)
    dl1, dl2 = st.columns(2)
    if consolidated:
        with dl1, open(consolidated, "rb") as fh:
            st.download_button("⬇️  Download consolidated BOQ (.xlsx)",
                               data=fh.read(), file_name=consolidated.name,
                               type="primary", use_container_width=True,
                               mime="application/vnd.openxmlformats-officedocument."
                                    "spreadsheetml.sheet")
        st.caption("One Summary sheet: Sl. #, Description, UoM, then a quantity "
                   "column per drawing. Shaded cells are assumptions, not "
                   "readings. Detail sheets follow, named by drawing and activity.")
    with dl2, open(rollup, "rb") as fh:
        st.download_button("⬇️  Download status roll-up (.xlsx)", data=fh.read(),
                           file_name=rollup.name, use_container_width=True,
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet")
    st.caption("Every workbook carries the UNVERIFIED DRAFT banner and its own "
               "Verification sheet. Check each one against its drawing before "
               "pricing.")


st.header("1 · Drawings")

uploads = st.file_uploader(
    "Upload drawing PDFs", type=["pdf"], accept_multiple_files=True,
    help="Saved to output/uploads/. Upload as many as you like; tick the ones "
         "to work on.")

# Streamlit hands back the uploader's whole file list on *every* rerun, not just
# the run where the files arrived. Acting on that list unconditionally meant
# re-ticking every uploaded drawing on each rerun — so unticking one was undone
# immediately and "Clear" was reverted before it could be seen — and re-reading
# every file from disk to compare bytes, which is ~100 MB of I/O per click with
# ten drawings. Each upload is therefore handled exactly once.
_seen_uploads: set[str] = st.session_state.setdefault("_seen_uploads", set())
for up in uploads or []:
    signature = f"{up.name}:{up.size}"
    if signature in _seen_uploads:
        continue
    _seen_uploads.add(signature)
    dest = UPLOAD_DIR / up.name
    dest.write_bytes(up.getvalue())
    st.session_state[f"pick::{dest}"] = True          # newly added = ticked


def _library() -> list[Path]:
    """Every drawing available, uploads first, de-duplicated by real path."""
    found, seen = [], set()
    for path in (sorted(UPLOAD_DIR.glob("*.pdf"))
                 + [q for q in sorted(Path(".").glob("*.pdf"))
                    if not q.name.startswith(".")]):
        rp = path.resolve()
        if rp not in seen:
            seen.add(rp)
            found.append(path)
    return found


library = _library()
if not library:
    st.info("Upload a drawing PDF to begin.")
    st.stop()

# Select-all / clear operate by writing the checkbox keys before the widgets are
# created, which is the only way to drive a widget from a button in Streamlit.
b1, b2, b3, _ = st.columns([1, 1, 1.4, 3])
if b1.button("Select all", use_container_width=True):
    for path in library:
        st.session_state[f"pick::{path}"] = True
    st.rerun()
if b2.button("Clear", use_container_width=True):
    for path in library:
        st.session_state[f"pick::{path}"] = False
    st.rerun()

# The same drawing can sit in both output/uploads and the project root. They are
# different files, so both are listed — but an identical label on two rows is a
# trap, so the location is folded into the label when names collide.
_name_counts: dict[str, int] = {}
for path in library:
    _name_counts[path.name] = _name_counts.get(path.name, 0) + 1

selected: list[Path] = []
for path in library:
    key = f"pick::{path}"
    in_uploads = UPLOAD_DIR.resolve() in path.resolve().parents
    where = "uploaded" if in_uploads else "project root"
    label = path.name if _name_counts[path.name] == 1 else f"{path.name}  ·  {where}"
    c1, c2, c3 = st.columns([6, 1.4, 1.4])
    with c1:
        ticked = st.checkbox(label, key=key)
    c2.caption(f"{path.stat().st_size / 1024 / 1024:.1f} MB")
    c3.caption(where)
    if ticked:
        selected.append(path)

deletable = [q for q in selected if UPLOAD_DIR.resolve() in q.resolve().parents]
if deletable:
    with b3:
        st.session_state.setdefault("_confirm_delete", False)
        if st.button(f"🗑 Delete {len(deletable)}", use_container_width=True):
            st.session_state["_confirm_delete"] = True

if st.session_state.get("_confirm_delete") and deletable:
    st.warning(f"Delete **{len(deletable)}** uploaded file(s) permanently? "
               f"{', '.join(q.name for q in deletable)}", icon="🗑")
    d1, d2, _ = st.columns([1, 1, 4])
    if d1.button("Yes, delete", type="primary", use_container_width=True):
        for q in deletable:
            size = q.stat().st_size if q.exists() else 0
            try:
                q.unlink()
            except OSError as exc:                      # noqa: PERF203
                st.error(f"Could not delete {q.name}: {exc}")
            st.session_state.pop(f"pick::{q}", None)
            # Forget the upload signature too, so re-uploading the same file
            # later is treated as new rather than silently skipped.
            _seen_uploads.discard(f"{q.name}:{size}")
        st.session_state["_confirm_delete"] = False
        st.session_state.pop("extraction", None)
        st.rerun()
    if d2.button("Cancel", use_container_width=True):
        st.session_state["_confirm_delete"] = False
        st.rerun()

# Drawings in the project root are deliberately not deletable here: this page
# manages its own uploads, and quietly removing a file someone put in the repo
# is not its business.
if selected and not deletable:
    st.caption("Files in the project root are not deleted from this page.")

if not selected:
    st.info("Tick a drawing to work on it.")
    st.stop()

if len(selected) > 1:
    st.session_state.pop("extraction", None)
    _batch_panel(selected)
    st.stop()

source_pdf = selected[0]
if st.session_state.get("_last_pdf") != str(source_pdf):
    st.session_state.pop("extraction", None)
    st.session_state["_last_pdf"] = str(source_pdf)
project.pdf_source_filename = source_pdf.name
project.pdf_source_path = str(source_pdf)


# ======================================================================
# 2 · Sheet
# ======================================================================
st.header("2 · Sheet")

doc, page = R.open_page(source_pdf, 0)
n_pages = doc.page_count
doc.close()
page_no = 1
if n_pages > 1:
    page_no = st.number_input("Page", min_value=1, max_value=n_pages, value=1)

doc, page = R.open_page(source_pdf, page_no - 1)
try:
    info = R.page_info(page)
    preview = R.render_full(page, 1500)
    tb_preview = R.render_region(page, R.TITLE_BLOCK, 900, max_pixels=800_000)
    # Locating text is pure geometry — no model, ~1 s. Do it up front so the
    # user can see whether this sheet is extractable before spending minutes.
    blocks = VT.find_text_blocks(page)
    candidates = VT.callout_candidates(blocks)
    montages = VT.build_montages(page, candidates)
finally:
    doc.close()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Sheet", info["sheet_size"])
c2.metric("Text blocks found", len(blocks))
c3.metric("Callout candidates", len(candidates))
c4.metric("Model calls needed", len(montages) + 2)

if not info["has_text_layer"]:
    st.info("This PDF has no text layer — the text is drawn as curves, so a text "
            "parser would return nothing. The blocks above were located from the "
            "drawing's vector geometry, which costs no model time.")
if not candidates:
    st.warning("No text blocks located. If this sheet is a scan rather than CAD "
               "vector output, choose the **sweep** profile below — it looks at "
               "the whole sheet instead, but takes far longer.")

pv1, pv2 = st.columns([3, 1])
SC.image(pv1, preview.png, caption=f"{source_pdf.name} — page {page_no}")
SC.image(pv2, tb_preview.png, caption="Title block (read first)")


# ======================================================================
# 3 · Extract
# ======================================================================
st.header("3 · Extract")

if ok:
    st.success(f"Local model ready — {msg}")
else:
    st.error(f"Local model unavailable — {msg}")
    st.code("ollama serve\nollama pull qwen2.5vl:7b", language="bash")

e1, e2 = st.columns([1, 2])
with e1:
    profile = st.selectbox(
        "Profile", ["thorough", "quick", "sweep", "fast"],
        help="thorough — locate text, then transcribe it (default, fastest). "
             "quick — largest text only. "
             "sweep / fast — blind tile sweep for scanned sheets; much slower.")
cfg = ExtractionConfig(**PROFILES[profile].__dict__)
n_calls = (len(montages) + 2 if cfg.strategy == "montage"
           else len(QV._tile_plan(cfg)) + 2)
with e2:
    st.caption(f"About **{n_calls} model calls ≈ {n_calls * 48 // 60} min "
               f"{n_calls * 48 % 60} s** on this machine. The window can stay "
               f"in the background, but leave it open.")

if st.button("🔍 Extract drawing", type="primary", disabled=not ok,
             use_container_width=True):
    bar = st.progress(0.0, text="starting…")

    def progress(label: str, i: int, n: int) -> None:
        bar.progress(min(1.0, i / max(n, 1)), text=f"[{i}/{n}] {label}")

    with st.spinner("Reading the drawing…"):
        st.session_state.extraction = QV.extract_from_pdf(
            source_pdf, page_number=page_no - 1, config=cfg,
            client=client, progress=progress)
    bar.empty()
    st.rerun()

result: ExtractionResult | None = st.session_state.get("extraction")
if result is None:
    st.stop()


# ======================================================================
# 4 · Review and correct
# ======================================================================
st.header("4 · Review and correct")
st.caption(f"{result.model} · {result.profile} · {result.total_elapsed_s:.0f} s · "
           f"{len([r for r in result.responses if not r.skipped])} model calls")

tb = result.title_block
m1, m2, m3, m4 = st.columns(4)
m1.metric("Drawing no", tb.drawing_no or "—")
m2.metric("Revision", tb.revision or "—")
m3.metric("Date", tb.date or "—")
m4.metric("Pedestals found", len(result.pedestals))

with st.expander("Project details (edit if the model misread them)", expanded=False):
    d1, d2 = st.columns(2)
    with d1:
        tb.drawing_no = st.text_input("Drawing no", value=tb.drawing_no)
        tb.revision = st.text_input("Revision", value=tb.revision)
        tb.date = st.text_input("Date (YYYY-MM-DD)", value=tb.date)
    with d2:
        tb.project_name = st.text_area("Project name", value=tb.project_name, height=90)
        tb.prepared_by = st.text_input("Prepared by",
                                       value=tb.prepared_by or project.prepared_by)

# ---------- Pedestals ----------
st.subheader("Pedestals")
st.warning("**Pedestal height is not printed on these callouts.** The model "
           f"cannot read it, so it is pre-filled with "
           f"{QV.PLACEHOLDER_HEIGHT_M:.3f} m. Enter the real heights below — "
           f"concrete, formwork and rebar are wrong until you do.")

ped_rows = [{
    "tag": p.tag,
    "length_m": round(p.length_mm / 1000, 3),
    "width_m": round(p.width_mm / 1000, 3),
    "height_m": round(p.height_mm / 1000, 3) if p.height_mm else QV.PLACEHOLDER_HEIGHT_M,
    "quantity": int(p.quantity),
    "grade": "RCC_M30",
    "rebar_coefficient_kg_per_m3": 120.0,
    "callout": p.raw_text,
} for p in result.pedestals]

ped_df = st.data_editor(
    pd.DataFrame(ped_rows, columns=["tag", "length_m", "width_m", "height_m",
                                    "quantity", "grade",
                                    "rebar_coefficient_kg_per_m3", "callout"]),
    num_rows="dynamic", use_container_width=True, key="ped_edit",
    column_config={
        "tag": st.column_config.TextColumn("Tag", required=True),
        "length_m": st.column_config.NumberColumn("Length (m)", min_value=0.001, step=0.05),
        "width_m": st.column_config.NumberColumn("Width (m)", min_value=0.001, step=0.05),
        "height_m": st.column_config.NumberColumn("Height (m) ⚠", min_value=0.001,
                                                  step=0.05,
                                                  help="Not on the drawing — enter it"),
        "quantity": st.column_config.NumberColumn("Nos", min_value=1, step=1),
        "grade": st.column_config.SelectboxColumn("Grade", options=CONCRETE_GRADES),
        "rebar_coefficient_kg_per_m3": st.column_config.NumberColumn(
            "Rebar (kg/m³)", min_value=0.0, step=5.0),
        "callout": st.column_config.TextColumn("Verbatim callout", disabled=True),
    })

# ---------- Grade slab ----------
st.subheader("Grade slab")
slab_rows = [{
    "tag": s.tag,
    "length_m": round((s.length_mm or 0) / 1000, 3),
    "width_m": round((s.width_mm or 0) / 1000, 3),
    "thickness_m": round((s.thickness_mm or 0) / 1000, 3),
    "grade": "RCC_M30",
    "rebar_coefficient_kg_per_m3": 80.0,
} for s in result.grade_slabs if s.is_usable()]

slab_df = st.data_editor(
    pd.DataFrame(slab_rows, columns=["tag", "length_m", "width_m", "thickness_m",
                                     "grade", "rebar_coefficient_kg_per_m3"]),
    num_rows="dynamic", use_container_width=True, key="slab_edit",
    column_config={
        "tag": st.column_config.TextColumn("Tag", required=True),
        "length_m": st.column_config.NumberColumn("Length (m)", min_value=0.001, step=0.1),
        "width_m": st.column_config.NumberColumn("Width (m)", min_value=0.001, step=0.1),
        "thickness_m": st.column_config.NumberColumn("Thickness (m)",
                                                     min_value=0.001, step=0.025),
        "grade": st.column_config.SelectboxColumn("Grade", options=CONCRETE_GRADES),
        "rebar_coefficient_kg_per_m3": st.column_config.NumberColumn(
            "Rebar (kg/m³)", min_value=0.0, step=5.0),
    })
if result.grade_slabs:
    st.caption("Read off the plan view rather than a callout — check it against "
               "the dimension string on the sheet.")

# ---------- Items the drawing named itself ----------
# The panels above look for element types chosen in advance. This one shows what
# the sheet actually specifies, whatever that turns out to be — on a details or
# sections sheet it is the only thing there is.
disc = result.discovery or {}
disc_items = disc.get("items") or []
if disc_items:
    measured = disc.get("measured", 0)
    with st.expander(f"Read from this drawing — {len(disc_items)} item(s), "
                     f"{measured} with a stated quantity", expanded=True):
        st.caption("Named by the drawing rather than by a preset list. A blank "
                   "quantity means the sheet states the specification but not "
                   "the extent — supply the area, length or count and the row "
                   "becomes a quantity. Every row keeps the text it came from.")
        st.dataframe(pd.DataFrame([{
            "Description": row["description"],
            "UoM": row["uom"],
            "Qty stated": row["qty"],
            "Basis": row["basis"],
            "Seen": row["occurrences"],
            "Grid ref": row["grid_ref"],
            "Source text": row["source"],
        } for row in disc_items]), hide_index=True, use_container_width=True)

        specs = disc.get("specifications") or []
        if specs:
            st.markdown("**Stated in the sheet's notes**")
            st.dataframe(pd.DataFrame([{
                "Value": f"{s['value']:g} {s['unit']}", "Note": s["text"]}
                for s in specs]), hide_index=True, use_container_width=True)

        heights = disc.get("heights") or []
        if heights:
            st.markdown("**Heights implied by the levels on the sheet**")
            st.caption("Which bottom belongs to which top is a question about "
                       "the section. Nothing here is applied automatically.")
            st.dataframe(pd.DataFrame([{
                "Height (m)": h["height_m"], "Top": h["top"],
                "Bottom": h["bottom"]} for h in heights]),
                hide_index=True, use_container_width=True)

# ---------- Everything else the model read ----------
findings = result.findings or {}
if any(findings.get(k) for k in findings):
    with st.expander("Other things read from the drawing (not added to the BOQ)",
                     expanded=False):
        st.caption("These are recognised but incomplete — a curb wall callout "
                   "gives thickness and height but not its run length, a sump "
                   "plan dimension says nothing about depth. Enter them in "
                   "**1_Input** where you can supply the missing figures.")
        labels = {"curb_walls": "Curb walls", "sumps": "Sumps", "levels": "Levels",
                  "insert_plates": "Insert plates", "epoxy": "Epoxy coating",
                  "rebar": "Rebar callouts", "thicknesses": "Thickness notes"}
        for key, label in labels.items():
            rows = findings.get(key) or []
            if rows:
                st.markdown(f"**{label}**")
                st.dataframe(pd.DataFrame(rows), hide_index=True,
                             use_container_width=True)

with st.expander("Extraction log"):
    st.dataframe(pd.DataFrame([{
        "Stage": r.stage, "Region": r.region,
        "Pixels": f"{r.px_width}x{r.px_height}",
        "Seconds": r.elapsed_s,
        "Status": "skipped" if r.skipped else (r.error or "ok"),
    } for r in result.responses]), hide_index=True, use_container_width=True)
    if result.transcribed_lines:
        st.markdown(f"**{len(result.transcribed_lines)} transcribed line(s)**")
        st.code("\n".join(result.transcribed_lines), language=None)
    for n in result.confidence_notes:
        st.markdown(f"- {n}")


# ======================================================================
# 5 · Generate the workbook
# ======================================================================
st.header("5 · Generate BOQ")


def _rows_to_pedestals(df: pd.DataFrame) -> tuple[list[Pedestal], list[str]]:
    out, bad = [], []
    for rec in df.dropna(how="all").to_dict("records"):
        if not str(rec.get("tag") or "").strip():
            continue
        try:
            out.append(Pedestal(
                tag=str(rec["tag"]).strip(),
                length_m=float(rec["length_m"]), width_m=float(rec["width_m"]),
                height_m=float(rec["height_m"]), quantity=int(rec["quantity"]),
                grade=rec.get("grade") or "RCC_M30",
                rebar_coefficient_kg_per_m3=float(
                    rec.get("rebar_coefficient_kg_per_m3") or 120.0)))
        except Exception as exc:                      # noqa: BLE001 - shown to user
            bad.append(f"pedestal {rec.get('tag')}: {exc}")
    return out, bad


def _rows_to_slabs(df: pd.DataFrame) -> tuple[list[GradeSlab], list[str]]:
    out, bad = [], []
    for rec in df.dropna(how="all").to_dict("records"):
        if not str(rec.get("tag") or "").strip():
            continue
        try:
            out.append(GradeSlab(
                tag=str(rec["tag"]).strip(),
                length_m=float(rec["length_m"]), width_m=float(rec["width_m"]),
                thickness_m=float(rec["thickness_m"]),
                grade=rec.get("grade") or "RCC_M30",
                rebar_coefficient_kg_per_m3=float(
                    rec.get("rebar_coefficient_kg_per_m3") or 80.0)))
        except Exception as exc:                      # noqa: BLE001
            bad.append(f"slab {rec.get('tag')}: {exc}")
    return out, bad


pedestals, ped_err = _rows_to_pedestals(ped_df)
slabs, slab_err = _rows_to_slabs(slab_df)
for e in ped_err + slab_err:
    st.error(e)

untouched = [p.tag for p in pedestals
             if abs(p.height_m - QV.PLACEHOLDER_HEIGHT_M) < 1e-9]
if untouched:
    st.warning(f"Still at the placeholder height: **{', '.join(untouched)}**. "
               f"The workbook will carry an UNVERIFIED DRAFT banner saying so.")

st.markdown("**Derive the quantities the drawing does not print**")
st.caption("Excavation, blinding, fill, liner, coating, curb wall and joints all "
           "follow from the slab geometry plus site convention. These are "
           "assumptions, not readings — tick only what applies, and check the "
           "Derivation sheet in the workbook.")

if "x_rules" not in st.session_state:
    st.session_state.x_rules = rules_from_findings(default_rules(), result.findings)
x_rules = st.session_state.x_rules
_rule_toggles(x_rules, key_prefix="xr_", disabled=not slabs)
if not slabs:
    st.caption("These need a grade slab — add one in the grid above to enable them.")

_preview_project = Project(project_name="", drawing_no="")
_preview_project.grade_slabs = slabs
derived_preview = derive(_preview_project, x_rules) if slabs else []
if derived_preview:
    with st.expander(f"{len(derived_preview)} derived quantity(ies) — "
                     f"see the arithmetic", expanded=False):
        st.dataframe(pd.DataFrame([{
            "Element": i.target_field, "Tag": getattr(i.element, "tag", ""),
            "How it was worked out": i.explanation} for i in derived_preview]),
            hide_index=True, use_container_width=True)

g1, g2, g3 = st.columns([2, 1, 1])
with g1:
    merge_first = st.checkbox(
        "Also merge into the session project (so Input / BOM see it)",
        value=True,
        help="Non-destructive: anything you already entered elsewhere wins.")
with g2:
    audit = st.checkbox("Extraction_Log sheet", value=True)
with g3:
    want_check = st.checkbox("Check print", value=True,
                             help="The drawing with every extracted value boxed "
                                  "and quoted by grid square — for marking up.")

if st.button("🧾 Generate BOQ Excel", type="primary", use_container_width=True,
             disabled=not (pedestals or slabs)):
    target_project = project if merge_first else Project(project_name="", drawing_no="")

    for field_name, value in (("drawing_no", tb.drawing_no), ("revision", tb.revision),
                              ("project_name", tb.project_name), ("date", tb.date),
                              ("prepared_by", tb.prepared_by)):
        if value and not getattr(target_project, field_name, ""):
            setattr(target_project, field_name, value)

    existing = {p.tag.upper() for p in target_project.pedestals}
    target_project.pedestals += [p for p in pedestals if p.tag.upper() not in existing]
    existing_s = {s.tag.upper() for s in target_project.grade_slabs}
    target_project.grade_slabs += [s for s in slabs if s.tag.upper() not in existing_s]

    if not target_project.pdf_source_path:
        target_project.pdf_source_filename = source_pdf.name
        target_project.pdf_source_path = str(source_pdf)

    if not target_project.drawing_no:
        st.error("A drawing number is required. Set it under **Project details** above.")
        st.stop()

    derived_items = derive(target_project, x_rules)
    if derived_items:
        apply_derived(target_project, derived_items)

    target_project.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    bom = build_bom(target_project)
    plan = QV.plan_merge(Project(project_name="", drawing_no=""), result)
    bom.warnings = QV.extraction_warnings(result, plan) + bom.warnings

    out_path = build_output_path(target_project.drawing_no, OUTPUT_DIR)
    with st.spinner("Building workbook…"):
        written = write_workbook(target_project, bom, out_path)
        doc_g, page_g = R.open_page(source_pdf, page_no - 1)
        try:
            grid = SG.detect_grid(page_g)
            WE.append_verification_sheet(written, result, grid)
            if derived_items:
                WE.append_derivation_sheet(written, derived_items)
            WE.append_discovery_sheet(written, result)
            if audit:
                WE.append_audit_sheet(written, result, plan)
            if want_check:
                check = VER.build_check_print(
                    page_g, result, grid=grid,
                    out_path=OUTPUT_DIR / f"{source_pdf.stem}_check.png")
                st.session_state["last_check_print"] = str(check)
        finally:
            doc_g.close()

    st.session_state["last_workbook"] = str(written)
    st.success(f"Workbook written — {len(bom.lines)} BOQ line(s)"
               + (f", including {len(derived_items)} derived" if derived_items else ""))

if st.session_state.get("last_workbook"):
    wb_path = Path(st.session_state["last_workbook"])
    if wb_path.exists():
        dl1, dl2 = st.columns(2)
        with dl1:
            with open(wb_path, "rb") as fh:
                st.download_button(
                    "⬇️  Download BOQ workbook (.xlsx)", data=fh.read(),
                    file_name=wb_path.name, use_container_width=True,
                    mime="application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet")
            st.caption(f"Saved at `{wb_path}`")
        with dl2:
            cp = st.session_state.get("last_check_print")
            if cp and Path(cp).exists():
                with open(cp, "rb") as fh:
                    st.download_button("🖨️  Download check print (.png)",
                                       data=fh.read(), file_name=Path(cp).name,
                                       mime="image/png", use_container_width=True)
                st.caption("Print this, sit it beside the drawing, and mark up "
                           "the Verification sheet.")

        st.info("**For the check:** the workbook's **Verification** sheet lists "
                "every extracted value with its verbatim callout and drawing "
                "grid reference (e.g. D-7), with blank columns for the correct "
                "value, who checked it and when.", icon="✅")

st.divider()
d1, d2 = st.columns(2)
with d1:
    st.download_button("⬇️ Extraction JSON",
                       data=json.dumps(result.model_dump(), indent=2),
                       file_name=f"{source_pdf.stem}_extraction.json",
                       mime="application/json", use_container_width=True)
with d2:
    lib = rag_examples.library_stats()
    verifier = st.text_input("Save as verified example — your name",
                             value=tb.prepared_by or project.prepared_by,
                             label_visibility="collapsed",
                             placeholder="Your name, to save this as a verified example")
    if st.button(f"💾 Save as verified example ({lib['distinct_drawings']}/10)",
                 disabled=not (pedestals and verifier), use_container_width=True):
        path = rag_examples.save_example(
            drawing_no=tb.drawing_no, revision=tb.revision,
            image_hash=result.image_hash,
            verified_extraction={"pedestals": [
                {"tag": p.tag, "length_mm": p.length_m * 1000,
                 "width_mm": p.width_m * 1000, "quantity": p.quantity}
                for p in pedestals]},
            verified_by=verifier, source_pdf=source_pdf.name)
        st.success(f"Saved → `{path}` — future drawings in this family will use it.")

kit.sidebar_summary(project)
with st.sidebar:
    st.subheader("This drawing")
    st.write(f"Pedestals: {len(pedestals)}")
    st.write(f"Grade slabs: {len(slabs)}")
    st.write(f"Extraction: {result.total_elapsed_s:.0f} s")
    st.caption("Excavation, joints, sump, embedments and manual rebar are "
               "entered in **1_Input**.")
kit.sidebar_account()
