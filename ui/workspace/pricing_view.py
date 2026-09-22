"""Pricing tab: rates against the project estimate.

One rate per (category, unit), kept in `data/rates.db` so a rate set once is
there for the next project. The totals below the grid follow the grid as you
type; Save writes the rates, and the project workbook's Costing sheet uses them.

The combined BOQ for a set is quantities only — this tab prices the project
estimate (BOQ → Project estimate).
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import rate_library
from core.bom_builder import build_bom
from ui import kit
from ui.workspace import common as C


def render() -> None:
    proj = C.project()
    st.subheader("Pricing")
    st.caption("Prices the project estimate on the **BOQ** tab. Rates are kept "
               "in `data/rates.db`, so they carry over to the next project. The "
               "combined BOQ for a set carries quantities only.")

    blockers = kit.assess(proj).blockers
    for b in blockers:
        st.error(b, icon="🚫")
    if blockers:
        st.caption("Pricing an estimate with these open is how a tender goes "
                   "wrong. Fix them first.")

    try:
        bom = build_bom(proj)
    except Exception as exc:                          # noqa: BLE001 - shown to user
        st.error(f"The bill could not be built from this estimate: {exc}")
        return

    costable = [ln for ln in bom.lines if ln.category != "ROLLUP"]
    if not costable:
        st.info("Nothing to price yet. Add elements under **BOQ → Project "
                "estimate**, or open a drawing and generate its BOQ with **Also "
                "add it to the project estimate** ticked.")
        return

    # One rate per unique (category, unit) pair.
    rows = []
    for cat, unit in sorted({(ln.category, ln.unit) for ln in costable}):
        stored = rate_library.get_rate(cat, unit)
        qty = sum(ln.qty_gross for ln in costable
                  if ln.category == cat and ln.unit == unit)
        rows.append({"Category": cat, "Unit": unit, "Total Qty": round(qty, 3),
                     "Rate (SAR)": stored, "Amount (SAR)": round(qty * stored, 2)})

    st.markdown("**Rates by category and unit**")
    edited = st.data_editor(
        pd.DataFrame(rows), hide_index=True, use_container_width=True,
        disabled=["Category", "Unit", "Total Qty", "Amount (SAR)"],
        column_config={
            "Rate (SAR)": st.column_config.NumberColumn(min_value=0.0, step=10.0,
                                                        format="%.2f"),
            "Amount (SAR)": st.column_config.NumberColumn(format="%.2f"),
        },
        key="rate_editor")

    save, reload = st.columns(2)
    if save.button("💾 Save rates", type="primary", use_container_width=True,
                   key="rates_save"):
        for _, row in edited.iterrows():
            rate_library.set_rate(row["Category"], row["Unit"],
                                  float(row["Rate (SAR)"]))
        st.success(f"Saved {len(edited)} rate(s) to `data/rates.db`.")
        st.rerun()
    if reload.button("🔄 Reload saved rates", use_container_width=True,
                     key="rates_reload"):
        st.session_state.pop("rate_editor", None)
        st.rerun()

    total = 0.0
    by_category: dict[str, float] = {}
    for _, row in edited.iterrows():
        amount = float(row["Total Qty"]) * float(row["Rate (SAR)"])
        total += amount
        by_category[row["Category"]] = by_category.get(row["Category"], 0.0) + amount

    st.markdown("**Total at these rates**")
    c1, c2 = st.columns([1, 2])
    c1.metric("Grand total", f"SAR {total:,.2f}")
    with c2:
        st.dataframe(pd.DataFrame([
            {"Category": k, "Subtotal (SAR)": round(v, 2)}
            for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])]),
            hide_index=True, use_container_width=True)
    st.caption("💡 The project workbook (BOQ → Project estimate → Download) "
               "includes a Costing sheet with these rates.")
