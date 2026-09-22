"""Drawings tab: the set, the state of each drawing, and one drawing up close.

Two views, and which one shows is a choice rather than a side effect:

* **The list** — every drawing in the uploads folder, `Drawings/` and the
  project root, with a status badge, its site classification, a tick box for
  batch actions and an Open button.
* **One drawing** — opened with Open: the sheet, its reading (from the saved
  extraction, or queued for the worker), the review grids and its own BOQ.

Ticking used to be the switch. None ticked showed the queue, one showed a
drawing, two or more showed a batch, and ticking a second drawing silently threw
away the one under review. Now a tick only ever selects.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from core import classification as CLASS
from core import drawing_status as DS
from core import extract_cache as CACHE
from core import gaps as GAPS
from core import jobstore as JS
from core import library as LIB
from core.bom_builder import build_bom
from core.derivation import apply_derived, default_rules, derive, rules_from_findings
from core.excel_writer import write_workbook
from core.filename import build_output_path
from core.models import GradeSlab, Pedestal, Project
from extractors import pdf_to_image as R
from extractors import qwen_vision as QV
from extractors import rag_examples
from extractors import sheet_grid as SG
from extractors import st_compat as SC
from extractors import vector_text as VT
from extractors import verification as VER
from extractors import workbook_extras as WE
from extractors.models import ExtractionResult
from extractors.qwen_vision import PROFILES, ExtractionConfig
from ui import plain
from ui.workspace import common as C

PROFILE_CHOICES = ["thorough", "quick", "sweep", "fast"]
PROFILE_HELP = ("thorough — locate text, then transcribe it (default, fastest). "
                "quick — largest text only. "
                "sweep / fast — blind tile sweep for scanned sheets; much slower.")


def render() -> None:
    st.session_state.pop("_sidebar_drawing", None)
    _uploader()
    drawings = LIB.find()
    by_path = {str(d.path): d for d in drawings}
    opened = st.session_state.get("open_drawing")
    if opened and opened in by_path:
        _detail(by_path[opened], drawings)
        return
    if opened:                        # deleted, moved or cleared since
        st.session_state.pop("open_drawing", None)
    _list(drawings)


def sidebar() -> None:
    """What the open drawing holds, under the estimate summary."""
    info = st.session_state.get("_sidebar_drawing")
    if not info:
        return
    with st.sidebar:
        st.subheader("This drawing")
        st.write(f"Pedestals: {info['pedestals']}")
        st.write(f"Grade slabs: {info['slabs']}")
        st.write(f"Extraction: {info['elapsed']:.0f} s")
        st.caption("Excavation, joints, sump, embedments and manual rebar are "
                   "entered under **BOQ → Project estimate**.")


# ======================================================================
# Status
# ======================================================================
@st.cache_data(show_spinner=False, max_entries=2048)
def _attention(fp: str, cache_modified_ns: int) -> int:
    """Gaps and assumptions a saved reading leaves for a person — the same
    count the workbook's Gaps_and_Assumptions sheet carries."""
    result = CACHE.load(fp)
    if result is None:
        return 0
    proj = Project(project_name="", drawing_no="")
    QV.merge_into_project(proj, result)
    return GAPS.report_for(proj, result, derived=[],
                           placeholder_height_m=QV.PLACEHOLDER_HEIGHT_M).needs_attention


def saved_fingerprint(path: Path, latest: JS.Job | None, profile: str = "thorough",
                      page: int = 0) -> str | None:
    """The fingerprint of a saved reading of this file *as it is now*.

    The newest finished job counts only if the file still has the bytes it was
    read from; failing that, a reading under the chosen profile. A drawing
    reissued under the same name is a different drawing.
    """
    modified = path.stat().st_mtime_ns
    candidates = []
    if (latest is not None and latest.state == JS.DONE and latest.page == page
            and latest.fingerprint == C.fingerprint(str(path), modified,
                                                    latest.profile, page)):
        candidates.append(latest.fingerprint)
    candidates.append(C.fingerprint(str(path), modified, profile, page))
    for fp in candidates:
        if CACHE.cache_path(fp).is_file():
            return fp
    return None


def statuses(drawings: list[LIB.Drawing]) -> dict[str, DS.Status]:
    latest = JS.latest_by_drawing(owner=C.owner())
    out = {}
    for d in drawings:
        job = latest.get((JS.drawing_key(d.path), 0))
        fp = saved_fingerprint(d.path, job)
        attention = (_attention(fp, CACHE.cache_path(fp).stat().st_mtime_ns)
                     if fp else 0)
        out[str(d.path)] = DS.status_for(latest=job, has_saved=fp is not None,
                                         attention=attention)
    return out


@st.fragment(run_every="3s")
def _live_strip() -> None:
    """Progress of anything being read, and a nudge to redraw the badges.

    Only this strip refreshes on the timer. When a drawing changes state — picked
    up, finished, failed — the whole page redraws once so the badges catch up,
    rather than the whole list redrawing every three seconds.
    """
    live = JS.list_jobs(owner=C.owner(), states=[JS.QUEUED, JS.RUNNING])
    signature = sorted((j.id, j.state) for j in live)
    previous = st.session_state.get("_ws_signature")
    st.session_state["_ws_signature"] = signature
    if previous is not None and previous != signature:
        st.rerun()
    running = next((j for j in live if j.state == JS.RUNNING), None)
    waiting = sum(1 for j in live if j.state == JS.QUEUED)
    if running is not None:
        st.progress(min(1.0, running.progress_pct / 100.0),
                    text=f"Reading {running.drawing_name} — "
                         f"{running.progress_pct:.0f}% · {waiting} waiting · "
                         f"details on the Queue tab")
    elif waiting:
        st.caption(f"⏳ {waiting} drawing(s) waiting to be read — details on the "
                   f"Queue tab.")


# ======================================================================
# Upload
# ======================================================================
def _uploader() -> None:
    uploads_dir = LIB.upload_dir()
    uploads_dir.mkdir(parents=True, exist_ok=True)
    opened = bool(st.session_state.get("open_drawing"))
    if opened:
        return
    with st.expander("⬆️ Upload drawing PDFs", expanded=not any(LIB.find())):
        # The uploader holds its own list of files that no session key can
        # reach, so a cleared session gives it a new identity instead.
        uploads = st.file_uploader(
            "Upload drawing PDFs", type=["pdf"], accept_multiple_files=True,
            key=f"uploader::{st.session_state.get('upload_nonce', 0)}",
            label_visibility="collapsed",
            help="Saved to the uploads folder. Upload as many as you like.")
    # Streamlit hands back the uploader's whole file list on *every* rerun, not
    # just the run where the files arrived. Acting on it unconditionally
    # re-ticked every upload on each rerun — so unticking one was undone at
    # once — and re-read every file to compare bytes. Each upload is handled
    # exactly once.
    seen: set[str] = st.session_state.setdefault("_seen_uploads", set())
    for up in uploads or []:
        signature = f"{up.name}:{up.size}"
        if signature in seen:
            continue
        seen.add(signature)
        dest = uploads_dir / up.name
        dest.write_bytes(up.getvalue())
        _picks().add(str(dest))                          # newly added = ticked


# ======================================================================
# The list
# ======================================================================
def _picks() -> set[str]:
    """Which drawings are ticked — kept here, not in the checkboxes.

    Streamlit forgets a widget's value on any run that does not draw it, and
    the list is not drawn while a drawing is open. Kept only in the checkboxes,
    the selection vanished every time someone opened a drawing to look at it
    and came back. The checkboxes are a view of this set.
    """
    return st.session_state.setdefault("_picks", set())


def _list(drawings: list[LIB.Drawing]) -> None:
    if not drawings:
        st.info("Upload a drawing PDF to begin, or put the set in the Drawings "
                "folder.")
        return

    # The strip may redraw the page when the queue changes, so it goes first:
    # a one-time note shown before it would be spent on a run nobody sees.
    _live_strip()
    note = st.session_state.pop("_queued_note", None)
    if note:
        st.success(note, icon="✅")

    badges = statuses(drawings)
    counts = DS.tally(badges.values())
    st.caption(f"**{len(drawings)} drawing(s)** — " + " · ".join(
        f"{n} {DS.COUNT_WORDS[k]}" for k, n in counts.items()))

    # Select-all / clear write the checkbox keys before the widgets exist, which
    # is the only way to drive a widget from a button in Streamlit. The action
    # buttons are filled in after the rows, once the selection is known.
    picks = _picks()
    t1, t2, t3, t4, t5 = st.columns([1, 1, 2.6, 1.2, 1.3])
    if t1.button("Select all", use_container_width=True, key="lib_all"):
        picks.update(str(d.path) for d in drawings)
        _forget_boxes(drawings)
        st.rerun()
    if t2.button("Clear", use_container_width=True, key="lib_none"):
        picks.difference_update(str(d.path) for d in drawings)
        _forget_boxes(drawings)
        st.rerun()
    with t4.popover("⚙ Options", use_container_width=True):
        profile = st.selectbox(
            "Profile", options=sorted(PROFILES),
            index=sorted(PROFILES).index("thorough"), key="q_profile",
            help="thorough and quick use the montage strategy and are the fast "
                 "routes. sweep tiles the whole sheet and is the slow fallback "
                 "for a drawing nothing else finds callouts on.")
        force = st.checkbox("Re-read even if saved", key="q_force",
                            help="Tick this when a drawing has been reissued "
                                 "under the same file name.")

    labels = LIB.labels(drawings)
    selected: list[LIB.Drawing] = []
    for d in drawings:
        key = str(d.path)
        c1, c2, c3, c4 = st.columns([5, 1.6, 2.1, 1.1], vertical_alignment="center")
        with c1:
            # Seeded from the selection only when the box has no state of its
            # own — after a run that did not draw it. Passing the selection as
            # `value=` instead looks equivalent and is not: the value is part of
            # the widget's identity, so a changed selection made a *new* box
            # that snapped back to its default, and unticking was undone on the
            # next rerun (tests/test_drawing_library.py guards this).
            box_key = f"pick::{d.path}"
            if box_key not in st.session_state:
                st.session_state[box_key] = key in picks
            ticked = st.checkbox(labels[d.path], key=box_key)
            st.caption(f"{d.size_mb:.1f} MB · {d.folder}")
        if ticked:
            picks.add(key)
        else:
            picks.discard(key)
        c2.markdown(C.badge_html(badges[key]), unsafe_allow_html=True)
        with c3:
            chosen = st.selectbox(
                "Site classification", C.CLASSIFICATIONS,
                index=C.CLASSIFICATIONS.index(C.classification_of(d.path)),
                key=f"class::{d.path}", label_visibility="collapsed",
                help="Groups this drawing in the Location Based Report sheet of "
                     "the workbook.")
            # Written through on every run rather than on change: the store is
            # the only copy that survives the tab, and a value the user can see
            # in the dropdown but that never reached disk is the worst of both.
            if chosen != CLASS.get(d.path):
                CLASS.set_for(d.path, chosen)
        if c4.button("Open", key=f"open::{d.path}", use_container_width=True):
            st.session_state["open_drawing"] = key
            st.rerun()
        if ticked:
            selected.append(d)

    st.caption("**Ready** — read, nothing flagged · **Needs review** — read, "
               "with gaps or assumptions to check · **Not read** — no reading "
               "of this version of the file yet.")

    with t3:
        if st.button(f"➕ Add {len(selected)} drawing(s) to the queue",
                     type="primary", use_container_width=True, key="q_add",
                     disabled=not selected):
            _add_to_queue([d.path for d in selected], profile=profile, force=force)
            st.rerun()
    if selected and not force:
        ready = sum(1 for d in selected if badges[str(d.path)].has_reading)
        if ready:
            st.success(f"{ready} of the {len(selected)} selected have been read "
                       f"before and will be ready straight away — no model "
                       f"time.", icon="⚡")

    _delete(selected, t5)


def _forget_boxes(drawings: list[LIB.Drawing]) -> None:
    """Drop the checkboxes' own state so they redraw from the selection set."""
    for d in drawings:
        st.session_state.pop(f"pick::{d.path}", None)


def _add_to_queue(paths: list[Path], *, profile: str, force: bool) -> None:
    """Queue the selection. Drawings read before are filed as done at once.

    That is bookkeeping, not reading: the saved extraction already exists, so
    there is nothing for the worker to do and no reason to make a drawing that
    opens instantly wait behind one that takes six minutes — or wait at all
    when the worker is not running.
    """
    added = JS.enqueue(paths, owner=C.owner(), profile=profile, force=force)
    instant = 0
    if not force:
        for job in added:
            if CACHE.load(job.fingerprint) is not None:
                JS.finish(job.id, from_cache=True)
                instant += 1
    skipped = len(paths) - len(added)
    parts = [f"Added {len(added)} drawing(s)."]
    if instant:
        parts.append(f"{instant} were read before and are ready now.")
    if len(added) - instant:
        parts.append(f"{len(added) - instant} are waiting for the reader — "
                     f"follow them on the Queue tab.")
    if skipped:
        parts.append(f"{skipped} were already waiting or being read.")
    st.session_state["_queued_note"] = " ".join(parts)


def _delete(selected: list[LIB.Drawing], slot) -> None:
    """Uploads only. Drawings someone put in the project are not ours to remove."""
    deletable = [d.path for d in selected if d.deletable]
    if deletable:
        with slot:
            if st.button(f"🗑 Delete {len(deletable)}", use_container_width=True,
                         key="lib_delete"):
                st.session_state["_confirm_delete"] = True
    elif selected:
        st.caption("Only uploaded files can be deleted here. Drawings in the "
                   "Drawings folder or the project root are left alone.")

    if not (st.session_state.get("_confirm_delete") and deletable):
        return
    st.warning(f"Delete **{len(deletable)}** uploaded file(s) permanently? "
               f"{', '.join(q.name for q in deletable)}", icon="🗑")
    d1, d2, _ = st.columns([1, 1, 4])
    if d1.button("Yes, delete", use_container_width=True, key="lib_delete_yes"):
        seen: set[str] = st.session_state.setdefault("_seen_uploads", set())
        for q in deletable:
            size = q.stat().st_size if q.exists() else 0
            try:
                q.unlink()
            except OSError as exc:                      # noqa: PERF203
                st.error(f"Could not delete {q.name}: {exc}")
            st.session_state.pop(f"pick::{q}", None)
            _picks().discard(str(q))
            # Forget the upload signature too, so uploading the same file again
            # later is treated as new rather than silently skipped.
            seen.discard(f"{q.name}:{size}")
        st.session_state["_confirm_delete"] = False
        st.rerun()
    if d2.button("Cancel", use_container_width=True, key="lib_delete_no"):
        st.session_state["_confirm_delete"] = False
        st.rerun()


# ======================================================================
# One drawing
# ======================================================================
def _detail(drawing: LIB.Drawing, drawings: list[LIB.Drawing]) -> None:
    path = drawing.path
    proj = C.project()
    _detail_header(drawing, drawings)
    proj.pdf_source_filename = path.name
    proj.pdf_source_path = str(path)

    sheet = _sheet(path)
    result = _read(path, sheet)
    if result is None:
        return
    review = _review(result, proj)
    _generate(path, sheet, result, review, proj)
    _keep(path, result, review, proj)
    st.session_state["_sidebar_drawing"] = {
        "pedestals": len(review["pedestals"]), "slabs": len(review["slabs"]),
        "elapsed": result.total_elapsed_s}


def _detail_header(drawing: LIB.Drawing, drawings: list[LIB.Drawing]) -> None:
    keys = [str(d.path) for d in drawings]
    here = keys.index(str(drawing.path))
    b1, b2, b3, _ = st.columns([1.4, 1, 1, 4])
    if b1.button("← All drawings", use_container_width=True, key="close_drawing"):
        st.session_state.pop("open_drawing", None)
        st.rerun()
    if b2.button("‹ Previous", use_container_width=True, key="prev_drawing",
                 disabled=here == 0):
        st.session_state["open_drawing"] = keys[here - 1]
        st.rerun()
    if b3.button("Next ›", use_container_width=True, key="next_drawing",
                 disabled=here == len(keys) - 1):
        st.session_state["open_drawing"] = keys[here + 1]
        st.rerun()

    status = statuses([drawing])[str(drawing.path)]
    h1, h2 = st.columns([5, 2], vertical_alignment="center")
    h1.markdown(f"### {drawing.name} &nbsp; {C.badge_html(status)}",
                unsafe_allow_html=True)
    h1.caption(f"{drawing.size_mb:.1f} MB · {drawing.folder} · drawing "
               f"{here + 1} of {len(keys)}")
    with h2:
        chosen = st.selectbox(
            "Site classification", C.CLASSIFICATIONS,
            index=C.CLASSIFICATIONS.index(C.classification_of(drawing.path)),
            key=f"class::{drawing.path}",
            help="Groups this drawing in the Location Based Report sheet.")
        if chosen != CLASS.get(drawing.path):
            CLASS.set_for(drawing.path, chosen)


# ---------------------------------------------------------------- 1 · Sheet
@st.cache_data(show_spinner="Looking at the sheet…", max_entries=24)
def sheet_analysis(path: str, modified_ns: int, page_idx: int) -> dict:
    """Everything shown about a sheet before it is read.

    Pure geometry, no model — but about a second of it, and the page used to
    pay that second again on every click in the review grids. Keyed by the
    file's modification time, so a replaced drawing is looked at afresh.
    """
    from extractors import text_layer as TL
    doc, page = R.open_page(path, page_idx)
    try:
        blocks = VT.find_text_blocks(page)
        candidates = VT.callout_candidates(blocks)
        return {
            "info": R.page_info(page),
            "preview": R.render_full(page, 1500).png,
            "title_block": R.render_region(page, R.TITLE_BLOCK, 900,
                                           max_pixels=800_000).png,
            "n_blocks": len(blocks),
            "n_candidates": len(candidates),
            "n_montages": len(VT.build_montages(page, candidates)),
            # A sheet that kept its own text layer is read from characters, not
            # pixels — "no model calls" is only honest if this says so.
            "reads_from_text": TL.has_usable_text(page),
        }
    finally:
        doc.close()


def _sheet(path: Path) -> dict:
    st.subheader("1 · Sheet")
    doc, _page0 = R.open_page(path, 0)
    n_pages = doc.page_count
    doc.close()
    page_no = 1
    if n_pages > 1:
        page_no = st.number_input("Page", min_value=1, max_value=n_pages, value=1,
                                  key=f"page::{path}")
    page_idx = page_no - 1
    modified_ns = path.stat().st_mtime_ns

    sheet = dict(sheet_analysis(str(path), modified_ns, page_idx))
    sheet.update(page_no=page_no, page_idx=page_idx, modified_ns=modified_ns)
    info = sheet["info"]
    reads_from_text = sheet["reads_from_text"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sheet", info["sheet_size"])
    c2.metric("Text blocks found", sheet["n_blocks"])
    c3.metric("Callout candidates", sheet["n_candidates"])
    c4.metric("Model calls needed", 0 if reads_from_text else sheet["n_montages"] + 2)

    if reads_from_text:
        st.success("This sheet kept its own text layer, so it is read from the "
                   "characters themselves: exact values, exact positions, in "
                   "about a fifth of a second. **No model calls at all**, and "
                   "the local model does not need to be running.", icon="⚡")
    elif not info["has_text_layer"]:
        st.info("This PDF has no text layer — the text is drawn as curves, so a "
                "text parser would return nothing. The blocks above were located "
                "from the drawing's vector geometry, which costs no model time.")
    # Worded against the metrics right above it: "No text blocks located"
    # beside "13 text blocks found" read as the page contradicting itself.
    if not reads_from_text and not sheet["n_blocks"]:
        st.warning("No text was found on this sheet. If it is a scan rather than "
                   "CAD output, choose the **sweep** profile below — it looks at "
                   "the whole sheet instead, but takes far longer.")
    elif not reads_from_text and not sheet["n_candidates"]:
        st.warning(f"{sheet['n_blocks']} text block(s) were found, but none of "
                   f"them looks like a quantity callout such as “P3(350x350) "
                   f"11Nos”. If you expected quantities on this sheet, choose the "
                   f"**sweep** profile below — it looks at the whole sheet "
                   f"instead, but takes far longer.")

    pv1, pv2 = st.columns([3, 1])
    SC.image(pv1, sheet["preview"], caption=f"{path.name} — page {page_no}")
    SC.image(pv2, sheet["title_block"], caption="Title block (read first)")
    return sheet


# ---------------------------------------------------------------- 2 · Read
@st.fragment(run_every="2s")
def _reading_progress(job_id: str) -> None:
    """One drawing being read, live. Reruns itself; the page does not."""
    job = JS.get(job_id)
    if job is None or job.state not in (JS.QUEUED, JS.RUNNING):
        # Finished, failed, stopped or cleared: the whole page knows what to
        # show next, so hand back to it.
        if job is not None and job.state == JS.DONE:
            saved = CACHE.load(job.fingerprint)
            if saved is not None:
                st.session_state["extraction"] = saved
                st.session_state["extraction_origin"] = "read"
        st.rerun()

    if job.state == JS.RUNNING:
        st.progress(min(1.0, job.progress_pct / 100.0),
                    text=f"Reading — {job.progress_pct:.0f}% · "
                         f"{job.stage or 'working'} · "
                         f"{C.fmt_duration(time.time() - job.started_at)} elapsed")
    else:
        waiting = JS.list_jobs(owner=job.owner, states=[JS.QUEUED, JS.RUNNING])
        ahead = sum(1 for j in waiting if j.id != job.id and (
            j.state == JS.RUNNING or j.position < job.position))
        if JS.is_paused(job.owner):
            st.info("The queue is paused, so this drawing is waiting. Resume it "
                    "to start reading.", icon="⏸")
            if st.button("▶ Resume the queue", key="x_resume"):
                JS.set_paused(False, owner=job.owner)
                st.rerun()
        elif (not C.worker_alive(waiting)
              and time.time() - job.created_at > C.WORKER_GRACE_S):
            plain.show(C.no_worker_problem("this drawing is"), level="warning",
                       icon="⏳")
        elif ahead:
            st.info(f"Waiting its turn — {ahead} drawing(s) ahead of it.", icon="⏳")
        else:
            st.info("Starting…", icon="⏳")

    if st.button("⏹ Stop", key="x_stop"):
        JS.request_cancel(job.id)
        st.rerun()
    st.caption("You can leave this drawing or close the tab. The reading carries "
               "on, and the result is saved when it finishes.")


def _queue_reading(pdf: Path, *, profile: str, page: int, force: bool = False) -> None:
    """Put one drawing on the queue and remember that this session asked."""
    added = JS.enqueue([pdf], owner=C.owner(), profile=profile, page=page,
                       force=force)
    if added:
        st.session_state["_reading_job"] = added[0].id


def _read(path: Path, sheet: dict) -> ExtractionResult | None:
    """The drawing's reading: from the saved extraction, or via the queue.

    The page never reads a drawing itself. Running the model in here is what
    froze the app and heated the laptop, and it held the tab hostage for the
    whole read.
    """
    st.subheader("2 · Read the drawing")
    page_idx = sheet["page_idx"]
    read_key = f"{path}#{page_idx}"
    if st.session_state.get("_last_read_key") != read_key:
        # The grids' pending edits are row-by-row changes to *that* drawing's
        # rows. Carried over, they would be applied to the next drawing's.
        for key in ("extraction", "extraction_origin", "ped_edit", "slab_edit"):
            st.session_state.pop(key, None)
        st.session_state["_last_read_key"] = read_key

    e1, e2 = st.columns([1, 2])
    with e1:
        profile = st.selectbox("Profile", PROFILE_CHOICES, key="x_profile",
                               help=PROFILE_HELP)
    cfg = ExtractionConfig(**PROFILES[profile].__dict__)
    n_calls = (sheet["n_montages"] + 2 if cfg.strategy == "montage"
               else len(QV._tile_plan(cfg)) + 2)
    if sheet["reads_from_text"] and cfg.strategy == "montage":
        n_calls = 0
    needs_model = n_calls > 0
    with e2:
        if n_calls:
            st.caption(f"About **{n_calls} model calls ≈ {n_calls * 48 // 60} min "
                       f"{n_calls * 48 % 60} s** on this machine. You can close "
                       f"the tab while it reads — the result is saved and "
                       f"waiting when you come back.")
        else:
            st.caption("**Under a second**, and no model time at all. Choosing "
                       "the sweep profile would override that and look at pixels "
                       "instead, which is only worth doing if the text layer "
                       "turns out to be wrong.")

    model_ok, model_msg = C.model_health()
    latest = JS.latest_by_drawing(owner=C.owner()).get(
        (JS.drawing_key(path), page_idx))
    live = latest if latest is not None and latest.state in (JS.QUEUED, JS.RUNNING) else None

    # Cache first — which is what makes a drawing read last week, or by a
    # colleague, open in a moment with the model switched off.
    if "extraction" not in st.session_state and live is None:
        fp = saved_fingerprint(path, latest, profile, page_idx)
        saved = CACHE.load(fp) if fp else None
        if saved is not None:
            # "Read" when this session asked for it and it has just come back;
            # "saved" when it was already there before anyone asked.
            mine = latest is not None and latest.id == st.session_state.get("_reading_job")
            st.session_state["extraction"] = saved
            st.session_state["extraction_origin"] = "read" if mine else "saved"

    if live is not None:
        _reading_progress(live.id)
        return None

    result: ExtractionResult | None = st.session_state.get("extraction")
    if result is None:
        failed_before = latest is not None and latest.state == JS.FAILED
        if failed_before:
            plain.show(plain.Problem(plain.job_problem(latest.error),
                                     detail=latest.error), icon="⚠️")
        if needs_model and not model_ok:
            plain.show(plain.model_problem(model_msg), level="warning", icon="🔌",
                       extra=C.MODEL_OFFLINE_NOTE)
        elif not needs_model:
            st.caption("The local model is not consulted for this sheet.")
        if st.button("🔍 Try reading it again" if failed_before
                     else "🔍 Read this drawing",
                     type="primary", use_container_width=True, key="x_read",
                     disabled=needs_model and not model_ok):
            _queue_reading(path, profile=profile, page=page_idx)
            st.rerun()
        return None

    r1, r2 = st.columns([3, 1])
    with r1:
        if st.session_state.get("extraction_origin") == "saved":
            st.success("Opened the saved reading of this drawing — no model time "
                       "used.", icon="⚡")
        else:
            st.success("Read and saved. Next time it opens instantly.", icon="✅")
    with r2:
        if st.button("🔁 Read again", use_container_width=True, key="x_reread",
                     disabled=needs_model and not model_ok,
                     help=f"Reads the drawing again with the {profile} profile and "
                          f"replaces the saved result. Use it after a reissue "
                          f"under the same name, or to try a different profile."):
            _queue_reading(path, profile=profile, page=page_idx, force=True)
            st.session_state.pop("extraction", None)
            st.session_state.pop("extraction_origin", None)
            st.rerun()
    return result


# ------------------------------------------------------- 3 · Review and correct
def _review(result: ExtractionResult, proj: Project) -> dict:
    st.subheader("3 · Review and correct")
    st.caption(f"{result.model} · {result.profile} · {result.total_elapsed_s:.0f} s · "
               f"{len([r for r in result.responses if not r.skipped])} model calls")

    tb = result.title_block
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Drawing no", tb.drawing_no or "—")
    m2.metric("Revision", tb.revision or "—")
    m3.metric("Date", tb.date or "—")
    m4.metric("Pedestals found", len(result.pedestals))

    with st.expander("Project details (edit if the model misread them)",
                     expanded=False):
        d1, d2 = st.columns(2)
        with d1:
            tb.drawing_no = st.text_input("Drawing no", value=tb.drawing_no)
            tb.revision = st.text_input("Revision", value=tb.revision)
            tb.date = st.text_input("Date (YYYY-MM-DD)", value=tb.date)
        with d2:
            tb.project_name = st.text_area("Project name", value=tb.project_name,
                                           height=90)
            tb.prepared_by = st.text_input("Prepared by",
                                           value=tb.prepared_by or proj.prepared_by)

    # ---------- Pedestals ----------
    st.markdown("**Pedestals**")
    st.warning("**Pedestal height is not printed on these callouts.** The model "
               f"cannot read it, so it is pre-filled with "
               f"{QV.PLACEHOLDER_HEIGHT_M:.3f} m. Enter the real heights below — "
               f"concrete, formwork and rebar are wrong until you do.")
    ped_rows = [{
        "tag": p.tag,
        "length_m": round(p.length_mm / 1000, 3),
        "width_m": round(p.width_mm / 1000, 3),
        "height_m": (round(p.height_mm / 1000, 3) if p.height_mm
                     else QV.PLACEHOLDER_HEIGHT_M),
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
            "length_m": st.column_config.NumberColumn("Length (m)", min_value=0.001,
                                                      step=0.05),
            "width_m": st.column_config.NumberColumn("Width (m)", min_value=0.001,
                                                     step=0.05),
            "height_m": st.column_config.NumberColumn(
                "Height (m) ⚠", min_value=0.001, step=0.05,
                help="Not on the drawing — enter it"),
            "quantity": st.column_config.NumberColumn("Nos", min_value=1, step=1),
            "grade": st.column_config.SelectboxColumn("Grade",
                                                      options=C.CONCRETE_GRADES),
            "rebar_coefficient_kg_per_m3": st.column_config.NumberColumn(
                "Rebar (kg/m³)", min_value=0.0, step=5.0),
            "callout": st.column_config.TextColumn("Verbatim callout",
                                                   disabled=True),
        })

    # ---------- Grade slab ----------
    st.markdown("**Grade slab**")
    slab_rows = [{
        "tag": s.tag,
        "length_m": round((s.length_mm or 0) / 1000, 3),
        "width_m": round((s.width_mm or 0) / 1000, 3),
        "thickness_m": round((s.thickness_mm or 0) / 1000, 3),
        "grade": "RCC_M30",
        "rebar_coefficient_kg_per_m3": 80.0,
    } for s in result.grade_slabs if s.is_usable()]
    slab_df = st.data_editor(
        pd.DataFrame(slab_rows, columns=["tag", "length_m", "width_m",
                                         "thickness_m", "grade",
                                         "rebar_coefficient_kg_per_m3"]),
        num_rows="dynamic", use_container_width=True, key="slab_edit",
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "length_m": st.column_config.NumberColumn("Length (m)", min_value=0.001,
                                                      step=0.1),
            "width_m": st.column_config.NumberColumn("Width (m)", min_value=0.001,
                                                     step=0.1),
            "thickness_m": st.column_config.NumberColumn(
                "Thickness (m)", min_value=0.001, step=0.025),
            "grade": st.column_config.SelectboxColumn("Grade",
                                                      options=C.CONCRETE_GRADES),
            "rebar_coefficient_kg_per_m3": st.column_config.NumberColumn(
                "Rebar (kg/m³)", min_value=0.0, step=5.0),
        })
    if result.grade_slabs:
        st.caption("Read off the plan view rather than a callout — check it "
                   "against the dimension string on the sheet.")

    _discovered(result)
    _findings(result)
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

    pedestals, ped_err = _rows_to_pedestals(ped_df)
    slabs, slab_err = _rows_to_slabs(slab_df)
    for e in ped_err + slab_err:
        st.error(e)
    return {"tb": tb, "pedestals": pedestals, "slabs": slabs}


def _discovered(result: ExtractionResult) -> None:
    """What the sheet itself specifies, whatever that turns out to be — on a
    details or sections sheet it is the only thing there is."""
    disc = result.discovery or {}
    items = disc.get("items") or []
    if not items:
        return
    with st.expander(f"Read from this drawing — {len(items)} item(s), "
                     f"{disc.get('measured', 0)} with a stated quantity",
                     expanded=True):
        st.caption("Named by the drawing rather than by a preset list. A blank "
                   "quantity means the sheet states the specification but not "
                   "the extent — supply the area, length or count and the row "
                   "becomes a quantity. Every row keeps the text it came from.")
        st.dataframe(pd.DataFrame([{
            "Description": row["description"], "UoM": row["uom"],
            "Qty stated": row["qty"], "Basis": row["basis"],
            "Seen": row["occurrences"], "Grid ref": row["grid_ref"],
            "Source text": row["source"],
        } for row in items]), hide_index=True, use_container_width=True)
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


def _findings(result: ExtractionResult) -> None:
    findings = result.findings or {}
    if not any(findings.get(k) for k in findings):
        return
    with st.expander("Other things read from the drawing (not added to the BOQ)",
                     expanded=False):
        st.caption("These are recognised but incomplete — a curb wall callout "
                   "gives thickness and height but not its run length, a sump "
                   "plan dimension says nothing about depth. Enter them under "
                   "**BOQ → Project estimate**, where you can supply the "
                   "missing figures.")
        labels = {"curb_walls": "Curb walls", "sumps": "Sumps", "levels": "Levels",
                  "insert_plates": "Insert plates", "epoxy": "Epoxy coating",
                  "rebar": "Rebar callouts", "thicknesses": "Thickness notes"}
        for key, label in labels.items():
            rows = findings.get(key) or []
            if rows:
                st.markdown(f"**{label}**")
                st.dataframe(pd.DataFrame(rows), hide_index=True,
                             use_container_width=True)


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


# -------------------------------------------------- 4 · This drawing's BOQ
def _generate(path: Path, sheet: dict, result: ExtractionResult, review: dict,
              proj: Project) -> None:
    st.subheader("4 · Generate this drawing's BOQ")
    tb, pedestals, slabs = review["tb"], review["pedestals"], review["slabs"]

    untouched = [p.tag for p in pedestals
                 if abs(p.height_m - QV.PLACEHOLDER_HEIGHT_M) < 1e-9]
    if untouched:
        st.warning(f"Still at the placeholder height: **{', '.join(untouched)}**. "
                   f"The workbook will carry an UNVERIFIED DRAFT banner saying so.")

    st.markdown("**Derive the quantities the drawing does not print**")
    st.caption("Excavation, blinding, fill, liner, coating, curb wall and joints "
               "all follow from the slab geometry plus site convention. These are "
               "assumptions, not readings — tick only what applies, and check "
               "the Derivation sheet in the workbook.")
    if "x_rules" not in st.session_state:
        st.session_state.x_rules = rules_from_findings(default_rules(),
                                                       result.findings)
    x_rules = st.session_state.x_rules
    C.rule_toggles(x_rules, key_prefix="xr_", disabled=not slabs)
    if not slabs:
        st.caption("These need a grade slab — add one in the grid above to "
                   "enable them.")

    preview_project = Project(project_name="", drawing_no="")
    preview_project.grade_slabs = slabs
    derived_preview = derive(preview_project, x_rules) if slabs else []
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
            "Also add it to the project estimate (BOQ → Project estimate)",
            value=True, key="x_merge",
            help="Non-destructive: anything already in the estimate wins.")
    with g2:
        audit = st.checkbox("Extraction_Log sheet", value=True, key="x_audit")
    with g3:
        want_check = st.checkbox("Check print", value=True, key="x_check",
                                 help="The drawing with every extracted value "
                                      "boxed and quoted by grid square — for "
                                      "marking up.")

    # What the drawing did not say. Shown before the button, and never in front
    # of it: a sections sheet gives no pedestal and no slab, and refusing to
    # produce a workbook for it was how five drawings in this set produced
    # nothing at all.
    gap_project = Project(project_name="", drawing_no="")
    gap_project.pedestals, gap_project.grade_slabs = pedestals, slabs
    gap_report = GAPS.report_for(gap_project, result, derived=derived_preview,
                                 placeholder_height_m=QV.PLACEHOLDER_HEIGHT_M)
    if gap_report.needs_attention:
        with st.expander(f"⚠️ {gap_report.needs_attention} gap(s) and "
                         f"assumption(s) — the workbook is produced anyway",
                         expanded=not (pedestals or slabs)):
            st.caption(gap_report.headline())
            st.dataframe(pd.DataFrame(gap_report.as_rows()), hide_index=True,
                         use_container_width=True)
            st.caption("This table becomes the **Gaps_and_Assumptions** sheet in "
                       "the workbook, right after the Summary.")

    if not (pedestals or slabs):
        st.info("No pedestal or grade slab on this sheet. The workbook is still "
                "worth having: it carries whatever the drawing specifies on its "
                "Drawing_Items sheet, and the gaps above on their own sheet.",
                icon="ℹ️")

    if st.button("🧾 Generate BOQ Excel", type="primary", use_container_width=True,
                 key="x_generate"):
        _write(path, sheet, result, review, proj, x_rules, gap_report,
               merge_first=merge_first, audit=audit, want_check=want_check)

    wb = st.session_state.get("last_workbook")
    if not (wb and Path(wb).exists()):
        return
    wb_path = Path(wb)
    dl1, dl2 = st.columns(2)
    with dl1, open(wb_path, "rb") as fh:
        st.download_button(
            "⬇️  Download BOQ workbook (.xlsx)", data=fh.read(),
            file_name=wb_path.name, use_container_width=True, key="x_dl_wb",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption(f"Saved at `{wb_path}`")
    with dl2:
        cp = st.session_state.get("last_check_print")
        if cp and Path(cp).exists():
            with open(cp, "rb") as fh:
                st.download_button("🖨️  Download check print (.png)",
                                   data=fh.read(), file_name=Path(cp).name,
                                   mime="image/png", use_container_width=True,
                                   key="x_dl_check")
            st.caption("Print this, sit it beside the drawing, and mark up the "
                       "Verification sheet.")
    st.info("**For the check:** the workbook's **Verification** sheet lists every "
            "extracted value with its verbatim callout and drawing grid reference "
            "(e.g. D-7), with blank columns for the correct value, who checked it "
            "and when.", icon="✅")


def _write(path, sheet, result, review, proj, x_rules, gap_report, *,
           merge_first: bool, audit: bool, want_check: bool) -> None:
    tb, pedestals, slabs = review["tb"], review["pedestals"], review["slabs"]
    target = proj if merge_first else Project(project_name="", drawing_no="")

    for field_name, value in (("drawing_no", tb.drawing_no),
                              ("revision", tb.revision),
                              ("project_name", tb.project_name),
                              ("date", tb.date), ("prepared_by", tb.prepared_by)):
        if value and not getattr(target, field_name, ""):
            setattr(target, field_name, value)

    existing = {p.tag.upper() for p in target.pedestals}
    target.pedestals += [p for p in pedestals if p.tag.upper() not in existing]
    existing_s = {s.tag.upper() for s in target.grade_slabs}
    target.grade_slabs += [s for s in slabs if s.tag.upper() not in existing_s]

    if not target.pdf_source_path:
        target.pdf_source_filename = path.name
        target.pdf_source_path = str(path)

    if not target.drawing_no:
        st.error("A drawing number is required. Set it under **Project details** "
                 "above.")
        return

    derived_items = derive(target, x_rules)
    if derived_items:
        apply_derived(target, derived_items)

    target.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    bom = build_bom(target)
    plan = QV.plan_merge(Project(project_name="", drawing_no=""), result)
    bom.warnings = QV.extraction_warnings(result, plan) + bom.warnings

    out_path = build_output_path(target.drawing_no, C.OUTPUT_DIR)
    with st.spinner("Building workbook…"):
        written = write_workbook(target, bom, out_path)
        doc_g, page_g = R.open_page(path, sheet["page_idx"])
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
                    out_path=C.OUTPUT_DIR / f"{path.stem}_check.png")
                st.session_state["last_check_print"] = str(check)
        finally:
            doc_g.close()

    st.session_state["last_workbook"] = str(written)
    st.success(f"Workbook written — {len(bom.lines)} BOQ line(s)"
               + (f", including {len(derived_items)} derived" if derived_items
                  else ""))


def _keep(path: Path, result: ExtractionResult, review: dict, proj: Project) -> None:
    """The raw reading, and saving a checked one as an example for next time."""
    tb, pedestals = review["tb"], review["pedestals"]
    st.divider()
    d1, d2 = st.columns(2)
    with d1:
        st.download_button("⬇️ Extraction JSON",
                           data=json.dumps(result.model_dump(), indent=2),
                           file_name=f"{path.stem}_extraction.json",
                           mime="application/json", use_container_width=True,
                           key="x_dl_json")
    with d2:
        lib = rag_examples.library_stats()
        verifier = st.text_input(
            "Save as verified example — your name",
            value=tb.prepared_by or proj.prepared_by, key="x_verifier",
            label_visibility="collapsed",
            placeholder="Your name, to save this as a verified example")
        if st.button(f"💾 Save as verified example ({lib['distinct_drawings']}/10)",
                     disabled=not (pedestals and verifier),
                     use_container_width=True, key="x_save_example"):
            saved = rag_examples.save_example(
                drawing_no=tb.drawing_no, revision=tb.revision,
                image_hash=result.image_hash,
                verified_extraction={"pedestals": [
                    {"tag": p.tag, "length_mm": p.length_m * 1000,
                     "width_mm": p.width_m * 1000, "quantity": p.quantity}
                    for p in pedestals]},
                verified_by=verifier, source_pdf=path.name)
            st.success(f"Saved → `{saved}` — future drawings in this family "
                       f"will use it.")
