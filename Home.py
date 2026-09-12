"""Civil Estimator — home.

Project identity, the drawing it comes from, and an honest view of how complete
the estimate is. Everything else happens on the pages in the sidebar.
"""
from __future__ import annotations

# Repair sys.path before anything heavy is imported: `streamlit run Home.py`
# uses the framework Python, which has no PyMuPDF. See sitepath.py.
import sitepath  # noqa: F401  (import first — it fixes the import path)

import json
from datetime import datetime
from pathlib import Path

import streamlit as st

from core.models import Project
from ui import kit

UPLOAD_DIR = Path("output/uploads")
PROJECT_DIR = Path("output/projects")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Civil Estimator", layout="wide", page_icon=kit.favicon())

# Each page is its own script, so each carries the gate: a login on the
# home page alone would be bypassed by navigating straight to a page URL.
kit.require_login()

if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")
project: Project = st.session_state.project

kit.page_header("Project setup", project,
                "Drawing → takeoff → BOQ workbook. Everything runs on this "
                "machine; no cloud services.", step="Project")

# ---------- Where to go ----------
cov = kit.assess(project)
if kit.element_count(project) == 0:
    st.info("**Start with a drawing.** Open **Drawing → BOQ** in the sidebar to "
            "upload a PDF and let the local model read it, or go to **Input** to "
            "enter elements by hand.", icon="👉")
elif cov.blockers:
    st.warning("The estimate has items that need attention before pricing — "
               "see below.", icon="⚠️")
else:
    st.success("Ready to price. **BOM** generates the workbook; **Costing** "
               "applies rates.", icon="✅")

st.subheader("Estimate status")
kit.readiness_panel(project)

st.divider()

# ---------- Identity ----------
st.subheader("Project details")
st.caption("These print on the title block of every sheet in the workbook. "
           "Extraction fills them in automatically if you use a drawing.")
c1, c2, c3 = st.columns(3)
with c1:
    project.project_name = st.text_input(
        "Project name", value=project.project_name,
        placeholder="e.g. Maaden Phosphate 3 — Phase 1")
    project.prepared_by = st.text_input(
        "Prepared by", value=project.prepared_by, placeholder="e.g. Johnson Andrew")
with c2:
    project.drawing_no = st.text_input(
        "Drawing No.", value=project.drawing_no,
        placeholder="e.g. MD-522-8110-EG-CV-LAD-0107",
        help="Required — the BOM page will not run without it.")
    project.revision = st.text_input("Revision", value=project.revision,
                                     placeholder="e.g. C01")
with c3:
    project.date = st.text_input("Drawing date", value=project.date,
                                 placeholder="YYYY-MM-DD")
    if project.pdf_source_filename:
        st.caption(f"📎 Drawing attached: **{project.pdf_source_filename}**")

st.divider()

# ---------- Drawing ----------
st.subheader("Drawing")
d1, d2 = st.columns([2, 1])
with d1:
    uploaded = st.file_uploader("Attach a drawing PDF", type=["pdf"],
                                help="Stored in output/uploads/ and used by "
                                     "Drawing → BOQ.")
    if uploaded is not None:
        save_path = UPLOAD_DIR / uploaded.name
        save_path.write_bytes(uploaded.getvalue())
        project.pdf_source_filename = uploaded.name
        project.pdf_source_path = str(save_path)
        st.success(f"Attached {uploaded.name} ({uploaded.size / 1024:.0f} KB). "
                   f"Open **Drawing → BOQ** to extract it.")
with d2:
    st.markdown("**Extraction reads**")
    st.markdown("- Title block — drawing no, rev, date\n"
                "- Pedestal callouts — size and quantity\n"
                "- Grade slab extent and thickness")
    st.caption("Pedestal **heights** are not printed on these drawings and are "
               "never guessed — you enter them.")

st.divider()

# ---------- Save / load ----------
st.subheader("Save / load")
s1, s2 = st.columns(2)
with s1:
    if st.button("💾 Save project to JSON", use_container_width=True):
        if not project.drawing_no:
            st.error("Set a Drawing No. before saving.")
        else:
            from core.filename import sanitise
            fname = (f"{sanitise(project.drawing_no)}_"
                     f"{datetime.now().strftime('%Y%m%d_%H%M')}.json")
            fpath = PROJECT_DIR / fname
            fpath.write_text(project.model_dump_json(indent=2))
            st.success(f"Saved → `{fpath}`")
with s2:
    existing = sorted(PROJECT_DIR.glob("*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if existing:
        sel = st.selectbox("Load a saved project",
                           ["— select —"] + [p.name for p in existing])
        if sel != "— select —" and st.button("📂 Load", use_container_width=True):
            data = json.loads((PROJECT_DIR / sel).read_text())
            st.session_state.project = Project(**data)
            st.rerun()
    else:
        st.caption("No saved projects yet.")

kit.sidebar_summary(project)
kit.sidebar_account()
