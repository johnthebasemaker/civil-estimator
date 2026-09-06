"""Excel workbook generator.

Produces a 10-sheet workbook from a Project + BOM:
1. Summary        — flat BOQ, all lines
2. Pedestals      — per-pedestal breakdown
3. Slab           — per-slab breakdown
4. Sump           — per-sump breakdown
5. Joints         — per-joint breakdown
6. Coating        — per-epoxy-area breakdown
7. Embedments     — per-embedment breakdown
8. Rebar_BBS      — bar-by-bar schedule (coefficient + manual)
9. Assumptions    — wastage %, rebar coefficients, densities (editable)
10. Costing       — rates × quantities, totals by category

Assumptions sheet is the single source of truth for wastage in the workbook:
qty_gross in the Summary is written as a formula = qty_net × (1 + wastage%).
Changing wastage in the Assumptions sheet recomputes the Summary and Costing
downstream. Rebar coefficient overrides are informational only (recompute
requires re-running the estimator).
"""
from __future__ import annotations
from pathlib import Path
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from core.models import Project
from core.bom_builder import BOM


# ---------- Styling ----------
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(bold=True, size=14, color="1F4E78")
SUBTITLE_FONT = Font(italic=True, size=10, color="595959")
ROLLUP_FILL = PatternFill("solid", fgColor="FFF2CC")
ROLLUP_FONT = Font(bold=True, color="7F6000")
WARN_FILL = PatternFill("solid", fgColor="FCE4D6")
WARN_FONT = Font(bold=True, color="C00000")
BORDER = Border(
    left=Side(style="thin", color="BFBFBF"),
    right=Side(style="thin", color="BFBFBF"),
    top=Side(style="thin", color="BFBFBF"),
    bottom=Side(style="thin", color="BFBFBF"),
)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
RIGHT = Alignment(horizontal="right", vertical="center")


# ---------- Public entry point ----------
def write_workbook(project: Project, bom: BOM, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Stamp generation time on the project (so all sheets show identical timestamp)
    if not project.created_at:
        project.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    wb = Workbook()
    wb.remove(wb.active)

    assumption_map = _write_assumptions(wb, project)
    _write_summary(wb, project, bom, assumption_map)
    _write_pedestals(wb, project, bom)
    _write_slabs(wb, project, bom)
    _write_sumps(wb, project, bom)
    _write_joints(wb, project, bom)
    _write_coating(wb, project, bom)
    _write_embedments(wb, project, bom)
    _write_rebar_bbs(wb, project, bom)
    _write_costing(wb, project, bom)

    wb.save(output_path)
    return output_path


# ---------- Helpers ----------
def _style_header_row(ws: Worksheet, row: int, ncols: int) -> None:
    for col in range(1, ncols + 1):
        c = ws.cell(row=row, column=col)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER
        c.border = BORDER


def _apply_borders(ws: Worksheet, first_row: int, last_row: int, ncols: int) -> None:
    for r in range(first_row, last_row + 1):
        for col in range(1, ncols + 1):
            ws.cell(row=r, column=col).border = BORDER


def _autosize(ws: Worksheet, max_width: int = 45) -> None:
    for col_cells in ws.columns:
        length = 0
        letter = None
        for cell in col_cells:
            # Safely check if the attribute exists to skip MergedCells
            if hasattr(cell, 'column_letter') and letter is None:
                letter = cell.column_letter
            
            val = cell.value
            if val is not None:
                length = max(length, len(str(val)))
        
        if letter:
            ws.column_dimensions[letter].width = min(max(length + 2, 10), max_width)


def _write_title_block(ws: Worksheet, project: Project, sheet_label: str) -> int:
    """Writes a 4-row title block; returns the next available row."""
    ws["A1"] = f"CIVIL ESTIMATOR — {sheet_label}"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"Project: {project.project_name or '-'}   |   "
                f"Drawing: {project.drawing_no or '-'}   |   "
                f"Rev: {project.revision or '-'}")
    ws["A2"].font = SUBTITLE_FONT
    ws["A3"] = (f"Prepared by: {project.prepared_by or '-'}   |   "
                f"Created: {project.created_at or datetime.now().strftime('%Y-%m-%d %H:%M')}")
    ws["A3"].font = SUBTITLE_FONT
    return 5


# ---------- 9. Assumptions (written first) ----------
def _write_assumptions(wb: Workbook, project: Project) -> dict[str, str]:
    """Returns a map of wastage_key -> cell reference (e.g. 'Assumptions!$B$7')
    so downstream sheets can point formulas at these cells.
    """
    ws = wb.create_sheet("Assumptions")
    row = _write_title_block(ws, project, "Assumptions (editable)")
        # Metadata block
    ws.cell(row=row, column=1, value="Source Drawing (PDF)").font = HEADER_FONT
    ws.cell(row=row, column=2, value=project.pdf_source_filename or "(not attached)")
    ws.cell(row=row, column=2).alignment = LEFT
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
    row += 1
    ws.cell(row=row, column=1, value="Generated").font = HEADER_FONT
    ws.cell(row=row, column=2, value=project.created_at or datetime.now().strftime("%Y-%m-%d %H:%M"))
    ws.cell(row=row, column=2).alignment = LEFT
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
    row += 2  # blank spacer

    ws.cell(row=row, column=1, value="Parameter").font = HEADER_FONT
    ws.cell(row=row, column=2, value="Value").font = HEADER_FONT
    ws.cell(row=row, column=3, value="Unit").font = HEADER_FONT
    ws.cell(row=row, column=4, value="Notes").font = HEADER_FONT
    _style_header_row(ws, row, 4)

    entries = [
        ("Wastage — Concrete",       project.wastage_concrete_pct, "%", "Structural + PCC concrete", "concrete"),
        ("Wastage — Rebar",          project.wastage_rebar_pct,    "%", "All rebar (manual + coefficient)", "rebar"),
        ("Wastage — Formwork",       project.wastage_formwork_pct, "%", "Contact area", "formwork"),
        ("Wastage — HDPE liner",     project.wastage_hdpe_pct,     "%", "Overlap + trim", "hdpe"),
        ("Wastage — Epoxy coating",  project.wastage_epoxy_pct,    "%", "Includes coat waste", "epoxy"),
        ("Wastage — PCC blinding",   project.wastage_pcc_pct,      "%", "Blinding layer", "pcc"),
        ("Wastage — Compacted soil", project.wastage_soil_pct,     "%", "Fill compaction losses", "soil"),
    ]

    cell_map: dict[str, str] = {}
    for i, (name, val, unit, notes, key) in enumerate(entries, start=1):
        r = row + i
        ws.cell(row=r, column=1, value=name).alignment = LEFT
        ws.cell(row=r, column=2, value=val).alignment = RIGHT
        ws.cell(row=r, column=3, value=unit).alignment = CENTER
        ws.cell(row=r, column=4, value=notes).alignment = LEFT
        ws.cell(row=r, column=2).number_format = "0.0"
        cell_map[key] = f"Assumptions!$B${r}"

    _apply_borders(ws, row, row + len(entries), 4)

    # Reference data block
    ref_row = row + len(entries) + 3
    ws.cell(row=ref_row, column=1, value="Reference — Rebar unit weights (kg/m, IS 1786)").font = TITLE_FONT
    rebar_row = ref_row + 1
    ws.cell(row=rebar_row, column=1, value="Diameter (mm)").font = HEADER_FONT
    ws.cell(row=rebar_row, column=2, value="Weight (kg/m)").font = HEADER_FONT
    _style_header_row(ws, rebar_row, 2)
    from core.formulas import REBAR_KG_PER_M
    for i, (dia, wt) in enumerate(sorted(REBAR_KG_PER_M.items()), start=1):
        ws.cell(row=rebar_row + i, column=1, value=dia).alignment = CENTER
        ws.cell(row=rebar_row + i, column=2, value=wt).alignment = RIGHT
        ws.cell(row=rebar_row + i, column=2).number_format = "0.000"
    _apply_borders(ws, rebar_row, rebar_row + len(REBAR_KG_PER_M), 2)

    _autosize(ws)
    return cell_map


# ---------- 1. Summary ----------
def _write_summary(wb: Workbook, project: Project, bom: BOM,
                   assumption_map: dict[str, str]) -> None:
    ws = wb.create_sheet("Summary", 0)  # insert at position 0
    row = _write_title_block(ws, project, "BOQ Summary")

    # Warnings block
    if bom.warnings:
        ws.cell(row=row, column=1, value="WARNINGS").font = WARN_FONT
        ws.cell(row=row, column=1).fill = WARN_FILL
        for i, w in enumerate(bom.warnings, start=1):
            c = ws.cell(row=row + i, column=1, value=f"• {w}")
            c.font = WARN_FONT
            c.fill = WARN_FILL
            c.alignment = LEFT
            ws.merge_cells(start_row=row + i, start_column=1, end_row=row + i, end_column=7)
        row += len(bom.warnings) + 2

    headers = ["#", "Category", "Item", "Unit", "Qty (net)", "Wastage %", "Qty (gross)", "Source", "Notes"]
    for col, h in enumerate(headers, start=1):
        ws.cell(row=row, column=col, value=h)
    _style_header_row(ws, row, len(headers))
    data_start = row + 1

    for idx, line in enumerate(bom.lines, start=1):
        r = data_start + idx - 1
        ws.cell(row=r, column=1, value=idx).alignment = CENTER
        ws.cell(row=r, column=2, value=line.category).alignment = LEFT
        ws.cell(row=r, column=3, value=line.item).alignment = LEFT
        ws.cell(row=r, column=4, value=line.unit).alignment = CENTER
        ws.cell(row=r, column=5, value=line.qty_net).alignment = RIGHT
        ws.cell(row=r, column=5).number_format = "#,##0.000"
        ws.cell(row=r, column=6, value=line.wastage_pct).alignment = RIGHT
        ws.cell(row=r, column=6).number_format = "0.0"
        # qty_gross as formula so it responds to wastage edits (except rollups)
        if line.category == "ROLLUP":
            ws.cell(row=r, column=7, value=line.qty_gross).alignment = RIGHT
        else:
            ws.cell(row=r, column=7, value=f"=E{r}*(1+F{r}/100)").alignment = RIGHT
        ws.cell(row=r, column=7).number_format = "#,##0.000"
        ws.cell(row=r, column=8, value=line.source_tag).alignment = CENTER
        ws.cell(row=r, column=9, value=line.notes).alignment = LEFT

        if line.category == "ROLLUP":
            for col in range(1, len(headers) + 1):
                ws.cell(row=r, column=col).fill = ROLLUP_FILL
                ws.cell(row=r, column=col).font = ROLLUP_FONT

    _apply_borders(ws, row, data_start + len(bom.lines) - 1, len(headers))
    ws.freeze_panes = f"A{data_start}"
    _autosize(ws)


# ---------- 2–7. Per-category detail sheets ----------
def _write_detail_sheet(wb: Workbook, project: Project, sheet_name: str,
                        label: str, records: list[dict]) -> None:
    ws = wb.create_sheet(sheet_name)
    row = _write_title_block(ws, project, label)

    if not records:
        ws.cell(row=row, column=1, value=f"(No {label.lower()} entered)").font = SUBTITLE_FONT
        _autosize(ws)
        return

    headers = list(records[0].keys())
    for col, h in enumerate(headers, start=1):
        ws.cell(row=row, column=col, value=h)
    _style_header_row(ws, row, len(headers))
    for i, rec in enumerate(records, start=1):
        for col, h in enumerate(headers, start=1):
            val = rec[h]
            ws.cell(row=row + i, column=col, value=val)
            if isinstance(val, (int, float)):
                ws.cell(row=row + i, column=col).number_format = "#,##0.000"
                ws.cell(row=row + i, column=col).alignment = RIGHT
            else:
                ws.cell(row=row + i, column=col).alignment = CENTER
    _apply_borders(ws, row, row + len(records), len(headers))
    _autosize(ws)


def _write_pedestals(wb, bom):  _write_detail_sheet(wb, _proj_from_bom(bom), "Pedestals", "Pedestals — Detail", bom.pedestals_detail)
def _write_slabs(wb, bom):      _write_detail_sheet(wb, _proj_from_bom(bom), "Slab", "Slabs — Detail", bom.slabs_detail)
def _write_sumps(wb, bom):      _write_detail_sheet(wb, _proj_from_bom(bom), "Sump", "Sumps — Detail", bom.sumps_detail)
def _write_joints(wb, bom):     _write_detail_sheet(wb, _proj_from_bom(bom), "Joints", "Joints — Detail", bom.joints_detail)
def _write_coating(wb, bom):    _write_detail_sheet(wb, _proj_from_bom(bom), "Coating", "Epoxy Coating — Detail", bom.coating_detail)
def _write_embedments(wb, bom): _write_detail_sheet(wb, _proj_from_bom(bom), "Embedments", "Embedments — Detail", bom.embedments_detail)


# The detail sheets currently only need the project for the title block.
# A tiny stub keeps them decoupled from Project state.
def _write_pedestals(wb, project, bom):
    _write_detail_sheet(wb, project, "Pedestals", "Pedestals — Detail", bom.pedestals_detail)
def _write_slabs(wb, project, bom):
    _write_detail_sheet(wb, project, "Slab", "Slabs — Detail", bom.slabs_detail)
def _write_sumps(wb, project, bom):
    _write_detail_sheet(wb, project, "Sump", "Sumps — Detail", bom.sumps_detail)
def _write_joints(wb, project, bom):
    _write_detail_sheet(wb, project, "Joints", "Joints — Detail", bom.joints_detail)
def _write_coating(wb, project, bom):
    _write_detail_sheet(wb, project, "Coating", "Epoxy Coating — Detail", bom.coating_detail)
def _write_embedments(wb, project, bom):
    _write_detail_sheet(wb, project, "Embedments", "Embedments — Detail", bom.embedments_detail)


# ---------- 8. Rebar BBS ----------
def _write_rebar_bbs(wb: Workbook, project: Project, bom: BOM) -> None:
    ws = wb.create_sheet("Rebar_BBS")
    row = _write_title_block(ws, project, "Rebar Bar Bending Schedule")

    headers = ["Parent Element", "Bar Mark", "Dia (mm)", "Cut Length (m)",
               "Nos (total)", "Total Length (m)", "Weight (kg)"]
    for col, h in enumerate(headers, start=1):
        ws.cell(row=row, column=col, value=h)
    _style_header_row(ws, row, len(headers))

    if not bom.rebar_bbs:
        ws.cell(row=row + 1, column=1, value="(No rebar entries)").font = SUBTITLE_FONT
        _autosize(ws)
        return

    for i, r in enumerate(bom.rebar_bbs, start=1):
        rr = row + i
        ws.cell(row=rr, column=1, value=r.parent_element).alignment = LEFT
        ws.cell(row=rr, column=2, value=r.bar_mark).alignment = CENTER
        ws.cell(row=rr, column=3, value=r.diameter_mm).alignment = CENTER
        ws.cell(row=rr, column=4, value=r.cut_length_m).alignment = RIGHT
        ws.cell(row=rr, column=5, value=r.nos).alignment = RIGHT
        ws.cell(row=rr, column=6, value=r.total_length_m).alignment = RIGHT
        ws.cell(row=rr, column=7, value=r.weight_kg).alignment = RIGHT
        ws.cell(row=rr, column=7).number_format = "#,##0.00"
        if r.bar_mark == "COEFF":
            for col in range(1, len(headers) + 1):
                ws.cell(row=rr, column=col).fill = ROLLUP_FILL

    total_row = row + len(bom.rebar_bbs) + 1
    ws.cell(row=total_row, column=1, value="TOTAL (kg)").font = HEADER_FONT
    ws.cell(row=total_row, column=7,
            value=f"=SUM(G{row + 1}:G{row + len(bom.rebar_bbs)})").font = HEADER_FONT
    ws.cell(row=total_row, column=7).number_format = "#,##0.00"

    _apply_borders(ws, row, total_row, len(headers))
    ws.freeze_panes = f"A{row + 1}"
    _autosize(ws)


# ---------- 10. Costing ----------
def _write_costing(wb: Workbook, project: Project, bom: BOM) -> None:
    ws = wb.create_sheet("Costing")
    row = _write_title_block(ws, project, "Costing (rates × quantities)")

    ws.cell(row=row, column=1, value="Enter unit rates in column F. Totals recompute automatically.").font = SUBTITLE_FONT
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=7)
    row += 2

    headers = ["#", "Category", "Item", "Unit", "Qty (gross)", "Unit Rate (SAR)", "Amount (SAR)"]
    for col, h in enumerate(headers, start=1):
        ws.cell(row=row, column=col, value=h)
    _style_header_row(ws, row, len(headers))
    data_start = row + 1

        # Exclude rollup lines (would double-count)
    costable = [l for l in bom.lines if l.category != "ROLLUP"]

    # Pre-fill rates from library (Step 6)
    from core import rate_library
    rate_cache: dict[tuple[str, str], float] = {}

    for idx, line in enumerate(costable, start=1):
        r = data_start + idx - 1
        ws.cell(row=r, column=1, value=idx).alignment = CENTER
        ws.cell(row=r, column=2, value=line.category).alignment = LEFT
        ws.cell(row=r, column=3, value=line.item).alignment = LEFT
        ws.cell(row=r, column=4, value=line.unit).alignment = CENTER
        ws.cell(row=r, column=5, value=line.qty_gross).alignment = RIGHT
        ws.cell(row=r, column=5).number_format = "#,##0.000"

        key = (line.category, line.unit)
        if key not in rate_cache:
            rate_cache[key] = rate_library.get_rate(*key)
        ws.cell(row=r, column=6, value=rate_cache[key]).alignment = RIGHT
        ws.cell(row=r, column=6).number_format = "#,##0.00"
        ws.cell(row=r, column=7, value=f"=E{r}*F{r}").alignment = RIGHT
        ws.cell(row=r, column=7).number_format = "#,##0.00"

    _apply_borders(ws, row, data_start + len(costable) - 1, len(headers))

    # Grand total + subtotals by category
    total_row = data_start + len(costable) + 1
    ws.cell(row=total_row, column=6, value="GRAND TOTAL (SAR)").font = HEADER_FONT
    ws.cell(row=total_row, column=6).alignment = RIGHT
    ws.cell(row=total_row, column=7,
            value=f"=SUM(G{data_start}:G{data_start + len(costable) - 1})").font = HEADER_FONT
    ws.cell(row=total_row, column=7).number_format = "#,##0.00"
    ws.cell(row=total_row, column=7).fill = ROLLUP_FILL

    # Subtotals by category (using SUMIF)
    sub_row = total_row + 3
    ws.cell(row=sub_row, column=2, value="Subtotals by Category").font = TITLE_FONT
    sub_row += 1
    ws.cell(row=sub_row, column=2, value="Category").font = HEADER_FONT
    ws.cell(row=sub_row, column=3, value="Subtotal (SAR)").font = HEADER_FONT
    _style_header_row(ws, sub_row, 3)
    categories = sorted({l.category for l in costable})
    for i, cat in enumerate(categories, start=1):
        rr = sub_row + i
        ws.cell(row=rr, column=2, value=cat).alignment = LEFT
        ws.cell(row=rr, column=3,
                value=f'=SUMIF(B{data_start}:B{data_start + len(costable) - 1},'
                      f'"{cat}",G{data_start}:G{data_start + len(costable) - 1})').alignment = RIGHT
        ws.cell(row=rr, column=3).number_format = "#,##0.00"

    ws.freeze_panes = f"A{data_start}"
    _autosize(ws)