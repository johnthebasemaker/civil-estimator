"""Review page — read-only totals preview, wastage overrides, warnings."""
from __future__ import annotations
import streamlit as st
import pandas as pd
from core.models import Project
from core.bom_builder import build_bom

st.set_page_config(page_title="Review — Civil Estimator", layout="wide")

if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")
project: Project = st.session_state.project

st.title("🔍 Review & Wastage")
st.caption(f"Project: **{project.project_name or '(unnamed)'}** | Drawing: `{project.drawing_no or '-'}`")

# ---------- Wastage overrides ----------
st.subheader("Wastage % (editable per run)")
c1, c2, c3, c4 = st.columns(4)
with c1:
    project.wastage_concrete_pct = st.number_input("Concrete", 0.0, 25.0, project.wastage_concrete_pct, 0.5)
    project.wastage_rebar_pct = st.number_input("Rebar", 0.0, 25.0, project.wastage_rebar_pct, 0.5)
with c2:
    project.wastage_formwork_pct = st.number_input("Formwork", 0.0, 25.0, project.wastage_formwork_pct, 0.5)
    project.wastage_hdpe_pct = st.number_input("HDPE liner", 0.0, 25.0, project.wastage_hdpe_pct, 0.5)
with c3:
    project.wastage_epoxy_pct = st.number_input("Epoxy", 0.0, 25.0, project.wastage_epoxy_pct, 0.5)
    project.wastage_pcc_pct = st.number_input("PCC", 0.0, 25.0, project.wastage_pcc_pct, 0.5)
with c4:
    project.wastage_soil_pct = st.number_input("Compacted soil", 0.0, 25.0, project.wastage_soil_pct, 0.5)

st.divider()

# ---------- Build BOM live ----------
try:
    bom = build_bom(project)
except Exception as e:
    st.error(f"BOM build failed: {e}")
    st.stop()

# ---------- Warnings ----------
if bom.warnings:
    st.warning("**Warnings detected:**")
    for w in bom.warnings:
        st.markdown(f"- ⚠️ {w}")
else:
    st.success("✅ No warnings.")

st.divider()

# ---------- Element counts ----------
st.subheader("Element counts")
counts = {
    "Excavations": len(project.excavations),
    "PCC / Blinding": len(project.pcc_blindings),
    "Pedestals": len(project.pedestals),
    "Grade Slabs": len(project.grade_slabs),
    "Sumps": len(project.sumps),
    "Curb Walls": len(project.curb_walls),
    "Rebar bars (manual)": len(project.rebar_bars),
    "HDPE liners": len(project.hdpe_liners),
    "Compacted Soil": len(project.compacted_soils),
    "Epoxy Coatings": len(project.epoxy_coatings),
    "Joints": len(project.joints),
    "Embedments": len(project.embedments),
    "Waterstop runs": len(project.waterstop_runs),
    "Sump ancillaries": len(project.sump_ancillaries),
}
cnt_df = pd.DataFrame([{"Element": k, "Count": v} for k, v in counts.items()])
st.dataframe(cnt_df, hide_index=True, use_container_width=True)

st.divider()

# ---------- BOM preview ----------
st.subheader("BOM preview (first 50 lines)")
if bom.lines:
    bom_df = pd.DataFrame([{
        "Category": l.category, "Item": l.item, "Unit": l.unit,
        "Qty (net)": l.qty_net, "Wastage %": l.wastage_pct,
        "Qty (gross)": l.qty_gross, "Source": l.source_tag,
    } for l in bom.lines[:50]])
    st.dataframe(bom_df, hide_index=True, use_container_width=True)
    if len(bom.lines) > 50:
        st.caption(f"…{len(bom.lines) - 50} more lines. Full list in the exported Excel.")
else:
    st.info("No elements entered yet. Go to **1_Input** to start.")

st.divider()

# ---------- Rollup totals ----------
rollups = [l for l in bom.lines if l.category == "ROLLUP"]
if rollups:
    st.subheader("Rollup totals")
    ru_df = pd.DataFrame([{"Item": l.item, "Qty": l.qty_gross, "Unit": l.unit} for l in rollups])
    st.dataframe(ru_df, hide_index=True, use_container_width=True)

st.info("Proceed to **3_BOM** (Step 6) to download the Excel workbook.")