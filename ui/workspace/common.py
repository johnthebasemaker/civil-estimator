"""What every workspace tab shares: the session, the queue owner, the model's
health, Clear session, and the guard that keeps one broken tab from taking the
others down with it."""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

import streamlit as st

from core import classification as CLASS
from core import drawing_status as DS
from core import jobstore as JS
from core import library as LIB
from core.models import Project
from extractors import st_compat as SC
from extractors.ollama_client import OllamaClient
from ui import plain

log = logging.getLogger("civil_estimator.workspace")

OUTPUT_DIR = Path("output")
PROJECT_DIR = OUTPUT_DIR / "projects"
CONCRETE_GRADES = ["PCC_M15", "RCC_M25", "RCC_M30", "RCC_M40"]

# Asked per drawing, not per submission: one package routinely mixes new
# ground, work inside a live plant, and making good what is there, and those
# three are tendered at different rates.
CLASSIFICATIONS = list(CLASS.CHOICES)

# How long a queued drawing may sit untouched before "nothing is reading the
# queue" is the likelier explanation than "the worker has not polled yet". The
# worker polls every two seconds; anything past this is not a slow start.
WORKER_GRACE_S = 12.0
MODEL_OFFLINE_NOTE = "Drawings that have been read before still open instantly."

# Set by the test suite. A tab that fails is normally reported in words and the
# rest of the page carries on — which is right for a person, and exactly wrong
# for a test, where it would turn every bug into a quiet pass.
STRICT_ENV = "CIVIL_ESTIMATOR_STRICT"


# ---------------------------------------------------------------- the session
def project() -> Project:
    """The estimate this session is building. Created empty on first use."""
    if "project" not in st.session_state:
        st.session_state.project = Project(project_name="", drawing_no="")
    return st.session_state.project


def owner() -> str:
    """Whose queue this is.

    One shared password today, so everyone is the same owner. The column exists
    because the queue has to be ready for per-person accounts without a
    migration, and because a shared queue where anyone can cancel anyone's batch
    is only tolerable while the team is small.
    """
    return str(st.session_state.get("_user") or "shared")


def classification_of(pdf_path) -> str:
    """The classification a drawing is filed under.

    Read from the session first so the dropdown responds immediately, then from
    the store, which is what `bin/rebuild_set.py` sees long after the tab closes.
    """
    session = st.session_state.get(f"class::{pdf_path}")
    if session in CLASSIFICATIONS:
        return session
    return CLASS.get(pdf_path)


# ------------------------------------------------------------ the model / queue
@st.cache_data(ttl=15, show_spinner=False)
def model_health() -> tuple[bool, str]:
    """Whether the model is up. The page never calls it — the worker does — but
    it says so plainly and refuses to queue a drawing that would only fail.
    Cached briefly: an unreachable host is a round trip on every click."""
    return OllamaClient().health()


@st.cache_data(show_spinner=False, max_entries=1024)
def fingerprint(path: str, modified_ns: int, profile: str = "thorough",
                page: int = 0) -> str:
    """A drawing's content hash, once per file version rather than per click."""
    return JS.fingerprint(path, profile, page)


def worker_alive(jobs: list) -> bool:
    """Whether something is actually doing the work.

    A queue that fills up while no worker is running looks identical to a slow
    model, and the difference is one command.
    """
    now = time.time()
    return any(j.state == JS.RUNNING and now - j.heartbeat_at < 120 for j in jobs)


def no_worker_problem(what: str = "these drawings are") -> plain.Problem:
    return plain.Problem(
        f"Nothing is reading the queue right now, so {what} waiting. The "
        f"reading service needs to be started on the computer running this app.",
        fix=f"{plain.START_ALL}\n# or only the worker:\n{plain.START_WORKER}")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def badge_html(status: DS.Status) -> str:
    """A status badge. Colours come from the theme's `.ce-badge` rules."""
    return (f'<span class="ce-badge {status.tone}" title="{_attr(status.detail)}">'
            f'{status.label}</span>')


def rule_toggles(rules, *, key_prefix: str, disabled: bool = False) -> None:
    """Derivation rule checkboxes with select-all / clear.

    A button writes the checkbox keys, then reruns — the only way to drive a
    widget from a button in Streamlit.
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


# ----------------------------------------------------------- clearing / saving
# Every key the workspace owns. Listed explicitly rather than wiped wholesale,
# because session state also holds the login, and a clear that signs the user
# out is a clear nobody presses twice.
SESSION_PREFIXES = (
    "pick::",        # drawings ticked in the list
    "class::",       # per-drawing site classification
    "dwg::", "fdwg::",   # drawings ticked for the combined BOQ
    "xr_", "qr_",    # derivation toggles: one drawing / the combined BOQ
    "rule_", "param_",   # derivation on the project estimate
    "wst_",          # wastage on the project estimate
    "est_",          # the project estimate's own options
    "page::",        # the page picked in a multi-page PDF
)
# The estimate's widgets hold pending edits against the project they were drawn
# from. Loading a different project has to drop them, or the wastage boxes and
# element grids write the old values straight over the project just loaded.
ESTIMATE_PREFIXES = ("wst_", "est_", "rule_", "param_")
ESTIMATE_KEYS = ("derive_rules", "rate_editor")
SESSION_SUFFIXES = ("_editor",)      # every data editor's pending edits
SESSION_KEYS = (
    # Drawings
    "_seen_uploads", "_picks", "_confirm_delete", "open_drawing", "q_profile",
    "q_force",
    "extraction", "extraction_origin", "_last_read_key", "_reading_job",
    "x_profile", "x_rules", "ped_edit", "slab_edit", "last_workbook",
    "last_check_print", "_queued_note", "_sidebar_drawing",
    "x_merge", "x_audit", "x_check", "x_verifier",
    "evidence_table", "trace_zoom",
    # Queue
    "_confirm_clear_queue", "_ws_signature",
    # BOQ
    "q_rules", "drawing_picks", "line_picks", "sel_dwg", "sel_src",
    "sel_search", "set_boq_path", "set_boq_built", "set_boq_lines",
    "set_boq_drawings", "_confirm_clear_result", "derive_rules", "last_output",
)


def _owned(key: str) -> bool:
    return (key in SESSION_KEYS or key.startswith(SESSION_PREFIXES)
            or key.endswith(SESSION_SUFFIXES))


def clear_session(*, delete_uploads: bool = False) -> dict:
    """Put the workspace back to how it looks on a cold start.

    The saved extractions under `output/cache` are deliberately left alone. They
    are keyed by the drawing's content hash, so a cleared drawing re-queued
    later comes back in milliseconds instead of minutes of model time — and a
    button that quietly threw that away would be expensive to press by accident.
    """
    removed = {"keys": 0, "jobs": 0, "files": 0}

    for key in list(st.session_state.keys()):
        if _owned(key):
            st.session_state.pop(key, None)
            removed["keys"] += 1

    # The project carries the title-block identity and the estimate's elements,
    # which are part of the session rather than of any one drawing.
    st.session_state.project = Project(project_name="", drawing_no="")

    # The combined BOQ is built from finished queue rows, so leaving them would
    # refill it the moment the page redrew — the ghost rendering to avoid.
    removed["jobs"] = JS.clear(owner=owner())

    if delete_uploads:
        for pdf in sorted(LIB.upload_dir().glob("*.pdf")):
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


def session_bar() -> None:
    """Clear, save and load — above the tabs, because they act on all four.

    Clear session is an outlined button on purpose. The filled, coloured button
    is reserved for the next step of the work (add to the queue, generate the
    BOQ); a reset is not a step, and making it the loudest thing on the page
    invited exactly the click nobody wanted.
    """
    left, right = st.columns([3, 1.4])
    with left:
        if st.session_state.get("_confirm_clear_session"):
            st.warning(
                "Clear the whole session? Ticked drawings, extracted figures, "
                "filters, the project estimate and the built BOQ all go back to "
                "empty. The saved extractions on disk are kept, so re-reading a "
                "drawing costs nothing.", icon="🧹")
            n_uploads = len(list(LIB.upload_dir().glob("*.pdf")))
            drop = st.checkbox(
                f"Also delete the {n_uploads} uploaded PDF(s)",
                key="clear_drop_uploads",
                help="Off by default. This one cannot be undone. Drawings in "
                     "the Drawings folder are never deleted from here.")
            y, n = st.columns(2)
            if y.button("Yes, clear the session", use_container_width=True,
                        key="clear_session_yes"):
                report = clear_session(delete_uploads=drop)
                st.session_state.pop("_confirm_clear_session", None)
                st.session_state["_cleared_report"] = report
                SC.rerun()
            if n.button("Keep everything", use_container_width=True,
                        key="clear_session_no"):
                st.session_state.pop("_confirm_clear_session", None)
                SC.rerun()
        else:
            c1, c2 = st.columns([1, 3])
            if c1.button("🧹 Clear session", use_container_width=True,
                         key="clear_session"):
                st.session_state["_confirm_clear_session"] = True
                SC.rerun()
            c2.caption("Resets the drawing selection, the extracted figures, "
                       "the filters and the estimate — every tab at once.")

    with right:
        with st.popover("💾 Save or load this project", use_container_width=True):
            _save_load(project())

    report = st.session_state.pop("_cleared_report", None)
    if report:
        parts = [f"{report['keys']} setting(s)"]
        if report["jobs"]:
            parts.append(f"{report['jobs']} queue row(s)")
        if report["files"]:
            parts.append(f"{report['files']} uploaded file(s)")
        st.success("Session cleared — " + ", ".join(parts) + ".", icon="✅")


def _save_load(proj: Project) -> None:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    if st.button("💾 Save project to JSON", use_container_width=True,
                 key="save_project"):
        if not proj.drawing_no:
            st.error("Set a drawing number first — open a drawing and fill in "
                     "Project details.")
        else:
            from core.filename import sanitise
            fpath = PROJECT_DIR / (f"{sanitise(proj.drawing_no)}_"
                                   f"{datetime.now().strftime('%Y%m%d_%H%M')}.json")
            fpath.write_text(proj.model_dump_json(indent=2))
            st.success(f"Saved → `{fpath}`")
    saved = sorted(PROJECT_DIR.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not saved:
        st.caption("No saved projects yet.")
        return
    sel = st.selectbox("Load a saved project",
                       ["— select —"] + [p.name for p in saved],
                       key="load_project_pick")
    if sel != "— select —" and st.button("📂 Load", use_container_width=True,
                                         key="load_project"):
        for key in list(st.session_state.keys()):
            if (key.startswith(ESTIMATE_PREFIXES) or key in ESTIMATE_KEYS
                    or (key.endswith("_editor") and key != "line_editor")):
                st.session_state.pop(key, None)
        st.session_state.project = Project(
            **json.loads((PROJECT_DIR / sel).read_text()))
        SC.rerun()


# ------------------------------------------------------------------ the guard
def guarded(name: str, render) -> None:
    """Run one tab. If it fails, say so in words there and keep going.

    Before the workspace, one exception anywhere on the page stopped the whole
    script: a bug in pricing blanked the drawings list. `st.rerun()` and
    `st.stop()` are not `Exception`s, so they pass straight through.
    """
    try:
        render()
    except Exception:                                    # noqa: BLE001
        if os.environ.get(STRICT_ENV):
            raise
        detail = traceback.format_exc()
        log.error("The %s tab failed:\n%s", name, detail)
        plain.show(plain.Problem(
            f"Something went wrong in the {name} tab. The other tabs still "
            f"work, and nothing saved on disk is affected. Try again, and if it "
            f"keeps happening, send the technical details to whoever maintains "
            f"the app.", detail=detail), icon="⚠️")


def _attr(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))
