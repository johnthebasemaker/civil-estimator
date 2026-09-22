"""BOQ tab: the combined bill, and the project estimate.

* **Combined BOQ** — every drawing the queue has read, one tick box per line
  item, built into one consolidated workbook linked back to each drawing's own
  sheet. Reading is machine time and happens over as many sittings as it takes;
  deciding what belongs in the bill is an estimator's judgement and wants every
  line on one screen.
* **Project estimate** — what used to be three pages (Input, Review, BOM): the
  elements, their wastage, the bill they make, and its workbook. One drawing's
  reading lands here when its BOQ is generated with "Also add it to the project
  estimate"; the rest is typed in.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from core import jobstore as JS
from core import set_workbook as SW
from core import extract_cache as CACHE
from core.bom_builder import build_bom
from core.derivation import apply_derived, default_rules, derive, rules_from_findings
from core.excel_writer import write_workbook
from core.filename import build_output_path, sanitise
from core.models import (
    CompactedSoil, CurbWall, Embedment, EpoxyCoating, Excavation, FormworkLoose,
    GradeSlab, HDPELiner, Joint, PCCBlinding, Pedestal, Project, RebarBar, Sump,
    SumpAncillary, WaterstopRun,
)
from core.search import search_rows
from extractors import qwen_vision as QV
from extractors import st_compat as SC
from ui import kit
from ui.workspace import common as C

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def render() -> None:
    combined, estimate = st.tabs(["Combined BOQ", "Project estimate"])
    with combined:
        C.guarded("Combined BOQ", _combined)
    with estimate:
        C.guarded("Project estimate", _estimate)


# ======================================================================
# Combined BOQ
# ======================================================================
def _entries_from_queue(owner: str, rules, project_name: str) -> "OrderedDict":
    """Every finished drawing, rebuilt from its saved extraction.

    Nothing here touches the model. The queue records where each result was
    written and the cache holds it under the drawing's content hash, so this
    runs in milliseconds no matter how many sittings the set took.
    """
    out: "OrderedDict[str, dict]" = OrderedDict()
    for job in JS.list_jobs(owner=owner, states=[JS.DONE]):
        result = CACHE.load(job.fingerprint)
        if result is None:
            continue
        proj = Project(project_name=project_name, drawing_no="")
        QV.merge_into_project(proj, result)
        if not proj.drawing_no:
            proj.drawing_no = sanitise(job.path.stem)

        derived_tags: set[str] = set()
        seeded = rules_from_findings(rules, result.findings)
        items = derive(proj, seeded)
        if items:
            derived_tags = {getattr(i.element, "tag", "") for i in items}
            apply_derived(proj, items)
        placeholders = {p.tag for p in proj.pedestals
                        if abs(p.height_m - QV.PLACEHOLDER_HEIGHT_M) < 1e-9}
        notes = []
        if result.used_text_layer:
            notes.append("read from the sheet's own text layer (no model calls)")
        if job.from_cache:
            notes.append("served from the saved extraction")
        entry = SW.build_entry(
            proj.drawing_no, proj, source_pdf=job.drawing_path,
            derived_tags=derived_tags, placeholder_tags=placeholders,
            notes=notes, position_marks=result.position_marks,
            discovery=result.discovery,
            classification=C.classification_of(job.path))
        out[proj.drawing_no] = {"entry": entry, "job": job, "result": result,
                                "derived": items}
    return out


def _line_rows(entries) -> list[dict]:
    """One selectable row per line item, across every extracted drawing."""
    rows = []
    for drawing_no, bundle in entries.items():
        entry = bundle["entry"]
        for line in entry.bom.lines:
            if line.category == "ROLLUP":
                continue
            rows.append({
                "key": f"{drawing_no}|boq|{line.category}|{line.item}|{line.unit}",
                "Drawing": drawing_no, "Source": "BOQ",
                "Group": line.category, "Description": line.item,
                "UoM": line.unit, "Qty": round(line.qty_gross, 3),
            })
        for item in entry.discovered:
            rows.append({
                "key": f"{drawing_no}|read|{item['description']}|{item['uom']}",
                "Drawing": drawing_no, "Source": "Read from drawing",
                "Group": item.get("kind", ""), "Description": item["description"],
                "UoM": item["uom"], "Qty": item.get("qty"),
            })
    return rows


def _apply_selection(entries, chosen: set[str]) -> list:
    """Keep only the ticked lines, and drop a drawing left with nothing."""
    kept = []
    for drawing_no, bundle in entries.items():
        entry = bundle["entry"]
        entry.bom.lines = [
            line for line in entry.bom.lines
            if line.category == "ROLLUP"
            or f"{drawing_no}|boq|{line.category}|{line.item}|{line.unit}" in chosen]
        items = [i for i in entry.discovered
                 if f"{drawing_no}|read|{i['description']}|{i['uom']}" in chosen]
        entry.discovery = {**entry.discovery, "items": items}
        if entry.has_content:
            kept.append(entry)
    return kept


def _combined() -> None:
    owner = C.owner()
    proj = C.project()
    done = JS.list_jobs(owner=owner, states=[JS.DONE])
    if not done:
        st.info("No drawings are in this bill yet. Tick them on the **Drawings** "
                "tab and press **Add to the queue**. Drawings read before are "
                "here at once; the rest appear as soon as the worker reads them.")
        _download_panel()
        return

    st.subheader("Choose what goes in the BOQ")
    if "q_rules" not in st.session_state:
        st.session_state.q_rules = default_rules()
    with st.expander("Derive the quantities the drawings do not print",
                     expanded=False):
        C.rule_toggles(st.session_state.q_rules, key_prefix="qr_")

    all_entries = _entries_from_queue(owner, st.session_state.q_rules,
                                      proj.project_name)
    if not all_entries:
        st.info("The finished extractions carry no quantities yet.")
        return

    # ---- which drawings go into this bill ---------------------------------
    # The set is read over several sittings, so "everything extracted" and
    # "everything in this bill" are different lists. Ticking drawings here is
    # what turns several separate extractions into one consolidated workbook.
    st.markdown("**Drawings to combine into one BOQ**")
    picks_d: dict = st.session_state.setdefault("drawing_picks", {})
    for drawing_no in all_entries:
        picks_d.setdefault(drawing_no, True)

    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Select all", use_container_width=True, key="dwg_all"):
        for drawing_no in all_entries:
            picks_d[drawing_no] = True
        SC.rerun()
    if b2.button("Clear", use_container_width=True, key="dwg_none"):
        for drawing_no in all_entries:
            picks_d[drawing_no] = False
        SC.rerun()

    cols = st.columns(2)
    for i, (drawing_no, bundle) in enumerate(all_entries.items()):
        entry, job = bundle["entry"], bundle["job"]
        n_boq = len([ln for ln in entry.bom.lines if ln.category != "ROLLUP"])
        n_read = len(entry.discovered)
        suffix = " · from the saved extraction" if job.from_cache else ""
        picks_d[drawing_no] = cols[i % 2].checkbox(
            f"**{drawing_no}** — {n_boq} BOQ line(s), {n_read} read from the "
            f"drawing{suffix}",
            value=picks_d.get(drawing_no, True), key=f"dwg::{drawing_no}")

    entries = OrderedDict((k, v) for k, v in all_entries.items() if picks_d.get(k))
    if not entries:
        st.warning("No drawing ticked. Tick at least one to build a BOQ.")
        _download_panel()
        return

    st.divider()
    rows = _line_rows(entries)
    st.caption(f"{len(rows)} line(s) from {len(entries)} of "
               f"{len(all_entries)} extracted drawing(s). Untick anything that "
               f"does not belong in this bill.")

    # One box across all the fields. With a hundred-odd lines from a dozen
    # drawings, scrolling a multiselect to find "epoxy" is slower than typing
    # it, and an estimator looking for one item knows a word from it long
    # before they know which drawing it came from.
    search = st.text_input(
        "Search", key="sel_search", placeholder="Search drawing, source, "
        "group or description — e.g. 0107, rebar, epoxy, pedestal",
        help="Matches any of the columns. Every word you type has to appear "
             "somewhere in the row, so 'rebar 0107' narrows twice.")

    f1, f2, f3, f4 = st.columns([2, 2, 1, 1])
    matching = search_rows(rows, search)
    options = sorted({r["Drawing"] for r in matching}) or sorted(entries)
    which = f1.multiselect("Filter by drawing", options, default=[], key="sel_dwg",
                           help="Narrowed by the search box above, so typing "
                                "part of a number then picking from this list "
                                "is two steps rather than a long scroll.")
    kinds = f2.multiselect("Filter by source", ["BOQ", "Read from drawing"],
                           default=[], key="sel_src")
    visible = [r for r in matching
               if (not which or r["Drawing"] in which)
               and (not kinds or r["Source"] in kinds)]

    if search and not visible:
        st.info(f"Nothing matches “{search}”. Clear the box to see all "
                f"{len(rows)} line(s) again.")
    elif search:
        st.caption(f"{len(visible)} of {len(rows)} line(s) match “{search}”.")

    picks: dict = st.session_state.setdefault("line_picks", {})
    for row in rows:
        picks.setdefault(row["key"], row["Source"] == "BOQ")

    if f3.button("Select all", use_container_width=True, key="sel_all"):
        for row in visible:
            picks[row["key"]] = True
        SC.rerun()
    if f4.button("Clear", use_container_width=True, key="sel_none"):
        for row in visible:
            picks[row["key"]] = False
        SC.rerun()

    table = pd.DataFrame([{**{"Include": picks[r["key"]]},
                           **{k: v for k, v in r.items() if k != "key"}}
                          for r in visible])
    edited = st.data_editor(
        table, hide_index=True, use_container_width=True, height=420,
        disabled=["Drawing", "Source", "Group", "Description", "UoM", "Qty"],
        column_config={"Include": st.column_config.CheckboxColumn(
            "Include", help="Ticked lines go into the BOQ")},
        key="line_editor")
    if not edited.empty:
        for row, include in zip(visible, edited["Include"].tolist()):
            picks[row["key"]] = bool(include)

    chosen = {k for k, v in picks.items() if v}
    st.caption(f"{len(chosen)} line(s) ticked.")

    if st.button(f"🧾 Generate one combined BOQ from {len(entries)} drawing(s)",
                 type="primary", use_container_width=True, disabled=not chosen,
                 key="set_generate"):
        kept = _apply_selection(entries, chosen)
        if not kept:
            st.error("Nothing ticked on any drawing.")
        else:
            out = SW.write_set_workbook(kept, C.OUTPUT_DIR / "SET_BOQ.xlsx",
                                        project_name=proj.project_name)
            st.session_state["set_boq_path"] = str(out)
            st.session_state["set_boq_built"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            st.session_state["set_boq_lines"] = len(chosen)
            st.session_state["set_boq_drawings"] = len(kept)

    _download_panel()


def _download_panel() -> None:
    """Results stay on screen until they are explicitly cleared.

    A download triggers a rerun. Buttons built inside the run that produced the
    file vanished with it, which looked exactly like the work had been thrown
    away — so the panel reads from disk instead.
    """
    path = st.session_state.get("set_boq_path")
    if not path or not Path(path).exists():
        return
    path = Path(path)
    st.success(f"Consolidated BOQ built {st.session_state.get('set_boq_built', '')} "
               f"— {st.session_state.get('set_boq_drawings', 0)} drawing(s), "
               f"{st.session_state.get('set_boq_lines', 0)} line(s).")
    d1, d2 = st.columns([2, 1])
    with d1, open(path, "rb") as fh:
        st.download_button(
            "⬇️  Download consolidated BOQ (.xlsx)", data=fh.read(),
            file_name=path.name, use_container_width=True, key="dl_set",
            mime=XLSX)
    st.caption(f"Also saved at `{path}`. Download it as many times as you like; "
               f"nothing is removed until you press the button beside it.")
    report = SW.write_queue_report(
        JS.list_jobs(owner=C.owner()), C.OUTPUT_DIR / "SET_EXTRACTION_LOG.xlsx",
        project_name=C.project().project_name)
    with open(report, "rb") as fh:
        st.download_button(
            "⬇️  Download extraction log (.xlsx)", data=fh.read(),
            file_name=report.name, use_container_width=True,
            key="dl_queue_report", mime=XLSX)
    st.caption("Which drawings were read, how long each took, and which came "
               "back from a saved extraction rather than the model.")
    with d2:
        if st.session_state.get("_confirm_clear_result"):
            if st.button("Yes, clear it", key="clear_result_yes",
                         use_container_width=True):
                for key in ("set_boq_path", "set_boq_built", "set_boq_lines",
                            "set_boq_drawings", "_confirm_clear_result"):
                    st.session_state.pop(key, None)
                SC.rerun()
            if st.button("Keep it", key="clear_result_no",
                         use_container_width=True):
                st.session_state.pop("_confirm_clear_result", None)
                SC.rerun()
        elif st.button("🧹 Clear this result", use_container_width=True,
                       key="clear_result"):
            st.session_state["_confirm_clear_result"] = True
            SC.rerun()
    if st.session_state.get("_confirm_clear_result"):
        st.warning("Clear the link to this workbook? The file stays on disk at "
                   "the path above, so this only removes it from the screen.",
                   icon="🧹")
    st.info("Every quantity in the Summary is a live link into that drawing's "
            "own activity sheet. Correct a net quantity or a wastage percentage "
            "there and the Summary and the set total follow.", icon="🔗")


# ======================================================================
# Project estimate  (was Input, Review and BOM)
# ======================================================================
SOIL_TYPES = ["ordinary", "hard_murrum", "soft_rock", "hard_rock"]
JOINT_TYPES = ["expansion", "contraction", "construction"]
EMBEDMENT_TYPES = ["insert_plate", "anchor_bolt", "dowel", "sleeve"]
WATERSTOP_MATERIALS = ["PVC", "hydrophilic", "bentonite"]
SUMP_ANCILLARY_TYPES = ["grating", "drain_pipe", "cover"]
REBAR_DIAMETERS = [6, 8, 10, 12, 16, 20, 25, 28, 32, 40]

N = st.column_config.NumberColumn
T = st.column_config.TextColumn
S = st.column_config.SelectboxColumn
K = st.column_config.CheckboxColumn

WASTAGE = (   # (field, label) — one row of four per two pairs
    ("wastage_concrete_pct", "Concrete"), ("wastage_rebar_pct", "Rebar"),
    ("wastage_formwork_pct", "Formwork"), ("wastage_hdpe_pct", "HDPE liner"),
    ("wastage_epoxy_pct", "Epoxy"), ("wastage_pcc_pct", "PCC"),
    ("wastage_soil_pct", "Compacted soil"),
)


def _estimate() -> None:
    proj = C.project()
    st.caption("The estimate this session is building. A drawing's reading lands "
               "here when you generate its BOQ with **Also add it to the project "
               "estimate** ticked; everything else is entered below.")
    kit.readiness_panel(proj, compact=True)
    with st.expander("What the estimate has, and what it is missing",
                     expanded=False):
        kit.readiness_panel(proj)

    st.subheader("1 · Elements")
    _derivation(proj)
    _elements(proj)

    st.subheader("2 · Wastage")
    _wastage(proj)

    st.subheader("3 · Check the bill")
    try:
        bom = build_bom(proj)
    except Exception as exc:                          # noqa: BLE001 - shown to user
        st.error(f"The bill could not be built from these elements: {exc}")
        return
    _check(proj, bom)

    st.subheader("4 · Download")
    _download(proj, bom)


# ---------------------------------------------------------------- elements
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


def _editor(proj: Project, field: str, label: str, model_cls, config: dict, *,
            caption: str = "", critical: bool = False) -> None:
    """One element type, in a section that opens only when it matters."""
    items = getattr(proj, field)
    n = len(items)
    badge = f" · {n}" if n else ""
    with st.expander(f"{'⛔ ' if critical and not n else ''}{label}{badge}",
                     expanded=bool(n) or critical):
        if caption:
            st.caption(caption)
        edited = st.data_editor(_to_df(items, list(config)), num_rows="dynamic",
                                use_container_width=True, column_config=config,
                                key=f"{field}_editor")
        setattr(proj, field, _to_list(edited, model_cls))


def _derivation(proj: Project) -> None:
    """The arithmetic an estimator would do by hand from the slab geometry."""
    if not proj.grade_slabs:
        st.caption("Add a grade slab (or generate a drawing's BOQ with one) and "
                   "the derivation rules — excavation, blinding, liner, coating, "
                   "curb wall, joints — become available.")
        return
    if "derive_rules" not in st.session_state:
        findings = getattr(st.session_state.get("extraction"), "findings", None)
        st.session_state.derive_rules = rules_from_findings(default_rules(), findings)
    rules = st.session_state.derive_rules
    with st.expander(f"Derive quantities from the slab · "
                     f"{sum(r.enabled for r in rules)} rule(s) selected",
                     expanded=False):
        st.caption("These are **not read from the drawing** — they follow from "
                   "the slab geometry plus site convention. Check each formula, "
                   "adjust the inputs, then apply. Nothing is added until you "
                   "press the button.")
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
        preview = derive(proj, rules)
        if not preview:
            st.caption("Tick a rule to see what it would add.")
            return
        st.markdown("**Would add**")
        st.dataframe(pd.DataFrame([{
            "Element": i.target_field, "Tag": getattr(i.element, "tag", ""),
            "How it was worked out": i.explanation} for i in preview]),
            hide_index=True, use_container_width=True)
        if st.button(f"➕ Apply {len(preview)} derived quantity(ies)",
                     type="primary", use_container_width=True, key="est_apply"):
            added = apply_derived(proj, preview)
            st.success(f"Added {len(added)} element(s). They are ordinary rows "
                       f"now — edit or delete them below.")
            st.rerun()


def _elements(proj: Project) -> None:
    """Grouped the way an estimator works down a foundation: dig it, blind it,
    pour it, reinforce it, finish it."""
    g = st.tabs(["🕳 Earthworks & sub-base", "🧱 Concrete",
                 "📐 Reinforcement & formwork", "🎨 Finishes & ancillaries"])
    grades = C.CONCRETE_GRADES
    with g[0]:
        _editor(proj, "excavations", "Excavation", Excavation, {
            "tag": T("Tag", required=True),
            "length_m": N("L (m)", min_value=0.001, step=0.1),
            "width_m": N("W (m)", min_value=0.001, step=0.1),
            "depth_m": N("Depth (m)", min_value=0.001, step=0.1),
            "quantity": N("Nos", min_value=1, step=1),
            "soil_type": S("Soil", options=SOIL_TYPES),
        }, critical=True)
        _editor(proj, "compacted_soils", "Compacted soil / fill", CompactedSoil, {
            "tag": T("Tag", required=True),
            "volume_m3": N("Volume (m³)", min_value=0.01, step=1.0),
            "description": T("Description"),
        })
        _editor(proj, "pcc_blindings", "PCC / blinding", PCCBlinding, {
            "tag": T("Tag", required=True),
            "length_m": N("L (m)", min_value=0.001, step=0.1),
            "width_m": N("W (m)", min_value=0.001, step=0.1),
            "thickness_m": N("Thk (m)", min_value=0.05, step=0.05),
            "grade": S("Grade", options=grades),
            "quantity": N("Nos", min_value=1, step=1),
        }, critical=True)
        _editor(proj, "hdpe_liners", "HDPE liner", HDPELiner, {
            "tag": T("Tag", required=True),
            "length_m": N("L (m)", min_value=0.1, step=0.5),
            "width_m": N("W (m)", min_value=0.1, step=0.5),
            "thickness_mm": N("Thk (mm)", min_value=0.5, step=0.5),
        })
    with g[1]:
        _editor(proj, "pedestals", "Pedestals", Pedestal, {
            "tag": T("Tag", required=True, help="e.g. P1 … P7"),
            "length_m": N("L (m)", min_value=0.001, step=0.05),
            "width_m": N("W (m)", min_value=0.001, step=0.05),
            "height_m": N("H (m)", min_value=0.001, step=0.05,
                          help="Not printed on the callouts — take it from a section"),
            "quantity": N("Nos", min_value=1, step=1),
            "grade": S("Grade", options=grades),
            "rebar_coefficient_kg_per_m3": N("Rebar coeff (kg/m³)",
                                             min_value=0.0, step=10.0),
        }, caption="Default rebar coefficient 120 kg/m³. Heights extracted from "
                   "a drawing arrive as a placeholder — check every one.",
            critical=True)
        _editor(proj, "grade_slabs", "Grade slab", GradeSlab, {
            "tag": T("Tag", required=True),
            "length_m": N("L (m)", min_value=0.001, step=0.5),
            "width_m": N("W (m)", min_value=0.001, step=0.5),
            "thickness_m": N("Thk (m)", min_value=0.05, step=0.05),
            "grade": S("Grade", options=grades),
            "rebar_coefficient_kg_per_m3": N("Rebar coeff (kg/m³)",
                                             min_value=0.0, step=10.0),
            "has_top_formwork": K("Top formwork?"),
        }, critical=True)
        _editor(proj, "sumps", "Sump / pit", Sump, {
            "tag": T("Tag", required=True),
            "outer_length_m": N("Outer L (m)", min_value=0.1, step=0.1),
            "outer_width_m": N("Outer W (m)", min_value=0.1, step=0.1),
            "depth_m": N("Depth (m)", min_value=0.1, step=0.1),
            "wall_thickness_m": N("Wall Thk (m)", min_value=0.05, step=0.05),
            "base_thickness_m": N("Base Thk (m)", min_value=0.05, step=0.05),
            "grade": S("Grade", options=grades),
            "rebar_coefficient_kg_per_m3": N("Rebar coeff (kg/m³)",
                                             min_value=0.0, step=10.0),
        })
        _editor(proj, "curb_walls", "Curb / dyke wall", CurbWall, {
            "tag": T("Tag", required=True),
            "length_m": N("L (m)", min_value=0.1, step=0.5),
            "height_m": N("H (m)", min_value=0.05, step=0.05),
            "thickness_m": N("Thk (m)", min_value=0.05, step=0.05),
            "grade": S("Grade", options=grades),
            "rebar_coefficient_kg_per_m3": N("Rebar coeff (kg/m³)",
                                             min_value=0.0, step=10.0),
        })
    with g[2]:
        _editor(proj, "rebar_bars", "Rebar — manual BBS", RebarBar, {
            "tag": T("Bar mark", required=True),
            "parent_element": T("Parent tag", required=True),
            "diameter_mm": S("Dia (mm)", options=REBAR_DIAMETERS),
            "cut_length_m": N("Cut len (m)", min_value=0.01, step=0.1),
            "nos_per_element": N("Nos/elem", min_value=1, step=1),
            "parent_quantity": N("Nos of parent", min_value=1, step=1),
        }, caption="Optional. An element with both a coefficient and manual bars "
                   "is flagged under Check the bill — pick one path per element.")
        _editor(proj, "formwork_loose", "Loose formwork", FormworkLoose, {
            "tag": T("Tag", required=True),
            "area_m2": N("Area (m²)", min_value=0.01, step=1.0),
            "description": T("Description"),
        }, caption="Formwork to pedestals, slabs, sumps and curbs is computed "
                   "automatically. Use this for anything else — or a negative "
                   "area to deduct faces cast against soil.")
    with g[3]:
        _editor(proj, "epoxy_coatings", "Epoxy / acid-resistant coating",
                EpoxyCoating, {
                    "tag": T("Tag", required=True),
                    "area_m2": N("Area (m²)", min_value=0.1, step=1.0),
                    "thickness_mm": N("Thk (mm)", min_value=0.5, step=0.5),
                    "coats": N("Coats", min_value=1, step=1),
                })
        _editor(proj, "joints", "Joints", Joint, {
            "tag": T("Tag", required=True),
            "joint_type": S("Type", options=JOINT_TYPES),
            "length_m": N("Length (m)", min_value=0.01, step=0.5),
            "has_waterstop": K("Waterstop?"),
            "has_sealant": K("Sealant?"),
            "has_backer_rod": K("Backer rod?"),
        })
        _editor(proj, "embedments", "Embedments", Embedment, {
            "tag": T("Tag", required=True),
            "embedment_type": S("Type", options=EMBEDMENT_TYPES),
            "quantity": N("Nos", min_value=1, step=1),
            "size_description": T("Size", required=True),
            "unit_weight_kg": N("Unit wt (kg) — optional", min_value=0.0, step=0.1),
        }, caption="Insert plates, anchor bolts, dowels, sleeves.")
        _editor(proj, "waterstop_runs", "Waterstop — standalone runs",
                WaterstopRun, {
                    "tag": T("Tag", required=True),
                    "length_m": N("Length (m)", min_value=0.1, step=0.5),
                    "material": S("Material", options=WATERSTOP_MATERIALS),
                    "width_mm": N("Width (mm)", min_value=50.0, step=25.0),
                })
        _editor(proj, "sump_ancillaries", "Sump ancillaries", SumpAncillary, {
            "tag": T("Tag", required=True),
            "item_type": S("Type", options=SUMP_ANCILLARY_TYPES),
            "quantity": N("Nos", min_value=1, step=1),
            "size_description": T("Size"),
            "length_m": N("Length (m) — pipes only", min_value=0.0, step=0.5),
        }, caption="Gratings, drain pipes, covers.")


# ---------------------------------------------------------------- wastage
def _wastage(proj: Project) -> None:
    st.caption("Allowances added on top of the net quantities, per material. "
               "They apply to this estimate only.")
    cols = st.columns(4)
    for i, (field, label) in enumerate(WASTAGE):
        with cols[i % 4]:
            setattr(proj, field, st.number_input(
                f"{label} %", 0.0, 25.0, float(getattr(proj, field)), 0.5,
                key=f"wst_{field}"))


# ---------------------------------------------------------------- check
def _check(proj: Project, bom) -> None:
    if bom.warnings:
        with st.expander(f"⚠️ {len(bom.warnings)} warning(s)", expanded=True):
            for w in bom.warnings:
                st.markdown(f"- {w}")
    lines = [ln for ln in bom.lines if ln.category != "ROLLUP"]
    if not lines:
        st.info("No elements entered yet. Add them under **Elements** above, or "
                "open a drawing and generate its BOQ with **Also add it to the "
                "project estimate** ticked.")
        return

    concrete = sum(ln.qty_gross for ln in lines
                   if ln.category in {"Structural Concrete", "PCC / Blinding"})
    rebar = sum(ln.qty_gross for ln in lines if ln.category.startswith("Rebar"))
    formwork = sum(ln.qty_gross for ln in lines if ln.category == "Formwork")
    excavation = sum(ln.qty_gross for ln in lines
                     if ln.category == "Earthwork" and "Excavation" in ln.item)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total concrete", f"{concrete:,.2f} m³")
    c2.metric("Total rebar", f"{rebar:,.0f} kg")
    c3.metric("Total formwork", f"{formwork:,.2f} m²")
    c4.metric("Total excavation", f"{excavation:,.2f} m³")

    st.dataframe(pd.DataFrame([{
        "#": i + 1, "Category": ln.category, "Item": ln.item, "Unit": ln.unit,
        "Qty (net)": round(ln.qty_net, 3), "Wastage %": ln.wastage_pct,
        "Qty (gross)": round(ln.qty_gross, 3), "Source": ln.source_tag,
        "Notes": ln.notes,
    } for i, ln in enumerate(bom.lines)]), hide_index=True,
        use_container_width=True, height=420)

    rollups = [ln for ln in bom.lines if ln.category == "ROLLUP"]
    if rollups:
        with st.expander("Rollup totals", expanded=False):
            st.dataframe(pd.DataFrame([{"Item": ln.item, "Qty": ln.qty_gross,
                                        "Unit": ln.unit} for ln in rollups]),
                         hide_index=True, use_container_width=True)
    if bom.rebar_bbs:
        with st.expander("Rebar bar bending schedule", expanded=False):
            st.dataframe(pd.DataFrame([{
                "Parent": r.parent_element, "Bar Mark": r.bar_mark,
                "Dia (mm)": r.diameter_mm, "Cut Length (m)": r.cut_length_m,
                "Nos": r.nos, "Total Length (m)": r.total_length_m,
                "Weight (kg)": r.weight_kg,
            } for r in bom.rebar_bbs]), hide_index=True, use_container_width=True)


# ---------------------------------------------------------------- download
def _download(proj: Project, bom) -> None:
    if not proj.drawing_no:
        st.info("This estimate has no drawing number yet. Open a drawing on the "
                "**Drawings** tab and set it under **Project details** — or "
                "generate that drawing's BOQ, which fills it in.")
        return
    if not [ln for ln in bom.lines if ln.category != "ROLLUP"]:
        st.info("Nothing to put in a workbook yet — add at least one element.")
        return

    target = build_output_path(proj.drawing_no, C.OUTPUT_DIR)
    st.caption(f"Output file name: `{target.name}` — includes a Costing sheet "
               f"priced from the rates on the **Pricing** tab.")
    add_verify = st.checkbox(
        "Add a Verification sheet (sign-off columns + drawing grid references)",
        value=bool(st.session_state.get("extraction")), key="est_verify",
        help="Only meaningful when the quantities came from an extraction — it "
             "lists each read value with its verbatim callout so a checker can "
             "confirm it against the sheet.")
    if st.button("🧾 Generate Excel BOQ", type="primary", use_container_width=True,
                 key="est_generate"):
        proj.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        with st.spinner("Building workbook…"):
            written = write_workbook(proj, bom, target)
            extraction = st.session_state.get("extraction")
            if add_verify and extraction is not None:
                from extractors import workbook_extras as WE
                WE.append_verification_sheet(written, extraction)
        st.session_state["last_output"] = str(written)
        st.success(f"Workbook written: `{written}`")

    last = st.session_state.get("last_output")
    if last and Path(last).exists():
        with open(last, "rb") as fh:
            st.download_button("⬇️ Download project BOQ (.xlsx)", data=fh.read(),
                               file_name=Path(last).name, mime=XLSX,
                               use_container_width=True, key="est_download")
