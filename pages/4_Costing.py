"""Costing page — rate entry + persistence + live total."""
from __future__ import annotations

# Repair sys.path before anything heavy is imported: `streamlit run Home.py`
# uses the framework Python, which has no PyMuPDF. See sitepath.py.
import sitepath  # noqa: F401  (import first — it fixes the import path)
import streamlit as st
import pandas as pd
from core.models import Project
from core.bom_builder import build_bom
from core import rate_library
from ui import kit

st.set_page_config(page_title="Costing — Civil Estimator", layout="wide",
                   page_icon=kit.favicon())

# Each page is its own script, so each carries the gate: a login on the
# home page alone would be bypassed by navigating straight to a page URL.
kit.require_login()

if "project" not in st.session_state:
    st.session_state.project = Project(project_name="", drawing_no="")
project: Project = st.session_state.project

kit.page_header("Costing", project,
                "Rates persist across projects in `data/rates.db`.",
                step="Costing")
_cov = kit.assess(project)
if _cov.blockers:
    for _b in _cov.blockers:
        st.error(_b, icon="🚫")
    st.caption("Pricing an estimate with these open is how a tender goes wrong. "
               "Fix them on **Input** first.")

# ---------- Build BOM ----------
try:
    bom = build_bom(project)
except Exception as e:
    st.error(f"BOM build failed: {e}")
    st.stop()

costable = [l for l in bom.lines if l.category != "ROLLUP"]
if not costable:
    st.warning("Nothing to price yet. Add elements on the **Input** page first.")
    st.stop()

# ---------- Load rates keyed by (category, unit) ----------
# Group lines: one rate per unique (category, unit) pair
unique_pairs = sorted({(l.category, l.unit) for l in costable})
rate_rows = []
for cat, unit in unique_pairs:
    stored = rate_library.get_rate(cat, unit)
    qty = sum(l.qty_gross for l in costable if l.category == cat and l.unit == unit)
    rate_rows.append({
        "Category": cat, "Unit": unit,
        "Total Qty": round(qty, 3),
        "Rate (SAR)": stored,
        "Amount (SAR)": round(qty * stored, 2),
    })
rate_df = pd.DataFrame(rate_rows)

st.subheader("Rates by (Category × Unit)")
st.caption("Edit the **Rate (SAR)** column. Amounts update on save.")

edited = st.data_editor(
    rate_df, hide_index=True, use_container_width=True,
    disabled=["Category", "Unit", "Total Qty", "Amount (SAR)"],
    column_config={
        "Rate (SAR)": st.column_config.NumberColumn(min_value=0.0, step=10.0, format="%.2f"),
        "Amount (SAR)": st.column_config.NumberColumn(format="%.2f"),
    },
    key="rate_editor",
)

col_save, col_reset = st.columns([1, 1])
with col_save:
    if st.button("💾 Save rates", type="primary", use_container_width=True):
        n = 0
        for _, row in edited.iterrows():
            rate_library.set_rate(row["Category"], row["Unit"], float(row["Rate (SAR)"]))
            n += 1
        st.success(f"Saved {n} rates to `data/rates.db`.")
        st.rerun()

with col_reset:
    if st.button("🔄 Reload rates from DB", use_container_width=True):
        st.rerun()

st.divider()

# ---------- Live totals ----------
st.subheader("Total (using current rates)")
grand_total = 0.0
category_totals: dict[str, float] = {}
for _, row in edited.iterrows():
    amt = float(row["Total Qty"]) * float(row["Rate (SAR)"])
    grand_total += amt
    category_totals[row["Category"]] = category_totals.get(row["Category"], 0.0) + amt

c1, c2 = st.columns([1, 2])
c1.metric("Grand Total", f"SAR {grand_total:,.2f}")
with c2:
    cat_df = pd.DataFrame([
        {"Category": k, "Subtotal (SAR)": round(v, 2)}
        for k, v in sorted(category_totals.items(), key=lambda x: -x[1])
    ])
    st.dataframe(cat_df, hide_index=True, use_container_width=True)

st.info("💡 The Excel workbook from the **BOM** page includes a Costing sheet with these rates baked in.")

kit.sidebar_summary(project)
kit.sidebar_account()
