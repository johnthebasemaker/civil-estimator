"""BOM page — final preview and download."""
from __future__ import annotations

# Repair sys.path before anything heavy is imported: `streamlit run Home.py`
# uses the framework Python, which has no PyMuPDF. See sitepath.py.
import sitepath  # noqa: F401  (import first — it fixes the import path)
from datetime import datetime
from pathlib import Path
import streamlit as st
import pandas as pd
from core.models import Project
from core.bom_builder import build_bom
from core.excel_writer import write_workbook
from core.filename import build_output_path
from ui import kit

st.set_page_config(page_title="BOM — Civil Estimator", layout="wide",
                   page_icon=kit.favicon())

# Each page is its own script, so each carries the gate: a login on the
# home page alone would be bypassed by navigating straight to a page URL.
kit.require_login()

if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")
project: Project = st.session_state.project

kit.page_header("BOM Preview & Download", project,
                "The full bill of quantities, and the workbook.", step="BOM")
_cov = kit.readiness_panel(project, compact=True)
st.divider()

# ---------- Guardrails ----------
if not project.drawing_no:
    st.error("Set **Drawing No.** on the home page before generating a BOM.")
    st.stop()

total_elements = (
    len(project.pedestals) + len(project.grade_slabs) + len(project.sumps) +
    len(project.curb_walls) + len(project.excavations) + len(project.pcc_blindings) +
    len(project.epoxy_coatings) + len(project.joints) + len(project.embedments) +
    len(project.hdpe_liners) + len(project.compacted_soils) +
    len(project.waterstop_runs) + len(project.sump_ancillaries) +
    len(project.rebar_bars) + len(project.formwork_loose)
)
if total_elements == 0:
    st.warning("No elements entered. Go to **1_Input** to add at least one element.")
    st.stop()

# ---------- Build BOM ----------
try:
    bom = build_bom(project)
except Exception as e:
    st.error(f"BOM build failed: {e}")
    st.stop()

# ---------- Warnings ----------
if bom.warnings:
    with st.expander("⚠️ Warnings", expanded=True):
        for w in bom.warnings:
            st.markdown(f"- {w}")

# ---------- KPIs ----------
st.subheader("Snapshot")
total_concrete = sum(l.qty_gross for l in bom.lines
                     if l.category in {"Structural Concrete", "PCC / Blinding"}
                     and l.category != "ROLLUP")
total_rebar = sum(l.qty_gross for l in bom.lines
                  if l.category.startswith("Rebar") and l.category != "ROLLUP")
total_formwork = sum(l.qty_gross for l in bom.lines if l.category == "Formwork")
total_excavation = sum(l.qty_gross for l in bom.lines
                       if l.category == "Earthwork" and "Excavation" in l.item)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Concrete", f"{total_concrete:,.2f} m³")
c2.metric("Total Rebar", f"{total_rebar:,.0f} kg")
c3.metric("Total Formwork", f"{total_formwork:,.2f} m²")
c4.metric("Total Excavation", f"{total_excavation:,.2f} m³")

st.divider()

# ---------- Full BOM table ----------
st.subheader("Full BOM")
bom_df = pd.DataFrame([{
    "#": i + 1, "Category": l.category, "Item": l.item, "Unit": l.unit,
    "Qty (net)": round(l.qty_net, 3), "Wastage %": l.wastage_pct,
    "Qty (gross)": round(l.qty_gross, 3), "Source": l.source_tag, "Notes": l.notes,
} for i, l in enumerate(bom.lines)])
st.dataframe(bom_df, hide_index=True, use_container_width=True, height=500)

st.divider()

# ---------- Rebar BBS ----------
if bom.rebar_bbs:
    st.subheader("Rebar Bar Bending Schedule")
    bbs_df = pd.DataFrame([{
        "Parent": r.parent_element, "Bar Mark": r.bar_mark,
        "Dia (mm)": r.diameter_mm, "Cut Length (m)": r.cut_length_m,
        "Nos": r.nos, "Total Length (m)": r.total_length_m,
        "Weight (kg)": r.weight_kg,
    } for r in bom.rebar_bbs])
    st.dataframe(bbs_df, hide_index=True, use_container_width=True)

st.divider()

# ---------- Export ----------
st.subheader("Download Excel Workbook")

target = build_output_path(project.drawing_no, "output")
st.caption(f"Output filename: `{target.name}`")

add_verify = st.checkbox(
    "Add a Verification sheet (sign-off columns + drawing grid references)",
    value=bool(st.session_state.get("extraction")),
    help="Only meaningful when the quantities came from an extraction — it "
         "lists each read value with its verbatim callout so a checker can "
         "confirm it against the sheet.")

if st.button("🧾 Generate Excel BOQ", type="primary", use_container_width=True):
    project.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    with st.spinner("Building workbook…"):
        written = write_workbook(project, bom, target)
        _extraction = st.session_state.get("extraction")
        if add_verify and _extraction is not None:
            from extractors import workbook_extras as _WE
            _WE.append_verification_sheet(written, _extraction)
    st.success(f"✅ Workbook written: `{written}`")
    with open(written, "rb") as f:
        st.download_button(
            "⬇️ Download BOQ (.xlsx)",
            data=f.read(),
            file_name=written.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    st.session_state.last_output = str(written)

if st.session_state.get("last_output"):
    st.caption(f"Last generated: `{Path(st.session_state.last_output).name}`")

kit.sidebar_summary(project)
kit.sidebar_account()
