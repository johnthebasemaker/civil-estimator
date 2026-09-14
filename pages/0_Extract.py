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
import time
from collections import OrderedDict
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
    from core import extract_cache as CACHE
    from core import classification as CLASS
    from core import gaps as GAPS
    from core import jobstore as JS
    from core import set_workbook as SW
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

# Classification of the site condition a drawing belongs to. Asked per drawing
# because one submission routinely mixes all three, and the quantities are
# tendered separately: new-build ground, work inside a live plant, and making
# good what is already there do not carry the same rates.
CLASSIFICATIONS = list(CLASS.CHOICES)
DEFAULT_CLASSIFICATION = CLASS.DEFAULT


def classification_of(pdf_path) -> str:
    """The classification a drawing is filed under.

    Read from the session first so the dropdown responds immediately, then from
    the file, which is what `bin/rebuild_set.py` sees long after the tab closes.
    """
    session = st.session_state.get(f"class::{pdf_path}")
    if session in CLASSIFICATIONS:
        return session
    return CLASS.get(pdf_path)


# ----------------------------------------------------------------------
# Clearing the session
# ----------------------------------------------------------------------
# Every key the three areas of this page own. Listed explicitly rather than
# wiped wholesale, because session state also holds the login, and a clear that
# signs the user out is a clear nobody presses twice.
_SESSION_PREFIXES = (
    "pick::",        # which drawings are ticked in the library
    "class::",       # per-drawing site classification
    "fdwg::",        # which drawings are ticked for the combined BOQ
)
_SESSION_KEYS = (
    # Area 1 — uploads and the drawing library
    "_seen_uploads", "_confirm_delete", "_last_pdf",
    # Area 2 — extraction, the review grids and the workbook it produced
    "extraction", "x_rules", "q_rules", "q_profile", "q_force",
    "ped_edit", "slab_edit", "last_workbook", "last_check_print",
    "_confirm_clear_queue",
    # Area 3 — selection, filters and the built BOQ
    "drawing_picks", "line_picks", "sel_dwg", "sel_src", "sel_search",
    "line_editor", "set_boq_path", "set_boq_built", "set_boq_lines",
    "set_boq_drawings", "_confirm_clear_result",
)


def _clear_session(*, delete_uploads: bool = False) -> dict:
    """Put all three areas back to how the page looks on a cold start.

    The saved extractions under `output/cache` are deliberately left alone. They
    are keyed by the drawing's content hash, so a cleared drawing re-queued
    later comes back in milliseconds instead of minutes of model time — and a
    button that quietly threw that away would be expensive to press by accident.
    """
    removed = {"keys": 0, "jobs": 0, "files": 0}

    for key in list(st.session_state.keys()):
        if key in _SESSION_KEYS or key.startswith(_SESSION_PREFIXES):
            st.session_state.pop(key, None)
            removed["keys"] += 1

    # The project carries the title-block identity, which is part of the
    # session rather than of any one drawing.
    st.session_state.project = Project(project_name="", drawing_no="")

    # Area 3 is built from finished queue rows, so leaving them would refill the
    # section the moment the page redrew — the ghost rendering to avoid.
    removed["jobs"] = JS.clear(owner=_owner())

    if delete_uploads:
        for pdf in sorted(UPLOAD_DIR.glob("*.pdf")):
            try:
                pdf.unlink()
                removed["files"] += 1
            except OSError:
                continue
            # The classification describes that drawing, so it goes with it.
            # Left behind, it would quietly reattach to a different drawing
            # uploaded later under the same file name.
            CLASS.forget(pdf)

    # The uploader keeps its own list of files, which no key of ours can reach.
    # Bumping the nonce gives it a new identity, which is the only way to make
    # it forget.
    st.session_state["upload_nonce"] = st.session_state.get("upload_nonce", 0) + 1
    return removed


def _session_bar() -> None:
    """Clear, save and load, above everything the three areas hold."""
    left, right = st.columns([3, 2])
    with left:
        if st.session_state.get("_confirm_clear_session"):
            st.warning(
                "Clear the whole session? Ticked drawings, extracted figures, "
                "filters and the built BOQ all go back to empty. The saved "
                "extractions on disk are kept, so re-reading a drawing costs "
                "nothing.", icon="🧹")
            drop = st.checkbox(
                f"Also delete the {len(list(UPLOAD_DIR.glob('*.pdf')))} uploaded "
                f"PDF(s) from output/uploads",
                key="clear_drop_uploads",
                help="Off by default. This one cannot be undone.")
            y, n = st.columns(2)
            if y.button("Yes, clear the session", type="primary",
                        use_container_width=True, key="clear_session_yes"):
                report = _clear_session(delete_uploads=drop)
                st.session_state.pop("_confirm_clear_session", None)
                st.session_state["_cleared_report"] = report
                SC.rerun()
            if n.button("Keep everything", use_container_width=True,
                        key="clear_session_no"):
                st.session_state.pop("_confirm_clear_session", None)
                SC.rerun()
        else:
            if st.button("🧹 Clear session", type="primary",
                         use_container_width=True, key="clear_session"):
                st.session_state["_confirm_clear_session"] = True
                SC.rerun()
            st.caption("Resets the drawing list, the extracted figures and the "
                       "filters below, all three at once.")

    with right:
        with st.expander("Save or load this project", expanded=False):
            PROJECT_DIR = OUTPUT_DIR / "projects"
            PROJECT_DIR.mkdir(parents=True, exist_ok=True)
            if st.button("💾 Save project to JSON", use_container_width=True,
                         key="save_project"):
                if not project.drawing_no:
                    st.error("Set a drawing number below before saving.")
                else:
                    from core.filename import sanitise
                    fpath = PROJECT_DIR / (
                        f"{sanitise(project.drawing_no)}_"
                        f"{datetime.now().strftime('%Y%m%d_%H%M')}.json")
                    fpath.write_text(project.model_dump_json(indent=2))
                    st.success(f"Saved → `{fpath}`")
            saved = sorted(PROJECT_DIR.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if saved:
                sel = st.selectbox("Load a saved project",
                                   ["— select —"] + [p.name for p in saved],
                                   key="load_project_pick")
                if sel != "— select —" and st.button(
                        "📂 Load", use_container_width=True, key="load_project"):
                    st.session_state.project = Project(
                        **json.loads((PROJECT_DIR / sel).read_text()))
                    SC.rerun()
            else:
                st.caption("No saved projects yet.")

    report = st.session_state.pop("_cleared_report", None)
    if report:
        parts = [f"{report['keys']} setting(s)"]
        if report["jobs"]:
            parts.append(f"{report['jobs']} queue row(s)")
        if report["files"]:
            parts.append(f"{report['files']} uploaded file(s)")
        st.success("Session cleared — " + ", ".join(parts) + ".", icon="✅")
    st.divider()



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


def _owner() -> str:
    """Whose queue this is.

    One shared password today, so everyone is the same owner. The column exists
    because the queue has to be ready for per-person accounts without a
    migration, and because a shared queue where anyone can cancel anyone's batch
    is only tolerable while the team is small.
    """
    return str(st.session_state.get("_user") or "shared")


def _worker_alive(jobs: list) -> bool:
    """Whether something is actually doing the work.

    Worth showing plainly. A queue that fills up while no worker is running
    looks identical to a slow model, and the difference is one command.
    """
    now = time.time()
    return any(j.state == JS.RUNNING and now - j.heartbeat_at < 120 for j in jobs)


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


@st.fragment(run_every="2s")
def _queue_status() -> None:
    """Live view of the queue. Reruns itself; the rest of the page does not.

    This is what replaced running the batch inside the script. The work happens
    in `bin/worker.py`, so pressing pause or cancel is answered in about two
    seconds rather than at the end of a six-minute drawing — or, as before, not
    at all.
    """
    owner = _owner()
    jobs = JS.list_jobs(owner=owner)
    if not jobs:
        st.info("The queue is empty. Tick drawings above and add them.")
        return

    s = JS.summary(owner=owner)
    counts = s["counts"]
    running = s["running"]

    if s["paused"]:
        st.warning("Queue paused. The drawing in progress will finish; nothing "
                   "new starts until you resume.", icon="⏸")
    elif counts[JS.QUEUED] and not _worker_alive(jobs):
        st.error("Nothing is processing this queue. Start the worker:", icon="⚠️")
        st.code("venv/bin/python bin/worker.py", language="bash")

    # The bar measures how much of the queue has been *processed*, and a failed
    # drawing has been processed. Saying "100%" on its own would read as
    # success, so failures are named beside the number and again above it.
    if counts[JS.FAILED]:
        failed_names = ", ".join(j.drawing_name for j in jobs
                                 if j.state == JS.FAILED)[:200]
        st.warning(f"{counts[JS.FAILED]} drawing(s) did not extract: "
                   f"{failed_names}. The reason is in the Problem column below. "
                   f"Fix it and press Retry failed.", icon="⚠️")
    st.progress(min(1.0, s["percent"] / 100.0),
                text=f"{s['percent']:.0f}% processed — {counts[JS.DONE]} read, "
                     f"{counts[JS.QUEUED]} waiting, {counts[JS.FAILED]} failed")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Done", counts[JS.DONE])
    m2.metric("Waiting", counts[JS.QUEUED])
    m3.metric("Average per drawing",
              _fmt_duration(s["mean_seconds"]) if s["mean_seconds"] else "—")
    m4.metric("Estimated remaining",
              _fmt_duration(s["eta_seconds"]) if s["eta_seconds"] else "—")

    if running is not None:
        st.progress(min(1.0, running.progress_pct / 100.0),
                    text=f"▶ {running.drawing_name} — {running.progress_pct:.0f}% "
                         f"· {running.stage or 'working'} · "
                         f"{_fmt_duration(time.time() - running.started_at)} elapsed")

    c1, c2, c3, c4 = st.columns(4)
    if s["paused"]:
        if c1.button("▶ Resume", use_container_width=True, key="q_resume"):
            JS.set_paused(False, owner=owner)
            st.rerun()
    elif c1.button("⏸ Pause", use_container_width=True, key="q_pause",
                   disabled=not counts[JS.QUEUED]):
        JS.set_paused(True, owner=owner)
        st.rerun()

    if c2.button("⏹ Stop current", use_container_width=True, key="q_stop",
                 disabled=running is None):
        JS.request_cancel(running.id)
        st.rerun()

    if c3.button("✖ Cancel waiting", use_container_width=True, key="q_cancel",
                 disabled=not counts[JS.QUEUED]):
        JS.request_cancel([j.id for j in jobs if j.state == JS.QUEUED])
        st.rerun()

    if c4.button("↻ Retry failed", use_container_width=True, key="q_retry",
                 disabled=not (counts[JS.FAILED] or counts[JS.CANCELLED])):
        JS.retry([j.id for j in jobs
                  if j.state in (JS.FAILED, JS.CANCELLED)])
        st.rerun()

    st.dataframe(pd.DataFrame([{
        "Drawing": j.drawing_name,
        "State": j.state,
        "%": round(j.progress_pct),
        "Stage": j.stage,
        "Time": _fmt_duration(j.elapsed_s) if j.elapsed_s else "",
        "From cache": "yes" if j.from_cache else "",
        "Problem": j.error,
    } for j in jobs]), hide_index=True, use_container_width=True)

    st.caption("Nothing here disappears on its own. Finished rows stay until "
               "you clear them, and the extraction JSON on disk survives even "
               "that — clearing the queue costs no model time to undo.")
    finished = counts[JS.DONE] + counts[JS.FAILED] + counts[JS.CANCELLED]
    if st.session_state.get("_confirm_clear_queue"):
        st.warning(f"Clear {finished} finished row(s) from the queue? The "
                   f"drawings and their saved extractions are untouched, so "
                   f"re-queueing them costs nothing.", icon="🧹")
        y, n = st.columns(2)
        if y.button("Yes, clear them", key="q_clear_yes", type="primary",
                    use_container_width=True):
            JS.clear(owner=owner)
            st.session_state.pop("_confirm_clear_queue", None)
            st.session_state.pop("drawing_picks", None)
            st.session_state.pop("line_picks", None)
            # A whole-app rerun, not a fragment one. Section 3 below is built
            # from the finished jobs, so redrawing only this fragment leaves it
            # listing drawings the queue no longer has.
            st.rerun()
        if n.button("Keep them", key="q_clear_no", use_container_width=True):
            st.session_state.pop("_confirm_clear_queue", None)
            st.rerun()
    elif st.button("🧹 Clear finished rows", key="q_clear", disabled=not finished):
        st.session_state["_confirm_clear_queue"] = True
        st.rerun()


def _queue_panel(drawings: list[Path]) -> None:
    """Queue drawings for extraction, a sitting at a time.

    Deliberately separate from the single-drawing flow above: that flow exists
    so a person can correct what the model read before anything is written, and
    there is no honest way to offer that for twenty sheets at once. A queued run
    is explicitly a first pass — every workbook it produces still carries the
    UNVERIFIED DRAFT banner and its own Verification sheet to be marked up.
    """
    st.header("2 · Extraction queue")
    st.caption(f"{len(drawings)} drawing(s) ticked. Add them to the queue and "
               f"the worker reads them one at a time. You can close this tab: "
               f"the queue is on disk, and finished drawings stay finished.")

    owner = _owner()
    cached = sum(1 for p in drawings
                 if CACHE.load(JS.fingerprint(p, "thorough")) is not None)
    if cached:
        st.success(f"{cached} of these {len(drawings)} have been read before and "
                   f"will come back instantly from the saved extraction. Only "
                   f"{len(drawings) - cached} need the model.", icon="⚡")

    c1, c2 = st.columns([2, 1])
    with c1:
        profile = st.selectbox(
            "Profile", options=sorted(PROFILES), index=sorted(PROFILES).index("thorough"),
            key="q_profile",
            help="thorough and quick use the montage strategy and are the fast "
                 "routes. sweep tiles the whole sheet and is the slow fallback "
                 "for a drawing nothing else finds callouts on.")
    with c2:
        force = st.checkbox("Re-read even if saved", key="q_force",
                            help="Tick this when a drawing has been reissued "
                                 "under the same filename.")

    if st.button(f"➕ Add {len(drawings)} drawing(s) to the queue",
                 type="primary", use_container_width=True):
        added = JS.enqueue(drawings, owner=owner, profile=profile, force=force)
        skipped = len(drawings) - len(added)
        msg = f"Queued {len(added)} drawing(s)."
        if skipped:
            msg += f" {skipped} were already waiting or running."
        st.success(msg)

    st.divider()
    _queue_status()

def _entries_from_queue(owner: str, rules) -> "OrderedDict":
    """Every finished drawing, rebuilt from its saved extraction.

    Nothing here touches the model. The queue records where each result was
    written and the cache holds it under the drawing's content hash, so this
    runs in milliseconds no matter how many sittings the set took.
    """
    out: "OrderedDict[str, dict]" = OrderedDict()
    for job in JS.list_jobs(owner=owner, states=[JS.DONE]):
        result = CACHE.load(job.fingerprint)
        if result is None:
            continue
        proj = Project(project_name=project.project_name, drawing_no="")
        QV.merge_into_project(proj, result)
        if not proj.drawing_no:
            from core.filename import sanitise
            proj.drawing_no = sanitise(job.path.stem)

        derived_tags: set[str] = set()
        seeded = rules_from_findings(rules, result.findings)
        items = derive(proj, seeded)
        if items:
            derived_tags = {getattr(i.element, "tag", "") for i in items}
            apply_derived(proj, items)
        placeholders = {p.tag for p in proj.pedestals
                        if abs(p.height_m - QV.PLACEHOLDER_HEIGHT_M) < 1e-9}
        notes = []
        if result.used_text_layer:
            notes.append("read from the sheet's own text layer (no model calls)")
        if job.from_cache:
            notes.append("served from the saved extraction")
        entry = SW.build_entry(
            proj.drawing_no, proj, source_pdf=job.drawing_path,
            derived_tags=derived_tags, placeholder_tags=placeholders,
            notes=notes, position_marks=result.position_marks,
            discovery=result.discovery,
            classification=classification_of(job.path))
        out[proj.drawing_no] = {"entry": entry, "job": job, "result": result,
                                "derived": items}
    return out


def _line_rows(entries) -> list[dict]:
    """One selectable row per line item, across every extracted drawing."""
    rows = []
    for drawing_no, bundle in entries.items():
        entry = bundle["entry"]
        for line in entry.bom.lines:
            if line.category == "ROLLUP":
                continue
            rows.append({
                "key": f"{drawing_no}|boq|{line.category}|{line.item}|{line.unit}",
                "Drawing": drawing_no, "Source": "BOQ",
                "Group": line.category, "Description": line.item,
                "UoM": line.unit, "Qty": round(line.qty_gross, 3),
            })
        for item in entry.discovered:
            rows.append({
                "key": f"{drawing_no}|read|{item['description']}|{item['uom']}",
                "Drawing": drawing_no, "Source": "Read from drawing",
                "Group": item.get("kind", ""), "Description": item["description"],
                "UoM": item["uom"], "Qty": item.get("qty"),
            })
    return rows


# The matcher lives in tests/helpers_search.py rather than here: this file is a
# Streamlit script, and importing it to test one function would execute the
# whole page.
from tests.helpers_search import search_rows as _search_rows      # noqa: E402


def _apply_selection(entries, chosen: set[str]) -> list:
    """Keep only the ticked lines, and drop a drawing left with nothing."""
    kept = []
    for drawing_no, bundle in entries.items():
        entry = bundle["entry"]
        entry.bom.lines = [
            l for l in entry.bom.lines
            if l.category == "ROLLUP"
            or f"{drawing_no}|boq|{l.category}|{l.item}|{l.unit}" in chosen]
        items = [i for i in entry.discovered
                 if f"{drawing_no}|read|{i['description']}|{i['uom']}" in chosen]
        entry.discovery = {**entry.discovery, "items": items}
        if entry.has_content:
            kept.append(entry)
    return kept


def _results_panel() -> None:
    """Pick the lines that belong in the BOQ, then build it.

    Extraction and selection are separated on purpose. Reading twenty drawings
    is machine time and happens over as many sittings as it takes; deciding what
    is in the bill is an estimator's judgement and wants everything on one
    screen.
    """
    owner = _owner()
    done = JS.list_jobs(owner=owner, states=[JS.DONE])
    if not done:
        return

    st.divider()
    st.header("3 · Choose what goes in the BOQ")

    if "q_rules" not in st.session_state:
        st.session_state.q_rules = default_rules()
    with st.expander("Derive the quantities the drawings do not print",
                     expanded=False):
        _rule_toggles(st.session_state.q_rules, key_prefix="qr_")

    all_entries = _entries_from_queue(owner, st.session_state.q_rules)
    if not all_entries:
        st.info("The finished extractions carry no quantities yet.")
        return

    # ---- which drawings go into this bill ---------------------------------
    # The set is read over several sittings, so "everything extracted" and
    # "everything in this bill" are different lists. Ticking drawings here is
    # what turns several separate extractions into one consolidated workbook.
    st.markdown("**Drawings to combine into one BOQ**")
    picks_d: dict = st.session_state.setdefault("drawing_picks", {})
    for drawing_no in all_entries:
        picks_d.setdefault(drawing_no, True)

    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Select all", use_container_width=True, key="dwg_all"):
        for drawing_no in all_entries:
            picks_d[drawing_no] = True
        SC.rerun()
    if b2.button("Clear", use_container_width=True, key="dwg_none"):
        for drawing_no in all_entries:
            picks_d[drawing_no] = False
        SC.rerun()

    cols = st.columns(2)
    for i, (drawing_no, bundle) in enumerate(all_entries.items()):
        entry, job = bundle["entry"], bundle["job"]
        n_boq = len([l for l in entry.bom.lines if l.category != "ROLLUP"])
        n_read = len(entry.discovered)
        suffix = " · from the saved extraction" if job.from_cache else ""
        picks_d[drawing_no] = cols[i % 2].checkbox(
            f"**{drawing_no}** — {n_boq} BOQ line(s), {n_read} read from the "
            f"drawing{suffix}",
            value=picks_d.get(drawing_no, True), key=f"dwg::{drawing_no}")

    entries = OrderedDict((k, v) for k, v in all_entries.items() if picks_d.get(k))
    if not entries:
        st.warning("No drawing ticked. Tick at least one to build a BOQ.")
        return

    st.divider()
    rows = _line_rows(entries)
    st.caption(f"{len(rows)} line(s) from {len(entries)} of "
               f"{len(all_entries)} extracted drawing(s). Untick anything that "
               f"does not belong in this bill.")

    # One box across all four fields. With a hundred-odd lines from a dozen
    # drawings, scrolling a multiselect to find "epoxy" is slower than typing
    # it, and an estimator looking for one item knows a word from it long
    # before they know which drawing it came from.
    search = st.text_input(
        "Search", key="sel_search", placeholder="Search drawing, source, "
        "group or description — e.g. 0107, rebar, epoxy, pedestal",
        help="Matches any of the four columns. Every word you type has to "
             "appear somewhere in the row, so 'rebar 0107' narrows twice.")

    f1, f2, f3, f4 = st.columns([2, 2, 1, 1])
    matching_drawings = _search_rows(rows, search)
    options = sorted({r["Drawing"] for r in matching_drawings}) or sorted(entries)
    which = f1.multiselect("Filter by drawing", options, default=[],
                           key="sel_dwg",
                           help="Narrowed by the search box above, so typing "
                                "part of a number then picking from this list "
                                "is two steps rather than a long scroll.")
    kinds = f2.multiselect("Filter by source", ["BOQ", "Read from drawing"],
                           default=[], key="sel_src")
    visible = [r for r in matching_drawings
               if (not which or r["Drawing"] in which)
               and (not kinds or r["Source"] in kinds)]

    if search and not visible:
        st.info(f"Nothing matches “{search}”. Clear the box to see all "
                f"{len(rows)} line(s) again.")
    elif search:
        st.caption(f"{len(visible)} of {len(rows)} line(s) match “{search}”.")

    picks: dict = st.session_state.setdefault("line_picks", {})
    for row in rows:
        picks.setdefault(row["key"], row["Source"] == "BOQ")

    if f3.button("Select all", use_container_width=True, key="sel_all"):
        for row in visible:
            picks[row["key"]] = True
        SC.rerun()
    if f4.button("Clear", use_container_width=True, key="sel_none"):
        for row in visible:
            picks[row["key"]] = False
        SC.rerun()

    table = pd.DataFrame([{**{"Include": picks[r["key"]]},
                           **{k: v for k, v in r.items() if k != "key"}}
                          for r in visible])
    edited = st.data_editor(
        table, hide_index=True, use_container_width=True, height=420,
        disabled=["Drawing", "Source", "Group", "Description", "UoM", "Qty"],
        column_config={"Include": st.column_config.CheckboxColumn(
            "Include", help="Ticked lines go into the BOQ")},
        key="line_editor")
    for row, include in zip(visible, edited["Include"].tolist()):
        picks[row["key"]] = bool(include)

    chosen = {k for k, v in picks.items() if v}
    st.caption(f"{len(chosen)} line(s) ticked.")

    if st.button(f"🧾 Generate one combined BOQ from {len(entries)} drawing(s)",
                 type="primary", use_container_width=True, disabled=not chosen):
        kept = _apply_selection(entries, chosen)
        if not kept:
            st.error("Nothing ticked on any drawing.")
        else:
            out = SW.write_set_workbook(
                kept, OUTPUT_DIR / "SET_BOQ.xlsx",
                project_name=project.project_name)
            st.session_state["set_boq_path"] = str(out)
            st.session_state["set_boq_built"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            st.session_state["set_boq_lines"] = len(chosen)
            st.session_state["set_boq_drawings"] = len(kept)

    _download_panel()


def _download_panel() -> None:
    """Results stay on screen until they are explicitly cleared.

    The previous version built its download buttons inside the run that produced
    them. Clicking a download triggers a rerun, the run did not happen again, so
    the buttons and the file paths went with it — which looked exactly like the
    work had been thrown away.
    """
    path = st.session_state.get("set_boq_path")
    if not path or not Path(path).exists():
        return
    path = Path(path)
    st.success(f"Consolidated BOQ built {st.session_state.get('set_boq_built', '')} "
               f"— {st.session_state.get('set_boq_drawings', 0)} drawing(s), "
               f"{st.session_state.get('set_boq_lines', 0)} line(s).")
    d1, d2 = st.columns([2, 1])
    with d1, open(path, "rb") as fh:
        st.download_button(
            "⬇️  Download consolidated BOQ (.xlsx)", data=fh.read(),
            file_name=path.name, type="primary", use_container_width=True,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.caption(f"Also saved at `{path}`. Download it as many times as you like; "
               f"nothing is removed until you press the button below.")
    report = SW.write_queue_report(
        JS.list_jobs(owner=_owner()), OUTPUT_DIR / "SET_EXTRACTION_LOG.xlsx",
        project_name=project.project_name)
    with open(report, "rb") as fh:
        st.download_button(
            "⬇️  Download extraction log (.xlsx)", data=fh.read(),
            file_name=report.name, use_container_width=True,
            key="dl_queue_report",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.caption("Which drawings were read, how long each took, and which came "
               "back from a saved extraction rather than the model.")
    with d2:
        if st.session_state.get("_confirm_clear_result"):
            if st.button("Yes, clear it", key="clear_result_yes",
                         use_container_width=True):
                for key in ("set_boq_path", "set_boq_built", "set_boq_lines",
                            "set_boq_drawings", "_confirm_clear_result"):
                    st.session_state.pop(key, None)
                SC.rerun()
            if st.button("Keep it", key="clear_result_no",
                         use_container_width=True):
                st.session_state.pop("_confirm_clear_result", None)
                SC.rerun()
        elif st.button("🧹 Clear this result", use_container_width=True,
                       key="clear_result"):
            st.session_state["_confirm_clear_result"] = True
            SC.rerun()
    if st.session_state.get("_confirm_clear_result"):
        st.warning("Clear the link to this workbook? The file stays on disk at "
                   "the path above, so this only removes it from the screen.",
                   icon="🧹")
    st.info("Every quantity in the Summary is a live link into that drawing's "
            "own activity sheet. Correct a net quantity or a wastage percentage "
            "there and the Summary and the set total follow.", icon="🔗")


_session_bar()

st.header("1 · Drawings")

# The uploader holds its own list of files that no session key can reach, so a
# cleared session gives it a new identity instead. Without this, Clear left the
# previously uploaded names sitting in the drop zone.
uploads = st.file_uploader(
    "Upload drawing PDFs", type=["pdf"], accept_multiple_files=True,
    key=f"uploader::{st.session_state.get('upload_nonce', 0)}",
    help="Saved to output/uploads/. Upload as many as you like; tick the ones "
         "to work on, and set each one's site classification.")

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
    c1, c2, c3 = st.columns([5, 2.2, 1.2])
    with c1:
        ticked = st.checkbox(label, key=key)
        st.caption(f"{path.stat().st_size / 1024 / 1024:.1f} MB · {where}")
    with c2:
        # Asked per drawing, not per submission: one package routinely mixes
        # new ground, work inside a live plant, and making good what is there,
        # and those three are tendered at different rates.
        chosen = st.selectbox(
            "Site classification", CLASSIFICATIONS,
            index=CLASSIFICATIONS.index(classification_of(path)),
            key=f"class::{path}", label_visibility="collapsed",
            help="Groups this drawing in the Location Based Report sheet of "
                 "the workbook.")
        # Written through on every run rather than on change: the store is the
        # only copy that survives the tab, and a value the user can see in the
        # dropdown but that never reached disk is the worst of both.
        if chosen != CLASS.get(path):
            CLASS.set_for(path, chosen)
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
    # No drawing ticked is not the same as nothing to do. Work queued in an
    # earlier sitting is still there, and so is everything already extracted —
    # the whole point of a queue that outlives the tab.
    if JS.list_jobs(owner=_owner()):
        st.info("Tick a drawing to work on it, or pick up where you left off "
                "below.")
        st.header("2 · Extraction queue")
        _queue_status()
        _results_panel()
    else:
        st.info("Tick a drawing to work on it.")
    st.stop()

if len(selected) > 1:
    st.session_state.pop("extraction", None)
    _queue_panel(selected)
    _results_panel()
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
    # A sheet that kept its own text layer is read from characters, not pixels.
    # Knowing that here is what lets the page say "no model calls" honestly, and
    # what stops it refusing to extract when Ollama happens to be down.
    from extractors import text_layer as TL
    reads_from_text = TL.has_usable_text(page)
finally:
    doc.close()

model_calls = 0 if reads_from_text else len(montages) + 2

c1, c2, c3, c4 = st.columns(4)
c1.metric("Sheet", info["sheet_size"])
c2.metric("Text blocks found", len(blocks))
c3.metric("Callout candidates", len(candidates))
c4.metric("Model calls needed", model_calls)

if reads_from_text:
    st.success("This sheet kept its own text layer, so it is read from the "
               "characters themselves: exact values, exact positions, in about "
               "a fifth of a second. **No model calls at all**, and the local "
               "model does not need to be running.", icon="⚡")
elif not info["has_text_layer"]:
    st.info("This PDF has no text layer — the text is drawn as curves, so a text "
            "parser would return nothing. The blocks above were located from the "
            "drawing's vector geometry, which costs no model time.")
if not candidates and not reads_from_text:
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

if reads_from_text:
    st.caption("The local model is not consulted for this sheet.")
elif ok:
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
if reads_from_text and cfg.strategy == "montage":
    n_calls = 0
with e2:
    if n_calls:
        st.caption(f"About **{n_calls} model calls ≈ {n_calls * 48 // 60} min "
                   f"{n_calls * 48 % 60} s** on this machine. The window can "
                   f"stay in the background, but leave it open.")
    else:
        st.caption("**Under a second**, and no model time at all. Choosing the "
                   "sweep profile would override that and look at pixels "
                   "instead, which is only worth doing if the text layer turns "
                   "out to be wrong.")

# Only the model's absence blocks extraction, and only for a sheet that needs
# it. Refusing to read a drawing whose own text layer answers the question was
# the reason five of these sheets looked unextractable.
if st.button("🔍 Extract drawing", type="primary", disabled=not (ok or n_calls == 0),
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

# What the drawing did not say. Shown before the button, and never in front of
# it: a sections sheet gives no pedestal and no slab, and refusing to produce a
# workbook for it was how five drawings in this set produced nothing at all. A
# bill with a stated gap tells an engineer which figure to supply and where.
_gap_project = Project(project_name="", drawing_no="")
_gap_project.pedestals, _gap_project.grade_slabs = pedestals, slabs
gap_report = GAPS.report_for(_gap_project, result, derived=derived_preview,
                             placeholder_height_m=QV.PLACEHOLDER_HEIGHT_M)
if gap_report.needs_attention:
    with st.expander(f"⚠️ {gap_report.needs_attention} gap(s) and assumption(s) "
                     f"— the workbook is produced anyway", expanded=not (pedestals or slabs)):
        st.caption(gap_report.headline())
        st.dataframe(pd.DataFrame(gap_report.as_rows()), hide_index=True,
                     use_container_width=True)
        st.caption("This table becomes the **Gaps_and_Assumptions** sheet in the "
                   "workbook, right after the Summary.")

if not (pedestals or slabs):
    st.info("No pedestal or grade slab on this sheet. The workbook is still "
            "worth having: it carries whatever the drawing specifies on its "
            "Drawing_Items sheet, and the gaps above on their own sheet.",
            icon="ℹ️")

if st.button("🧾 Generate BOQ Excel", type="primary", use_container_width=True):
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
            WE.append_gaps_sheet(written, gap_report)
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
