"""BOM page — final preview and download."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import streamlit as st
import pandas as pd
from core.models import Project
from core.bom_builder import build_bom
from core.excel_writer import write_workbook
from core.filename import build_output_path

st.set_page_config(page_title="BOM — Civil Estimator", layout="wide")

if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")
project: Project = st.session_state.project

st.title("📊 BOM Preview & Download")
st.caption(f"Project: **{project.project_name or '(unnamed)'}** | "
           f"Drawing: `{project.drawing_no or '-'}` Rev `{project.revision or '-'}`")

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

if st.button("🧾 Generate Excel BOQ", type="primary", use_container_width=True):
    project.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    with st.spinner("Building workbook…"):
        written = write_workbook(project, bom, target)
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