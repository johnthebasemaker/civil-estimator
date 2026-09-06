"""Civil Estimator — home page.

Handles project metadata, PDF drawing attachment, and project persistence
(save/load JSON in output/projects/).
"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime
import streamlit as st
from core.models import Project


UPLOAD_DIR = Path("output/uploads")
PROJECT_DIR = Path("output/projects")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_DIR.mkdir(parents=True, exist_ok=True)


st.set_page_config(page_title="Civil Estimator", layout="wide", page_icon="🏗️")

# ---------- Session state bootstrap ----------
if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")

project: Project = st.session_state.project

st.title("🏗️ Civil Estimator")
st.caption("Structured input → BOQ Excel workbook. Phase 1 (manual input, PDF attached for reference).")

# ---------- Project metadata ----------
st.header("Project Details")
col1, col2, col3 = st.columns(3)
with col1:
    project.project_name = st.text_input("Project name", value=project.project_name,
                                         placeholder="e.g. Maaden Phosphate 3 — Phase 1")
    project.prepared_by = st.text_input("Prepared by", value=project.prepared_by,
                                        placeholder="e.g. Johnson Andrew")
with col2:
    project.drawing_no = st.text_input("Drawing No.", value=project.drawing_no,
                                       placeholder="e.g. MD-522-8110-EG-CV-LAD-0107")
    project.revision = st.text_input("Revision", value=project.revision,
                                     placeholder="e.g. C01")
with col3:
    project.date = st.text_input("Drawing date", value=project.date,
                                 placeholder="YYYY-MM-DD")

st.divider()

# ---------- PDF attachment ----------
st.header("Attach Drawing PDF (reference only)")
st.caption("The PDF is stored alongside the workbook for reference. This phase does not auto-extract data.")

uploaded = st.file_uploader("Upload drawing PDF", type=["pdf"],
                             help="File will be saved to output/uploads/")
if uploaded is not None:
    save_path = UPLOAD_DIR / uploaded.name
    save_path.write_bytes(uploaded.getvalue())
    project.pdf_source_filename = uploaded.name
    project.pdf_source_path = str(save_path)
    st.success(f"✅ Attached: {uploaded.name} ({uploaded.size / 1024:.1f} KB)")

if project.pdf_source_filename:
    st.info(f"📎 Currently attached: **{project.pdf_source_filename}**")

st.divider()

# ---------- Project save/load ----------
st.header("Save / Load Project")
col_save, col_load = st.columns(2)

with col_save:
    if st.button("💾 Save project to JSON", use_container_width=True):
        if not project.drawing_no:
            st.error("Set Drawing No. before saving.")
        else:
            from core.filename import sanitise
            fname = f"{sanitise(project.drawing_no)}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
            fpath = PROJECT_DIR / fname
            fpath.write_text(project.model_dump_json(indent=2))
            st.success(f"Saved → `{fpath}`")

with col_load:
    existing = sorted(PROJECT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if existing:
        options = [p.name for p in existing]
        sel = st.selectbox("Load existing project", ["-- select --"] + options)
        if sel != "-- select --":
            if st.button("📂 Load selected", use_container_width=True):
                data = json.loads((PROJECT_DIR / sel).read_text())
                st.session_state.project = Project(**data)
                st.rerun()
    else:
        st.caption("No saved projects yet.")

st.divider()

# ---------- Next step nav hint ----------
st.header("Next steps")
st.markdown("""
1. **Enter project details above** ← you are here
2. Go to **1_Input** in the sidebar to enter elements (pedestals, slab, sump, joints, etc.)
3. Go to **2_Review** to verify totals and warnings
4. Go to **3_BOM** *(Step 6)* to preview and download the Excel workbook
5. Go to **4_Costing** *(Step 6)* to enter unit rates and view totals
""")

# ---------- Live sidebar summary ----------
with st.sidebar:
    st.subheader("Current project")
    st.write(f"**{project.project_name or '(no name)'}**")
    st.write(f"Drawing: `{project.drawing_no or '-'}` Rev `{project.revision or '-'}`")
    st.caption(f"Elements entered:")
    st.write(f"- Pedestals: {len(project.pedestals)}")
    st.write(f"- Grade slabs: {len(project.grade_slabs)}")
    st.write(f"- Sumps: {len(project.sumps)}")
    st.write(f"- Joints: {len(project.joints)}")
    st.write(f"- Excavations: {len(project.excavations)}")
    st.write(f"- Rebar bars (manual): {len(project.rebar_bars)}")