"""Input page — tabbed data entry for all 12 BOM element types.

Uses st.data_editor for each element type so rows can be added, edited, or
deleted in a grid. Data flows to st.session_state.project on each rerun.
"""
from __future__ import annotations
import streamlit as st
import pandas as pd
from core.models import (
    Project, Excavation, PCCBlinding, Pedestal, GradeSlab, Sump, CurbWall,
    FormworkLoose, RebarBar, HDPELiner, CompactedSoil, EpoxyCoating,
    Joint, Embedment, WaterstopRun, SumpAncillary,
)

st.set_page_config(page_title="Input — Civil Estimator", layout="wide")

if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")
project: Project = st.session_state.project

st.title("📝 Element Input")
st.caption(f"Project: **{project.project_name or '(unnamed)'}** | Drawing: `{project.drawing_no or '-'}`")

CONCRETE_GRADES = ["PCC_M15", "RCC_M25", "RCC_M30", "RCC_M40"]
SOIL_TYPES = ["ordinary", "hard_murrum", "soft_rock", "hard_rock"]
JOINT_TYPES = ["expansion", "contraction", "construction"]
EMBEDMENT_TYPES = ["insert_plate", "anchor_bolt", "dowel", "sleeve"]
WATERSTOP_MATERIALS = ["PVC", "hydrophilic", "bentonite"]
SUMP_ANCILLARY_TYPES = ["grating", "drain_pipe", "cover"]
REBAR_DIAMETERS = [6, 8, 10, 12, 16, 20, 25, 28, 32, 40]


# ---------- Helper: generic data_editor block ----------
def _list_to_df(items: list, columns: list[str]) -> pd.DataFrame:
    if not items:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame([{c: getattr(i, c) for c in columns} for i in items])


def _df_to_list(df: pd.DataFrame, model_cls) -> list:
    if df is None or df.empty:
        return []
    records = df.dropna(how="all").to_dict("records")
    out = []
    for rec in records:
        # Skip rows with no tag (user added blank row and left it)
        if not rec.get("tag"):
            continue
        try:
            out.append(model_cls(**rec))
        except Exception as e:
            st.warning(f"Skipped invalid row in {model_cls.__name__}: {e}")
    return out


# ---------- Tabs ----------
tabs = st.tabs([
    "🕳 Excavation", "🧱 PCC", "🏛 Pedestals", "🟫 Grade Slab",
    "🕳 Sump", "🚧 Curb Wall", "📐 Rebar (manual)", "🎽 HDPE Liner",
    "🌱 Compacted Soil", "🎨 Epoxy", "↔️ Joints", "🔩 Embedments",
    "💧 Waterstop", "🌀 Sump Ancillaries",
])


# ---------- 1. Excavation ----------
with tabs[0]:
    st.subheader("Excavation")
    cols = ["tag", "length_m", "width_m", "depth_m", "quantity", "soil_type"]
    df = _list_to_df(project.excavations, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "length_m": st.column_config.NumberColumn("Length (m)", min_value=0.001, step=0.1),
            "width_m": st.column_config.NumberColumn("Width (m)", min_value=0.001, step=0.1),
            "depth_m": st.column_config.NumberColumn("Depth (m)", min_value=0.001, step=0.1),
            "quantity": st.column_config.NumberColumn("Nos", min_value=1, step=1),
            "soil_type": st.column_config.SelectboxColumn("Soil", options=SOIL_TYPES),
        }, key="exc_editor")
    project.excavations = _df_to_list(edited, Excavation)


# ---------- 2. PCC ----------
with tabs[1]:
    st.subheader("PCC / Blinding")
    cols = ["tag", "length_m", "width_m", "thickness_m", "grade", "quantity"]
    df = _list_to_df(project.pcc_blindings, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "length_m": st.column_config.NumberColumn("L (m)", min_value=0.001, step=0.1),
            "width_m": st.column_config.NumberColumn("W (m)", min_value=0.001, step=0.1),
            "thickness_m": st.column_config.NumberColumn("Thk (m)", min_value=0.05, step=0.05),
            "grade": st.column_config.SelectboxColumn("Grade", options=CONCRETE_GRADES),
            "quantity": st.column_config.NumberColumn("Nos", min_value=1, step=1),
        }, key="pcc_editor")
    project.pcc_blindings = _df_to_list(edited, PCCBlinding)


# ---------- 3. Pedestals ----------
with tabs[2]:
    st.subheader("Pedestals")
    st.caption("Rebar coefficient default: 120 kg/m³ for pedestals. Override per row if needed.")
    cols = ["tag", "length_m", "width_m", "height_m", "quantity", "grade", "rebar_coefficient_kg_per_m3"]
    df = _list_to_df(project.pedestals, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True, help="e.g. P1, P2 … P7"),
            "length_m": st.column_config.NumberColumn("L (m)", min_value=0.001, step=0.05),
            "width_m": st.column_config.NumberColumn("W (m)", min_value=0.001, step=0.05),
            "height_m": st.column_config.NumberColumn("H (m)", min_value=0.001, step=0.05),
            "quantity": st.column_config.NumberColumn("Nos", min_value=1, step=1),
            "grade": st.column_config.SelectboxColumn("Grade", options=CONCRETE_GRADES),
            "rebar_coefficient_kg_per_m3": st.column_config.NumberColumn("Rebar coeff (kg/m³)", min_value=0.0, step=10.0),
        }, key="ped_editor")
    project.pedestals = _df_to_list(edited, Pedestal)


# ---------- 4. Grade Slab ----------
with tabs[3]:
    st.subheader("Grade Slab")
    cols = ["tag", "length_m", "width_m", "thickness_m", "grade",
            "rebar_coefficient_kg_per_m3", "has_top_formwork"]
    df = _list_to_df(project.grade_slabs, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "length_m": st.column_config.NumberColumn("L (m)", min_value=0.001, step=0.5),
            "width_m": st.column_config.NumberColumn("W (m)", min_value=0.001, step=0.5),
            "thickness_m": st.column_config.NumberColumn("Thk (m)", min_value=0.05, step=0.05),
            "grade": st.column_config.SelectboxColumn("Grade", options=CONCRETE_GRADES),
            "rebar_coefficient_kg_per_m3": st.column_config.NumberColumn("Rebar coeff (kg/m³)", min_value=0.0, step=10.0),
            "has_top_formwork": st.column_config.CheckboxColumn("Top formwork?"),
        }, key="slab_editor")
    project.grade_slabs = _df_to_list(edited, GradeSlab)


# ---------- 5. Sump ----------
with tabs[4]:
    st.subheader("Sump / Pit")
    cols = ["tag", "outer_length_m", "outer_width_m", "depth_m",
            "wall_thickness_m", "base_thickness_m", "grade", "rebar_coefficient_kg_per_m3"]
    df = _list_to_df(project.sumps, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "outer_length_m": st.column_config.NumberColumn("Outer L (m)", min_value=0.1, step=0.1),
            "outer_width_m": st.column_config.NumberColumn("Outer W (m)", min_value=0.1, step=0.1),
            "depth_m": st.column_config.NumberColumn("Depth (m)", min_value=0.1, step=0.1),
            "wall_thickness_m": st.column_config.NumberColumn("Wall Thk (m)", min_value=0.05, step=0.05),
            "base_thickness_m": st.column_config.NumberColumn("Base Thk (m)", min_value=0.05, step=0.05),
            "grade": st.column_config.SelectboxColumn("Grade", options=CONCRETE_GRADES),
            "rebar_coefficient_kg_per_m3": st.column_config.NumberColumn("Rebar coeff (kg/m³)", min_value=0.0, step=10.0),
        }, key="sump_editor")
    project.sumps = _df_to_list(edited, Sump)


# ---------- 6. Curb Wall ----------
with tabs[5]:
    st.subheader("Curb Wall / Dyke Wall")
    cols = ["tag", "length_m", "height_m", "thickness_m", "grade", "rebar_coefficient_kg_per_m3"]
    df = _list_to_df(project.curb_walls, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "length_m": st.column_config.NumberColumn("L (m)", min_value=0.1, step=0.5),
            "height_m": st.column_config.NumberColumn("H (m)", min_value=0.05, step=0.05),
            "thickness_m": st.column_config.NumberColumn("Thk (m)", min_value=0.05, step=0.05),
            "grade": st.column_config.SelectboxColumn("Grade", options=CONCRETE_GRADES),
            "rebar_coefficient_kg_per_m3": st.column_config.NumberColumn("Rebar coeff (kg/m³)", min_value=0.0, step=10.0),
        }, key="curb_editor")
    project.curb_walls = _df_to_list(edited, CurbWall)


# ---------- 7. Rebar (manual BBS) ----------
with tabs[6]:
    st.subheader("Rebar — Manual BBS entries")
    st.caption("Optional. If both a coefficient AND manual bars exist for the same parent, "
               "you'll see a warning on the Review page.")
    cols = ["tag", "parent_element", "diameter_mm", "cut_length_m",
            "nos_per_element", "parent_quantity"]
    df = _list_to_df(project.rebar_bars, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Bar Mark", required=True),
            "parent_element": st.column_config.TextColumn("Parent Tag", required=True),
            "diameter_mm": st.column_config.SelectboxColumn("Dia (mm)", options=REBAR_DIAMETERS),
            "cut_length_m": st.column_config.NumberColumn("Cut Len (m)", min_value=0.01, step=0.1),
            "nos_per_element": st.column_config.NumberColumn("Nos/elem", min_value=1, step=1),
            "parent_quantity": st.column_config.NumberColumn("Nos of parent", min_value=1, step=1),
        }, key="rebar_editor")
    project.rebar_bars = _df_to_list(edited, RebarBar)


# ---------- 8. HDPE Liner ----------
with tabs[7]:
    st.subheader("HDPE Liner")
    cols = ["tag", "length_m", "width_m", "thickness_mm"]
    df = _list_to_df(project.hdpe_liners, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "length_m": st.column_config.NumberColumn("L (m)", min_value=0.1, step=0.5),
            "width_m": st.column_config.NumberColumn("W (m)", min_value=0.1, step=0.5),
            "thickness_mm": st.column_config.NumberColumn("Thk (mm)", min_value=0.5, step=0.5),
        }, key="hdpe_editor")
    project.hdpe_liners = _df_to_list(edited, HDPELiner)


# ---------- 9. Compacted Soil ----------
with tabs[8]:
    st.subheader("Compacted Soil / Fill")
    cols = ["tag", "volume_m3", "description"]
    df = _list_to_df(project.compacted_soils, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "volume_m3": st.column_config.NumberColumn("Volume (m³)", min_value=0.01, step=1.0),
            "description": st.column_config.TextColumn("Description"),
        }, key="soil_editor")
    project.compacted_soils = _df_to_list(edited, CompactedSoil)


# ---------- 10. Epoxy Coating ----------
with tabs[9]:
    st.subheader("Epoxy / Acid-Resistant Coating")
    cols = ["tag", "area_m2", "thickness_mm", "coats"]
    df = _list_to_df(project.epoxy_coatings, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "area_m2": st.column_config.NumberColumn("Area (m²)", min_value=0.1, step=1.0),
            "thickness_mm": st.column_config.NumberColumn("Thk (mm)", min_value=0.5, step=0.5),
            "coats": st.column_config.NumberColumn("Coats", min_value=1, step=1),
        }, key="epoxy_editor")
    project.epoxy_coatings = _df_to_list(edited, EpoxyCoating)


# ---------- 11. Joints ----------
with tabs[10]:
    st.subheader("Joints")
    cols = ["tag", "joint_type", "length_m", "has_waterstop", "has_sealant", "has_backer_rod"]
    df = _list_to_df(project.joints, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "joint_type": st.column_config.SelectboxColumn("Type", options=JOINT_TYPES),
            "length_m": st.column_config.NumberColumn("Length (m)", min_value=0.01, step=0.5),
            "has_waterstop": st.column_config.CheckboxColumn("Waterstop?"),
            "has_sealant": st.column_config.CheckboxColumn("Sealant?"),
            "has_backer_rod": st.column_config.CheckboxColumn("Backer rod?"),
        }, key="joint_editor")
    project.joints = _df_to_list(edited, Joint)


# ---------- 12. Embedments ----------
with tabs[11]:
    st.subheader("Embedments (insert plates, anchor bolts, dowels, sleeves)")
    cols = ["tag", "embedment_type", "quantity", "size_description", "unit_weight_kg"]
    df = _list_to_df(project.embedments, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "embedment_type": st.column_config.SelectboxColumn("Type", options=EMBEDMENT_TYPES),
            "quantity": st.column_config.NumberColumn("Nos", min_value=1, step=1),
            "size_description": st.column_config.TextColumn("Size", required=True),
            "unit_weight_kg": st.column_config.NumberColumn("Unit wt (kg) — optional", min_value=0.0, step=0.1),
        }, key="embed_editor")
    project.embedments = _df_to_list(edited, Embedment)


# ---------- 13. Waterstop ----------
with tabs[12]:
    st.subheader("Waterstop (standalone runs, not tied to joints)")
    cols = ["tag", "length_m", "material", "width_mm"]
    df = _list_to_df(project.waterstop_runs, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "length_m": st.column_config.NumberColumn("Length (m)", min_value=0.1, step=0.5),
            "material": st.column_config.SelectboxColumn("Material", options=WATERSTOP_MATERIALS),
            "width_mm": st.column_config.NumberColumn("Width (mm)", min_value=50.0, step=25.0),
        }, key="ws_editor")
    project.waterstop_runs = _df_to_list(edited, WaterstopRun)


# ---------- 14. Sump Ancillaries ----------
with tabs[13]:
    st.subheader("Sump Ancillaries (gratings, drain pipes, covers)")
    cols = ["tag", "item_type", "quantity", "size_description", "length_m"]
    df = _list_to_df(project.sump_ancillaries, cols)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
        column_config={
            "tag": st.column_config.TextColumn("Tag", required=True),
            "item_type": st.column_config.SelectboxColumn("Type", options=SUMP_ANCILLARY_TYPES),
            "quantity": st.column_config.NumberColumn("Nos", min_value=1, step=1),
            "size_description": st.column_config.TextColumn("Size"),
            "length_m": st.column_config.NumberColumn("Length (m) — for pipes only", min_value=0.0, step=0.5),
        }, key="anc_editor")
    project.sump_ancillaries = _df_to_list(edited, SumpAncillary)

st.divider()
st.info("💡 Data auto-saves to session on every edit. Use **Save project to JSON** on the home page for permanent storage.")