"""Element input — grouped by how a takeoff is actually built up.

Fourteen element types is a lot of tabs to stare at. They are grouped here the
way an estimator works down a foundation: dig it, blind it, pour it, reinforce
it, finish it. Sections with data open on load; empty ones stay out of the way.

The derivation panel at the top does the arithmetic an estimator would do by
hand from the slab geometry — excavation, blinding, liner, coating, curb wall,
joints. Every rule shows its formula and its parameters, and none is applied
until you say so.
"""
from __future__ import annotations

# Repair sys.path before anything heavy is imported: `streamlit run Home.py`
# uses the framework Python, which has no PyMuPDF. See sitepath.py.
import sitepath  # noqa: F401  (import first — it fixes the import path)

import pandas as pd
import streamlit as st

from core.derivation import apply_derived, default_rules, derive, rules_from_findings
from core.models import (
    CompactedSoil, CurbWall, Embedment, EpoxyCoating, Excavation, FormworkLoose,
    GradeSlab, HDPELiner, Joint, PCCBlinding, Pedestal, Project, RebarBar, Sump,
    SumpAncillary, WaterstopRun,
)
from ui import kit

st.set_page_config(page_title="Input — Civil Estimator", layout="wide", page_icon=kit.favicon())

# Each page is its own script, so each carries the gate: a login on the
# home page alone would be bypassed by navigating straight to a page URL.
kit.require_login()

if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")
project: Project = st.session_state.project

kit.page_header("Element Input", project,
                "Quantities that go into the BOQ. Extraction fills some of "
                "these in; the rest is yours.", step="Input")

CONCRETE_GRADES = ["PCC_M15", "RCC_M25", "RCC_M30", "RCC_M40"]
SOIL_TYPES = ["ordinary", "hard_murrum", "soft_rock", "hard_rock"]
JOINT_TYPES = ["expansion", "contraction", "construction"]
EMBEDMENT_TYPES = ["insert_plate", "anchor_bolt", "dowel", "sleeve"]
WATERSTOP_MATERIALS = ["PVC", "hydrophilic", "bentonite"]
SUMP_ANCILLARY_TYPES = ["grating", "drain_pipe", "cover"]
REBAR_DIAMETERS = [6, 8, 10, 12, 16, 20, 25, 28, 32, 40]

N = st.column_config.NumberColumn
T = st.column_config.TextColumn
S = st.column_config.SelectboxColumn
C = st.column_config.CheckboxColumn


# ---------- Shared editor ----------
def _to_df(items: list, columns: list[str]) -> pd.DataFrame:
    if not items:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame([{c: getattr(i, c) for c in columns} for i in items])


def _to_list(df: pd.DataFrame, model_cls) -> list:
    if df is None or df.empty:
        return []
    out = []
    for rec in df.dropna(how="all").to_dict("records"):
        if not str(rec.get("tag") or "").strip():
            continue
        try:
            out.append(model_cls(**rec))
        except Exception as exc:                # noqa: BLE001 — surfaced to user
            st.warning(f"Skipped a {model_cls.__name__} row: {exc}")
    return out


def editor(field: str, label: str, model_cls, config: dict, *,
           caption: str = "", critical: bool = False) -> None:
    """One element type, in a section that opens only when it matters."""
    items = getattr(project, field)
    n = len(items)
    badge = f" · {n}" if n else ""
    with st.expander(f"{'⛔ ' if critical and not n else ''}{label}{badge}",
                     expanded=bool(n) or critical):
        if caption:
            st.caption(caption)
        cols = list(config)
        edited = st.data_editor(_to_df(items, cols), num_rows="dynamic",
                                use_container_width=True,
                                column_config=config, key=f"{field}_editor")
        setattr(project, field, _to_list(edited, model_cls))


# ---------- Derivation ----------
st.subheader("Derive quantities from the slab")
if not project.grade_slabs:
    st.caption("Add a grade slab (or extract one from a drawing) and these "
               "rules become available.")
else:
    if "derive_rules" not in st.session_state:
        findings = getattr(st.session_state.get("extraction"), "findings", None)
        st.session_state.derive_rules = rules_from_findings(default_rules(), findings)
    rules = st.session_state.derive_rules

    st.caption("These are **not read from the drawing** — they follow from the "
               "slab geometry plus site convention. Check each formula, adjust "
               "the inputs, then apply. Nothing is added until you press the "
               "button.")
    with st.expander(f"Derivation rules · {sum(r.enabled for r in rules)} selected",
                     expanded=False):
        for r in rules:
            c1, c2 = st.columns([3, 4])
            with c1:
                r.enabled = st.checkbox(r.label, value=r.enabled, key=f"rule_{r.key}")
                st.caption(r.formula)
            with c2:
                pcols = st.columns(max(len(r.params), 1))
                for (pname, pval), pc in zip(list(r.params.items()), pcols):
                    with pc:
                        r.params[pname] = st.number_input(
                            pname.replace("_", " "), value=float(pval),
                            step=0.05 if pname.endswith("_m") else 0.5,
                            format="%.3f", key=f"param_{r.key}_{pname}",
                            disabled=not r.enabled)
            st.divider()

        preview = derive(project, rules)
        if preview:
            st.markdown("**Would add**")
            st.dataframe(pd.DataFrame([{
                "Element": i.target_field, "Tag": getattr(i.element, "tag", ""),
                "How it was worked out": i.explanation} for i in preview]),
                hide_index=True, use_container_width=True)
            if st.button(f"➕ Apply {len(preview)} derived quantity(ies)",
                         type="primary", use_container_width=True):
                added = apply_derived(project, preview)
                st.success(f"Added {len(added)} element(s). They are ordinary "
                           f"rows now — edit or delete them below.")
                st.rerun()
        else:
            st.caption("Tick a rule to see what it would add.")

st.divider()

# ---------- Grouped element entry ----------
tabs = st.tabs(["🕳 Earthworks & sub-base", "🧱 Concrete",
                "📐 Reinforcement & formwork", "🎨 Finishes & ancillaries"])

with tabs[0]:
    editor("excavations", "Excavation", Excavation, {
        "tag": T("Tag", required=True),
        "length_m": N("L (m)", min_value=0.001, step=0.1),
        "width_m": N("W (m)", min_value=0.001, step=0.1),
        "depth_m": N("Depth (m)", min_value=0.001, step=0.1),
        "quantity": N("Nos", min_value=1, step=1),
        "soil_type": S("Soil", options=SOIL_TYPES),
    }, critical=True)
    editor("compacted_soils", "Compacted soil / fill", CompactedSoil, {
        "tag": T("Tag", required=True),
        "volume_m3": N("Volume (m³)", min_value=0.01, step=1.0),
        "description": T("Description"),
    })
    editor("pcc_blindings", "PCC / blinding", PCCBlinding, {
        "tag": T("Tag", required=True),
        "length_m": N("L (m)", min_value=0.001, step=0.1),
        "width_m": N("W (m)", min_value=0.001, step=0.1),
        "thickness_m": N("Thk (m)", min_value=0.05, step=0.05),
        "grade": S("Grade", options=CONCRETE_GRADES),
        "quantity": N("Nos", min_value=1, step=1),
    }, critical=True)
    editor("hdpe_liners", "HDPE liner", HDPELiner, {
        "tag": T("Tag", required=True),
        "length_m": N("L (m)", min_value=0.1, step=0.5),
        "width_m": N("W (m)", min_value=0.1, step=0.5),
        "thickness_mm": N("Thk (mm)", min_value=0.5, step=0.5),
    })

with tabs[1]:
    editor("pedestals", "Pedestals", Pedestal, {
        "tag": T("Tag", required=True, help="e.g. P1 … P7"),
        "length_m": N("L (m)", min_value=0.001, step=0.05),
        "width_m": N("W (m)", min_value=0.001, step=0.05),
        "height_m": N("H (m)", min_value=0.001, step=0.05,
                      help="Not printed on the callouts — take it from a section"),
        "quantity": N("Nos", min_value=1, step=1),
        "grade": S("Grade", options=CONCRETE_GRADES),
        "rebar_coefficient_kg_per_m3": N("Rebar coeff (kg/m³)",
                                         min_value=0.0, step=10.0),
    }, caption="Default rebar coefficient 120 kg/m³. Heights extracted from a "
               "drawing arrive as a placeholder — check every one.", critical=True)
    editor("grade_slabs", "Grade slab", GradeSlab, {
        "tag": T("Tag", required=True),
        "length_m": N("L (m)", min_value=0.001, step=0.5),
        "width_m": N("W (m)", min_value=0.001, step=0.5),
        "thickness_m": N("Thk (m)", min_value=0.05, step=0.05),
        "grade": S("Grade", options=CONCRETE_GRADES),
        "rebar_coefficient_kg_per_m3": N("Rebar coeff (kg/m³)",
                                         min_value=0.0, step=10.0),
        "has_top_formwork": C("Top formwork?"),
    }, critical=True)
    editor("sumps", "Sump / pit", Sump, {
        "tag": T("Tag", required=True),
        "outer_length_m": N("Outer L (m)", min_value=0.1, step=0.1),
        "outer_width_m": N("Outer W (m)", min_value=0.1, step=0.1),
        "depth_m": N("Depth (m)", min_value=0.1, step=0.1),
        "wall_thickness_m": N("Wall Thk (m)", min_value=0.05, step=0.05),
        "base_thickness_m": N("Base Thk (m)", min_value=0.05, step=0.05),
        "grade": S("Grade", options=CONCRETE_GRADES),
        "rebar_coefficient_kg_per_m3": N("Rebar coeff (kg/m³)",
                                         min_value=0.0, step=10.0),
    })
    editor("curb_walls", "Curb / dyke wall", CurbWall, {
        "tag": T("Tag", required=True),
        "length_m": N("L (m)", min_value=0.1, step=0.5),
        "height_m": N("H (m)", min_value=0.05, step=0.05),
        "thickness_m": N("Thk (m)", min_value=0.05, step=0.05),
        "grade": S("Grade", options=CONCRETE_GRADES),
        "rebar_coefficient_kg_per_m3": N("Rebar coeff (kg/m³)",
                                         min_value=0.0, step=10.0),
    })

with tabs[2]:
    editor("rebar_bars", "Rebar — manual BBS", RebarBar, {
        "tag": T("Bar mark", required=True),
        "parent_element": T("Parent tag", required=True),
        "diameter_mm": S("Dia (mm)", options=REBAR_DIAMETERS),
        "cut_length_m": N("Cut len (m)", min_value=0.01, step=0.1),
        "nos_per_element": N("Nos/elem", min_value=1, step=1),
        "parent_quantity": N("Nos of parent", min_value=1, step=1),
    }, caption="Optional. An element with both a coefficient and manual bars is "
               "flagged on the Review page — pick one path per element.")
    editor("formwork_loose", "Loose formwork", FormworkLoose, {
        "tag": T("Tag", required=True),
        "area_m2": N("Area (m²)", min_value=0.01, step=1.0),
        "description": T("Description"),
    }, caption="Formwork to pedestals, slabs, sumps and curbs is computed "
               "automatically. Use this for anything else — or a negative area "
               "to deduct faces cast against soil.")

with tabs[3]:
    editor("epoxy_coatings", "Epoxy / acid-resistant coating", EpoxyCoating, {
        "tag": T("Tag", required=True),
        "area_m2": N("Area (m²)", min_value=0.1, step=1.0),
        "thickness_mm": N("Thk (mm)", min_value=0.5, step=0.5),
        "coats": N("Coats", min_value=1, step=1),
    })
    editor("joints", "Joints", Joint, {
        "tag": T("Tag", required=True),
        "joint_type": S("Type", options=JOINT_TYPES),
        "length_m": N("Length (m)", min_value=0.01, step=0.5),
        "has_waterstop": C("Waterstop?"),
        "has_sealant": C("Sealant?"),
        "has_backer_rod": C("Backer rod?"),
    })
    editor("embedments", "Embedments", Embedment, {
        "tag": T("Tag", required=True),
        "embedment_type": S("Type", options=EMBEDMENT_TYPES),
        "quantity": N("Nos", min_value=1, step=1),
        "size_description": T("Size", required=True),
        "unit_weight_kg": N("Unit wt (kg) — optional", min_value=0.0, step=0.1),
    }, caption="Insert plates, anchor bolts, dowels, sleeves.")
    editor("waterstop_runs", "Waterstop — standalone runs", WaterstopRun, {
        "tag": T("Tag", required=True),
        "length_m": N("Length (m)", min_value=0.1, step=0.5),
        "material": S("Material", options=WATERSTOP_MATERIALS),
        "width_mm": N("Width (mm)", min_value=50.0, step=25.0),
    })
    editor("sump_ancillaries", "Sump ancillaries", SumpAncillary, {
        "tag": T("Tag", required=True),
        "item_type": S("Type", options=SUMP_ANCILLARY_TYPES),
        "quantity": N("Nos", min_value=1, step=1),
        "size_description": T("Size"),
        "length_m": N("Length (m) — pipes only", min_value=0.0, step=0.5),
    }, caption="Gratings, drain pipes, covers.")

st.divider()
st.subheader("Where this estimate stands")
kit.readiness_panel(project)
kit.sidebar_summary(project)
kit.sidebar_account()
