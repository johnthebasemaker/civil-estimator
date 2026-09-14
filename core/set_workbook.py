"""Consolidated workbook for a set of drawings.

One file for the whole set, laid out the way an estimator reads a BOQ:

    Sl. #  |  Description  |  UoM  |  <drawing A>  |  <drawing B>  | … | Total

One row per distinct item, one column per drawing, so the same item across
sheets lines up and the set totals across. Behind the Summary sit per-drawing
detail sheets, one per activity, named by the short drawing code — Excel caps
sheet names at 31 characters and a Maaden drawing number is 26 on its own, so
the tab shows `0107_Pedestals` while the sheet's own header carries the number
in full.

Assumptions are marked, not hidden: any row whose quantity depends on a derived
rule or a placeholder height is shaded and flagged in an Assumption column, so
the reviewing engineer can see at a glance which numbers are read off a drawing
and which were worked out.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.bom_builder import BOM, build_bom
from core.models import Project

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(bold=True, size=15, color="1F4E78")
DRAFT_FONT = Font(bold=True, size=12, color="C00000")
ASSUMED_FILL = PatternFill("solid", fgColor="FFF2CC")     # derived / placeholder
TOTAL_FILL = PatternFill("solid", fgColor="EEF3F9")
GROUP_FILL = PatternFill("solid", fgColor="F3F6FA")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")

# Activities get their own detail sheet, in the order a foundation is built.
ACTIVITIES: "OrderedDict[str, tuple[str, ...]]" = OrderedDict([
    ("Earthwork", ("Earthwork",)),
    ("PCC", ("PCC / Blinding",)),
    ("Concrete", ("Structural Concrete",)),
    ("Formwork", ("Formwork",)),
    ("Rebar", ("Rebar (coefficient)", "Rebar (manual BBS)")),
    ("Liner", ("HDPE Liner",)),
    ("Coating", ("Epoxy Coating",)),
    ("Joints", ("Joints", "Joint accessories")),
    ("Embedments", ("Embedments",)),
    ("Sump", ("Sump Ancillaries",)),
])


# Which activity sheet a BOM category lands on. The Summary needs the reverse of
# ACTIVITIES to know which sheet to reach into for a given row.
CATEGORY_TO_ACTIVITY = {cat: act for act, cats in ACTIVITIES.items() for cat in cats}


@dataclass
class DrawingEntry:
    """One drawing's contribution to the set."""
    drawing_no: str
    project: Project
    bom: BOM
    source_pdf: str = ""
    derived_tags: set[str] = field(default_factory=set)
    placeholder_tags: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    # Element marks printed on the sheet, e.g. {"P1": 19}. Reported for review,
    # never as a quantity — see text_layer.count_position_marks.
    position_marks: dict[str, int] = field(default_factory=dict)
    # Whatever the sheet specifies in its own words — see extractors/discovery.
    discovery: dict = field(default_factory=dict)
    # Which site condition this drawing belongs to: new ground, work inside a
    # live plant, or making good what is already there. Set per drawing on the
    # Extract page, because one submission routinely mixes all three and they
    # are tendered at different rates.
    classification: str = ""

    @property
    def discovered(self) -> list[dict]:
        return list(self.discovery.get("items") or ())

    @property
    def has_content(self) -> bool:
        return bool(self.bom.lines or self.position_marks or self.discovered)

    @property
    def short_code(self) -> str:
        return short_code(self.drawing_no)


def short_code(drawing_no: str) -> str:
    """The distinguishing tail of a drawing number, for sheet tabs.

    `MD-522-8110-EG-CV-LAD-0107` -> `0107`. Where two sheets in a set share a
    tail (different areas, same serial) the previous group is folded in to keep
    tabs unique — `8110-0107` — because a duplicate tab name is silently
    renamed by Excel and the reader loses track of which drawing they are in.
    """
    parts = [p for p in re.split(r"[-_ ]", drawing_no or "") if p]
    return parts[-1] if parts else (drawing_no or "sheet")


def unique_codes(drawing_nos: list[str]) -> dict[str, str]:
    """Map each drawing number to a unique short tab code."""
    split = {n: [p for p in re.split(r"[-_ ]", n or "") if p] for n in drawing_nos}
    tails: dict[str, list[str]] = {}
    for number, parts in split.items():
        tails.setdefault(parts[-1] if parts else "sheet", []).append(number)

    out: dict[str, str] = {}
    for tail, numbers in tails.items():
        if len(numbers) == 1:
            out[numbers[0]] = tail
            continue
        # Same serial on different sheets: qualify with the part that actually
        # differs (the area code), not simply the one before it — "LAD-0107"
        # twice would still collide, and Excel renames duplicate tabs silently.
        for number in numbers:
            parts = split[number]
            others = [split[o] for o in numbers if o != number]
            prefix = next((p for i, p in enumerate(parts[:-1])
                           if any(i >= len(o) or o[i] != p for o in others)), None)
            out[number] = f"{prefix}-{tail}" if prefix else f"{parts[0]}-{tail}"
    used: dict[str, int] = {}
    for number, code in list(out.items()):
        if code in used:
            used[code] += 1
            out[number] = f"{code}_{used[code]}"
        else:
            used[code] = 1
    return out


def _sheet_name(code: str, activity: str) -> str:
    """Excel allows 31 characters and forbids : \\ / ? * [ ]."""
    name = f"{code}_{activity}"
    name = re.sub(r"[:\\/?*\[\]]", "-", name)
    return name[:31]


def build_entry(drawing_no: str, project: Project, *, source_pdf: str = "",
                derived_tags: set[str] | None = None,
                placeholder_tags: set[str] | None = None,
                notes: list[str] | None = None,
                position_marks: dict[str, int] | None = None,
                discovery: dict | None = None,
                classification: str = "") -> DrawingEntry:
    return DrawingEntry(drawing_no=drawing_no or "(no number)", project=project,
                        bom=build_bom(project), source_pdf=source_pdf,
                        derived_tags=set(derived_tags or ()),
                        placeholder_tags=set(placeholder_tags or ()),
                        notes=list(notes or ()),
                        position_marks=dict(position_marks or {}),
                        discovery=dict(discovery or {}),
                        classification=classification)


def _row_key(line) -> tuple[str, str, str]:
    return (line.category, line.item, line.unit)


def _assumption(entry: DrawingEntry, line) -> str:
    """Why a quantity should not be taken at face value, if so."""
    tag = (line.source_tag or "").upper()
    reasons = []
    if tag in {t.upper() for t in entry.derived_tags}:
        reasons.append("derived from slab geometry, not read from the drawing")
    if tag in {t.upper() for t in entry.placeholder_tags}:
        reasons.append("pedestal height is a placeholder")
    return "; ".join(reasons)


def write_set_workbook(entries: list[DrawingEntry], out_path: str | Path, *,
                       project_name: str = "") -> Path:
    """One workbook for the whole set."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # A sheet with no BOQ lines still earns a column when it carries position
    # marks. On this set the plan sheets mark where every pedestal goes but
    # reference their sizes to a details drawing, so dropping them would leave
    # five of eleven drawings out of the set entirely — and those marks are
    # exactly what a reviewer needs to turn into quantities.
    entries = [e for e in entries if e.has_content]
    codes = unique_codes([e.drawing_no for e in entries])

    wb = Workbook()
    wb.remove(wb.active)
    _write_summary(wb, entries, codes, project_name)
    _write_drawing_index(wb, entries, codes)
    for entry in entries:
        _write_detail_sheets(wb, entry, codes[entry.drawing_no])
        _write_discovered_sheet(wb, entry, codes[entry.drawing_no])
    # Written last so it can link to detail sheets that now exist, inserted at
    # position 2 so it sits with the other set-wide views rather than behind
    # forty per-drawing tabs. Every existing sheet keeps its content untouched.
    _write_location_report(wb, entries, codes)
    wb.save(out_path)
    return out_path


def _write_summary(wb: Workbook, entries: list[DrawingEntry],
                   codes: dict[str, str], project_name: str) -> None:
    ws = wb.create_sheet("Summary")
    ws["A1"] = "BILL OF QUANTITIES — DRAWING SET"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"{project_name or '(project)'}   ·   {len(entries)} drawing(s)"
                f"   ·   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    ws["A3"] = ("UNVERIFIED DRAFT — quantities were read from drawings by a "
                "local model. Shaded cells are assumptions, not readings. "
                "Check every row before pricing.")
    ws["A3"].font = DRAFT_FONT
    ws["A3"].alignment = LEFT
    for r in (2, 3):
        ws.merge_cells(start_row=r, start_column=1,
                       end_row=r, end_column=max(6, 4 + len(entries)))

    head = 5
    headers = ["Sl. #", "Description", "UoM"] + \
              [codes[e.drawing_no] for e in entries] + ["Total", "Assumptions"]
    for col, text in enumerate(headers, start=1):
        c = ws.cell(row=head, column=col, value=text)
        c.fill, c.font, c.alignment, c.border = HEADER_FILL, HEADER_FONT, CENTER, BORDER
    # Second header row spells the drawing numbers out in full.
    for i, entry in enumerate(entries):
        c = ws.cell(row=head + 1, column=4 + i, value=entry.drawing_no)
        c.font = Font(size=8, italic=True)
        c.alignment = CENTER
        c.border = BORDER
    for col in (1, 2, 3, 4 + len(entries), 5 + len(entries)):
        ws.cell(row=head + 1, column=col).border = BORDER

    # Collect every distinct item across the set, grouped by category.
    rows: "OrderedDict[tuple[str, str, str], dict]" = OrderedDict()
    for entry in entries:
        for line in entry.bom.lines:
            if line.category == "ROLLUP":
                continue
            key = _row_key(line)
            row = rows.setdefault(key, {"qty": {}, "assumptions": set()})
            row["qty"][entry.drawing_no] = row["qty"].get(entry.drawing_no, 0.0) \
                + line.qty_gross
            why = _assumption(entry, line)
            if why:
                row["assumptions"].add(why)

    r = head + 2
    first_item_row = r
    sl = 0
    for category in sorted({k[0] for k in rows}):
        c = ws.cell(row=r, column=1, value=category.upper())
        c.font = Font(bold=True, color="1F4E78")
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).fill = GROUP_FILL
            ws.cell(row=r, column=col).border = BORDER
        r += 1
        for key in [k for k in rows if k[0] == category]:
            _, item, unit = key
            data = rows[key]
            sl += 1
            ws.cell(row=r, column=1, value=sl).alignment = CENTER
            ws.cell(row=r, column=2, value=item).alignment = LEFT
            ws.cell(row=r, column=3, value=unit).alignment = CENTER
            activity = CATEGORY_TO_ACTIVITY.get(category, "")
            for i, entry in enumerate(entries):
                qty = data["qty"].get(entry.drawing_no)
                # The quantity is not copied here; it is read from the drawing's
                # own activity sheet. Correcting a line there — a net quantity, a
                # wastage percentage — updates this cell and the set total with
                # it, which is the whole point of keeping one workbook.
                sheet = _sheet_name(codes[entry.drawing_no], activity) if activity else ""
                formula = None
                if qty and sheet:
                    formula = _sumif(sheet, f"$B{r}", desc_col=DETAIL_DESC_COL,
                                     value_col=DETAIL_GROSS_COL,
                                     first=DETAIL_FIRST_ROW, last=DETAIL_LAST_ROW)
                cell = ws.cell(row=r, column=4 + i,
                               value=formula or (round(qty, 3) if qty else None))
                cell.number_format = "#,##0.000"
                cell.alignment = RIGHT
                if qty and _assumption(entry, _Fake(key, entry)):
                    cell.fill = ASSUMED_FILL
            first = get_column_letter(4)
            last = get_column_letter(3 + len(entries))
            total = ws.cell(row=r, column=4 + len(entries),
                            value=f"=SUM({first}{r}:{last}{r})")
            total.number_format = "#,##0.000"
            total.alignment = RIGHT
            total.fill = TOTAL_FILL
            note = ws.cell(row=r, column=5 + len(entries),
                           value="; ".join(sorted(data["assumptions"])))
            note.alignment = LEFT
            if data["assumptions"]:
                note.fill = ASSUMED_FILL
            for col in range(1, len(headers) + 1):
                ws.cell(row=r, column=col).border = BORDER
            r += 1

    r, sl = _write_spare_rows(ws, entries, codes, headers, r, sl, first_item_row)
    r, sl = _write_discovered_block(ws, entries, codes, headers, r, sl)

    # Position marks, kept well away from the priced rows.
    if any(e.position_marks for e in entries):
        r += 1
        c = ws.cell(row=r, column=1, value="FOR REVIEW — COUNTS, NOT QUANTITIES")
        c.font = Font(bold=True, color="C00000")
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).fill = ASSUMED_FILL
            ws.cell(row=r, column=col).border = BORDER
        r += 1
        note = ws.cell(row=r, column=1,
                       value="Element marks printed on each sheet. An element "
                             "drawn in both plan and section is labelled twice, "
                             "and an unlabelled one is not counted at all — so "
                             "these are a starting point for the reviewer, not "
                             "a takeoff. Nothing below is priced or totalled.")
        note.alignment = LEFT
        ws.merge_cells(start_row=r, start_column=1, end_row=r,
                       end_column=len(headers))
        r += 1
        for mark in sorted({m for e in entries for m in e.position_marks},
                           key=lambda m: (m[0], int(m[1:]))):
            sl += 1
            ws.cell(row=r, column=1, value=sl).alignment = CENTER
            ws.cell(row=r, column=2,
                    value=f"Position marks labelled {mark}").alignment = LEFT
            ws.cell(row=r, column=3, value="Nos").alignment = CENTER
            for i, entry in enumerate(entries):
                count = entry.position_marks.get(mark)
                cell = ws.cell(row=r, column=4 + i, value=count or None)
                cell.alignment = RIGHT
                if count:
                    cell.fill = ASSUMED_FILL
            ws.cell(row=r, column=5 + len(entries),
                    value="count of printed marks — confirm the real quantity"
                    ).alignment = LEFT
            for col in range(1, len(headers) + 1):
                ws.cell(row=r, column=col).border = BORDER
            r += 1

    ws.column_dimensions["A"].width = 7
    ws.column_dimensions["B"].width = 46
    ws.column_dimensions["C"].width = 7
    for i in range(len(entries)):
        ws.column_dimensions[get_column_letter(4 + i)].width = 13
    ws.column_dimensions[get_column_letter(4 + len(entries))].width = 14
    ws.column_dimensions[get_column_letter(5 + len(entries))].width = 52
    ws.freeze_panes = ws.cell(row=head + 2, column=4)


# Rows left blank on the Summary, pre-wired, so a line added on a detail sheet
# has somewhere to land.
SPARE_ROWS = 12


def _activity_sheets(entry: DrawingEntry, code: str) -> list[str]:
    """Every activity sheet this drawing actually has."""
    out = []
    for activity, categories in ACTIVITIES.items():
        if any(l.category in categories for l in entry.bom.lines):
            out.append(_sheet_name(code, activity))
    return out


def _write_spare_rows(ws, entries: list[DrawingEntry], codes: dict[str, str],
                      headers: list[str], r: int, sl: int,
                      first_item_row: int) -> tuple[int, int]:
    """Blank rows that link themselves the moment you name them.

    The one real limit of matching on a description is that a line your team
    *adds* to a detail sheet has no Summary row to land in. Rather than leave
    that as a footnote, the Summary carries spare rows whose formulas are
    already written and which search every one of that drawing's activity
    sheets. Type the description you used and the quantity appears.

    The reconciliation row underneath makes the problem visible in the first
    place: it counts, per drawing, the lines on that drawing's detail sheets
    whose description appears nowhere on this Summary. Zero means nothing has
    been missed. Anything else names exactly how many rows to fill in.
    """
    if not entries:
        return r, sl
    last_item_row = r - 1

    r += 1
    c = ws.cell(row=r, column=1, value="ADD YOUR OWN LINES HERE")
    c.font = Font(bold=True, color="1F4E78")
    for col in range(1, len(headers) + 1):
        ws.cell(row=r, column=col).fill = GROUP_FILL
        ws.cell(row=r, column=col).border = BORDER
    r += 1
    note = ws.cell(row=r, column=1, value=(
        "Added a line to one of the detail sheets? Type its description in "
        "column B here and its UoM in column C. The quantity columns are "
        "already wired to search every activity sheet of each drawing, so the "
        "number appears as soon as the description matches."))
    note.alignment = LEFT
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
    r += 1

    spare_first = r
    for _ in range(SPARE_ROWS):
        sl += 1
        ws.cell(row=r, column=1, value=sl).alignment = CENTER
        ws.cell(row=r, column=2).alignment = LEFT
        ws.cell(row=r, column=3).alignment = CENTER
        for i, entry in enumerate(entries):
            sheets = _activity_sheets(entry, codes[entry.drawing_no])
            items_sheet = _sheet_name(codes[entry.drawing_no], "Drawing items")
            if entry.discovered:
                sheets_formula = [
                    _sumif(s, f"$B{r}", desc_col=DETAIL_DESC_COL,
                           value_col=DETAIL_GROSS_COL, first=DETAIL_FIRST_ROW,
                           last=DETAIL_LAST_ROW).lstrip("=") for s in sheets]
                sheets_formula.append(
                    _sumif(items_sheet, f"$B{r}", desc_col=ITEMS_DESC_COL,
                           value_col=ITEMS_QTY_COL, first=ITEMS_FIRST_ROW,
                           last=ITEMS_LAST_ROW).lstrip("="))
            else:
                sheets_formula = [
                    _sumif(s, f"$B{r}", desc_col=DETAIL_DESC_COL,
                           value_col=DETAIL_GROSS_COL, first=DETAIL_FIRST_ROW,
                           last=DETAIL_LAST_ROW).lstrip("=") for s in sheets]
            cell = ws.cell(row=r, column=4 + i,
                           value=("=" + "+".join(sheets_formula)) if sheets_formula
                           else None)
            cell.number_format = "#,##0.000"
            cell.alignment = RIGHT
        first_col = get_column_letter(4)
        last_col = get_column_letter(3 + len(entries))
        total = ws.cell(row=r, column=4 + len(entries),
                        value=f"=SUM({first_col}{r}:{last_col}{r})")
        total.number_format = "#,##0.000"
        total.alignment = RIGHT
        total.fill = TOTAL_FILL
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).border = BORDER
        r += 1
    spare_last = r - 1

    # --- the check that makes a missed line visible -------------------------
    r += 1
    c = ws.cell(row=r, column=1, value="LINES ON A DETAIL SHEET WITH NO SUMMARY ROW")
    c.font = Font(bold=True, color="C00000")
    for col in range(1, len(headers) + 1):
        ws.cell(row=r, column=col).fill = ASSUMED_FILL
        ws.cell(row=r, column=col).border = BORDER
    r += 1
    note = ws.cell(row=r, column=1, value=(
        "Counted live, per drawing. Zero means every line on that drawing's "
        "detail sheets is represented above. Anything else is the number of "
        "descriptions to copy into the spare rows."))
    note.alignment = LEFT
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
    r += 1

    ws.cell(row=r, column=2, value="Unmatched detail lines").alignment = LEFT
    ws.cell(row=r, column=3, value="count").alignment = CENTER
    summary_range = (f"$B${first_item_row}:$B${last_item_row},"
                     f"$B${spare_first}:$B${spare_last}")
    for i, entry in enumerate(entries):
        sheets = _activity_sheets(entry, codes[entry.drawing_no])
        terms = []
        for sheet in sheets:
            ref = _quote(sheet)
            rng = f"{ref}!${DETAIL_DESC_COL}${DETAIL_FIRST_ROW}:${DETAIL_DESC_COL}${DETAIL_LAST_ROW}"
            # A row counts as unmatched when it has a description and that
            # description appears in neither the priced rows above nor the
            # spare rows. The subtotal block on each detail sheet deliberately
            # leaves column B empty so it is never counted here.
            terms.append(
                f'SUMPRODUCT(({rng}<>"")*'
                f"(COUNTIF($B${first_item_row}:$B${last_item_row},{rng})=0)*"
                f"(COUNTIF($B${spare_first}:$B${spare_last},{rng})=0))")
        cell = ws.cell(row=r, column=4 + i,
                       value=("=" + "+".join(terms)) if terms else 0)
        cell.alignment = RIGHT
        cell.fill = ASSUMED_FILL
    ws.cell(row=r, column=5 + len(entries), value=(
        "0 is what you want. " + summary_range.replace("$", "")
        + " are the descriptions it checks against.")).alignment = LEFT
    for col in range(1, len(headers) + 1):
        ws.cell(row=r, column=col).border = BORDER
    r += 1
    return r, sl


def _discovered_key(row: dict) -> tuple[str, str]:
    return (str(row.get("description") or ""), str(row.get("uom") or ""))


def _priced_footprints(entry: DrawingEntry) -> set[tuple[int, int]]:
    """Plan sizes of elements the BOM already prices, in millimetres.

    The discovery pass reads a pedestal callout as plan area per unit; the BOM
    builder reads the same callout as concrete volume. Both belong in the
    workbook, but an estimator scanning two blocks needs to be told they are the
    same element seen twice. Matching on the numbers rather than on the wording
    is what makes that reliable — the two paths name things differently on
    purpose.
    """
    out: set[tuple[int, int]] = set()
    for ped in entry.project.pedestals:
        out.add(tuple(sorted((round(ped.length_m * 1000),
                              round(ped.width_m * 1000)))))
    for slab in entry.project.grade_slabs:
        out.add(tuple(sorted((round(slab.length_m * 1000),
                              round(slab.width_m * 1000)))))
    return out


def _double_count_warning(entry: DrawingEntry, item: dict) -> str:
    if item.get("kind") not in ("box2", "box3"):
        return ""
    spec = item.get("spec") or {}
    length, width = spec.get("length_mm"), spec.get("width_mm")
    if length is None or width is None:
        return ""
    pair = tuple(sorted((round(float(length)), round(float(width)))))
    if pair in _priced_footprints(entry):
        return "already priced above as concrete — do not add twice"
    return ""


def _write_discovered_block(ws, entries: list[DrawingEntry],
                            codes: dict[str, str], headers: list[str],
                            r: int, sl: int) -> tuple[int, int]:
    """Items the drawings specify that the priced rows above do not cover.

    These come from `extractors.discovery`, which names an item from the
    drawing's own wording instead of matching it against element types chosen in
    advance. That is the only way the sections-and-details sheets in this set
    contribute anything: they carry grout beds, coating, lugs and bar callouts
    and not one pedestal or slab the BOM builder knows how to price.

    Their numbers are what the sheet states — bars *per element*, volume *per
    unit*, a thickness with no area against it. Summing those across drawings
    would be arithmetic on incompatible things, so the Total column is left
    empty here and the basis is spelled out on every row instead.
    """
    rows: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    for entry in entries:
        for item in entry.discovered:
            data = rows.setdefault(_discovered_key(item),
                                   {"qty": {}, "basis": set(), "where": {}})
            if item.get("qty") is not None:
                data["qty"][entry.drawing_no] = item["qty"]
            if item.get("basis"):
                data["basis"].add(str(item["basis"]))
            warning = _double_count_warning(entry, item)
            if warning:
                data["basis"].add(warning)
            if item.get("grid_ref"):
                data["where"][entry.drawing_no] = item["grid_ref"]
    if not rows:
        return r, sl

    r += 1
    c = ws.cell(row=r, column=1, value="READ FROM THE DRAWINGS — CONFIRM BEFORE PRICING")
    c.font = Font(bold=True, color="C00000")
    for col in range(1, len(headers) + 1):
        ws.cell(row=r, column=col).fill = ASSUMED_FILL
        ws.cell(row=r, column=col).border = BORDER
    r += 1
    note = ws.cell(row=r, column=1, value=(
        "Items named by the drawings themselves rather than by a preset list. "
        "A figure here is what the sheet states — bars per element, volume per "
        "unit — and is not a set total, so these rows are not summed. Supply "
        "the missing area, length or count in the Assumptions column and the "
        "row becomes a quantity."))
    note.alignment = LEFT
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
    r += 1

    for (description, uom), data in rows.items():
        sl += 1
        ws.cell(row=r, column=1, value=sl).alignment = CENTER
        ws.cell(row=r, column=2, value=description).alignment = LEFT
        ws.cell(row=r, column=3, value=uom).alignment = CENTER
        for i, entry in enumerate(entries):
            # Linked, not copied — and this is the block where it matters most.
            # A row whose quantity is blank is the sheet stating a specification
            # without an extent; the reviewer types the area on the drawing's own
            # items sheet and it appears here.
            has_sheet = bool(entry.discovered or entry.discovery.get("specifications")
                             or entry.discovery.get("heights"))
            sheet = (_sheet_name(codes[entry.drawing_no], "Drawing items")
                     if has_sheet else "")
            named_here = any(_discovered_key(it) == (description, uom)
                             for it in entry.discovered)
            cell = ws.cell(row=r, column=4 + i, value=(
                _sumif(sheet, f"$B{r}", desc_col=ITEMS_DESC_COL,
                       value_col=ITEMS_QTY_COL, first=ITEMS_FIRST_ROW,
                       last=ITEMS_LAST_ROW) if (sheet and named_here) else None))
            cell.number_format = "#,##0.###"
            cell.alignment = RIGHT
            cell.fill = ASSUMED_FILL
        ws.cell(row=r, column=4 + len(entries), value="—").alignment = CENTER
        where = ", ".join(f"{code}" for code in sorted(set(data["where"].values())))
        basis = "; ".join(sorted(data["basis"]))
        if where:
            basis = f"{basis} (grid {where})" if basis else f"grid {where}"
        b = ws.cell(row=r, column=5 + len(entries), value=basis)
        b.alignment = LEFT
        b.fill = ASSUMED_FILL
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).border = BORDER
        r += 1
    return r, sl


class _Fake:
    """Adapter so _assumption can be reused from the summary aggregation."""

    def __init__(self, key, entry: DrawingEntry):
        self.category, self.item, self.unit = key
        self.source_tag = ""
        for line in entry.bom.lines:
            if _row_key(line) == key:
                self.source_tag = line.source_tag
                break


def _write_drawing_index(wb: Workbook, entries: list[DrawingEntry],
                         codes: dict[str, str]) -> None:
    ws = wb.create_sheet("Drawings")
    ws["A1"] = "DRAWINGS IN THIS SET"
    ws["A1"].font = TITLE_FONT
    headers = ["Tab code", "Drawing number", "Source file", "BOQ lines",
               "Pedestals", "Slabs", "Items read", "Notes"]
    for col, text in enumerate(headers, start=1):
        c = ws.cell(row=3, column=col, value=text)
        c.fill, c.font, c.alignment = HEADER_FILL, HEADER_FONT, CENTER
    for i, entry in enumerate(entries, start=4):
        ws.cell(row=i, column=1, value=codes[entry.drawing_no])
        ws.cell(row=i, column=2, value=entry.drawing_no)
        ws.cell(row=i, column=3, value=Path(entry.source_pdf).name)
        ws.cell(row=i, column=4, value=len(entry.bom.lines))
        ws.cell(row=i, column=5, value=len(entry.project.pedestals))
        ws.cell(row=i, column=6, value=len(entry.project.grade_slabs))
        ws.cell(row=i, column=7, value=len(entry.discovered))
        c = ws.cell(row=i, column=8, value=" | ".join(entry.notes))
        c.alignment = LEFT
    for col, width in {1: 12, 2: 30, 3: 40, 4: 11, 5: 11, 6: 8, 7: 11,
                       8: 70}.items():
        ws.column_dimensions[get_column_letter(col)].width = width


# How a discovered item was measured, in the order an estimator reads a section.
KIND_GROUPS = OrderedDict([
    ("box3", "Bodies — volume per unit"),
    ("box2", "Areas — plan area per unit"),
    ("thickness", "Layers — thickness stated, area to confirm"),
    ("compaction", "Fill and compaction — volume to confirm"),
    ("rebar", "Reinforcement — bars per element"),
    ("rebar_spacing", "Reinforcement — spacing, run length to confirm"),
    ("assembly", "Anchor and bolt assemblies"),
    ("diameter", "Round items — count to confirm"),
    ("set", "Items supplied per set"),
])


def _write_discovered_sheet(wb: Workbook, entry: DrawingEntry, code: str) -> None:
    """Everything this drawing specifies, in the drawing's own words.

    Kept as one sheet per drawing rather than split by activity, because the
    activity is exactly what is not known in advance here: the point of the
    discovery pass is that the sheet decides what is on it. Rows are grouped by
    how the item was measured, which is the thing that determines its unit.
    """
    items = entry.discovered
    spec = entry.discovery.get("specifications") or []
    heights = entry.discovery.get("heights") or []
    if not (items or spec or heights):
        return

    ws = wb.create_sheet(_sheet_name(code, "Drawing items"))
    ws["A1"] = f"READ FROM THE DRAWING — {entry.drawing_no}"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = Path(entry.source_pdf).name
    ws["A2"].font = Font(italic=True, size=9, color="595959")
    ws["A3"] = ("Named by the drawing, not by a preset list of element types. "
                "A blank quantity means the sheet states the specification but "
                "not the extent — fill it in and the row prices itself.")
    ws["A3"].font = DRAFT_FONT
    ws["A3"].alignment = LEFT
    ws.merge_cells("A3:H3")

    headers = ["Sl. #", "Description", "UoM", "Qty stated", "Basis",
               "Seen", "Grid ref", "Source text"]
    for col, text in enumerate(headers, start=1):
        c = ws.cell(row=5, column=col, value=text)
        c.fill, c.font, c.alignment, c.border = HEADER_FILL, HEADER_FONT, CENTER, BORDER

    r, sl = 6, 0
    by_kind: dict[str, list[dict]] = {}
    for item in items:
        by_kind.setdefault(str(item.get("kind") or "other"), []).append(item)
    ordered = [k for k in KIND_GROUPS if k in by_kind] + \
              [k for k in by_kind if k not in KIND_GROUPS]
    for kind in ordered:
        c = ws.cell(row=r, column=1, value=KIND_GROUPS.get(kind, kind).upper())
        c.font = Font(bold=True, color="1F4E78")
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).fill = GROUP_FILL
            ws.cell(row=r, column=col).border = BORDER
        r += 1
        for item in by_kind[kind]:
            sl += 1
            values = [sl, item.get("description"), item.get("uom"),
                      item.get("qty"), item.get("basis"),
                      item.get("occurrences"), item.get("grid_ref"),
                      item.get("source")]
            for col, value in enumerate(values, start=1):
                c = ws.cell(row=r, column=col, value=value)
                c.border = BORDER
                c.alignment = RIGHT if col in (4, 6) else (
                    CENTER if col in (1, 3, 7) else LEFT)
                if col == 4:
                    c.number_format = "#,##0.###"
                if item.get("confirm") and col in (4, 5):
                    c.fill = ASSUMED_FILL
            r += 1

    if spec or heights:
        r += 1
        c = ws.cell(row=r, column=1, value="STATED ON THE SHEET — CONTEXT, NOT ITEMS")
        c.font = Font(bold=True, color="1F4E78")
        r += 1
        for row in spec:
            ws.cell(row=r, column=1, value="Specification").alignment = LEFT
            ws.cell(row=r, column=2,
                    value=f"{row['value']:g} {row['unit']}").alignment = LEFT
            c = ws.cell(row=r, column=3, value=row["text"])
            c.alignment = LEFT
            ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=8)
            r += 1
        for row in heights:
            ws.cell(row=r, column=1, value="Height implied").alignment = LEFT
            ws.cell(row=r, column=2, value=f"{row['height_m']:g} m").alignment = LEFT
            c = ws.cell(row=r, column=3,
                        value=f"{row['top']} over {row['bottom']} — a pairing the "
                              f"section confirms, not the extractor")
            c.alignment = LEFT
            ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=8)
            r += 1

    for col, width in {1: 7, 2: 46, 3: 7, 4: 12, 5: 46, 6: 7, 7: 16,
                       8: 52}.items():
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A6"


# Where the linked numbers live on an activity sheet. The Summary reaches into
# these columns by SUMIF, so moving one means changing the formula too.
DETAIL_FIRST_ROW = 5
# Generous enough that rows appended by the reviewing engineer are still picked
# up, and bounded so the formulas stay legible when someone opens the sheet.
DETAIL_LAST_ROW = 800
DETAIL_DESC_COL = "B"
DETAIL_GROSS_COL = "F"
ITEMS_DESC_COL = "B"
ITEMS_QTY_COL = "D"
ITEMS_FIRST_ROW = 6
ITEMS_LAST_ROW = 400


def _quote(sheet_name: str) -> str:
    """A sheet reference Excel will accept. Names here carry spaces and hyphens."""
    return "'" + sheet_name.replace("'", "''") + "'"


def _sumif(sheet_name: str, key_cell: str, *, desc_col: str, value_col: str,
           first: int, last: int) -> str:
    ref = _quote(sheet_name)
    return (f"=SUMIF({ref}!${desc_col}${first}:${desc_col}${last},{key_cell},"
            f"{ref}!${value_col}${first}:${value_col}${last})")


def _write_detail_sheets(wb: Workbook, entry: DrawingEntry, code: str) -> None:
    """One sheet per activity present on this drawing."""
    for activity, categories in ACTIVITIES.items():
        lines = [l for l in entry.bom.lines if l.category in categories]
        if not lines:
            continue
        ws = wb.create_sheet(_sheet_name(code, activity))
        ws["A1"] = f"{activity.upper()} — {entry.drawing_no}"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = Path(entry.source_pdf).name
        ws["A2"].font = Font(italic=True, size=9, color="595959")

        headers = ["Sl. #", "Description", "UoM", "Qty (net)", "Wastage %",
                   "Qty (gross)", "Source tag", "Assumption", "Notes"]
        for col, text in enumerate(headers, start=1):
            c = ws.cell(row=4, column=col, value=text)
            c.fill, c.font, c.alignment, c.border = HEADER_FILL, HEADER_FONT, CENTER, BORDER
        for i, line in enumerate(lines, start=1):
            r = 4 + i
            why = _assumption(entry, line)
            values = [i, line.item, line.unit, round(line.qty_net, 3),
                      line.wastage_pct, None,
                      line.source_tag, why, line.notes]
            for col, value in enumerate(values, start=1):
                c = ws.cell(row=r, column=col, value=value)
                c.border = BORDER
                c.alignment = RIGHT if col in (4, 5, 6) else (
                    CENTER if col in (1, 3, 7) else LEFT)
                if col in (4, 5, 6):
                    c.number_format = "#,##0.000"
                if why and col in (4, 6, 8):
                    c.fill = ASSUMED_FILL
            # Gross is arithmetic, not a reading. Written as a formula so that
            # correcting the net quantity or the wastage on this sheet is enough
            # — the gross follows, and the Summary follows the gross.
            ws.cell(row=r, column=6, value=f"=D{r}*(1+E{r}/100)")

        last = 4 + len(lines)
        _write_sheet_subtotals(ws, lines, first=DETAIL_FIRST_ROW, last=last)
        for col, width in {1: 7, 2: 44, 3: 7, 4: 12, 5: 11, 6: 13, 7: 12,
                           8: 40, 9: 34}.items():
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.freeze_panes = "A5"


def _write_sheet_subtotals(ws, lines, *, first: int, last: int) -> None:
    """Per-unit totals for this sheet alone.

    An activity sheet mixes units — concrete in m3 beside formwork in m2 — so a
    single total at the foot would be a meaningless number that somebody would
    eventually price. One subtotal per unit is the honest version, and each is a
    SUMIF so it moves the moment a quantity above it is corrected.
    """
    units = []
    for line in lines:
        if line.unit not in units:
            units.append(line.unit)
    # Column B stays empty for the whole of this block, deliberately. The
    # Summary counts non-empty descriptions in column B to find lines that were
    # added here and have nowhere to go, and a heading sitting in that column
    # would be counted as one of them for ever.
    row = last + 2
    c = ws.cell(row=row, column=1, value="THIS SHEET — TOTAL BY UNIT")
    c.font = Font(bold=True, color="1F4E78")
    c.alignment = LEFT
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    ws.cell(row=row, column=6, value="Qty (gross)").font = Font(bold=True, size=9)
    for i, unit in enumerate(units, start=1):
        r = row + i
        ws.cell(row=r, column=3, value=unit).alignment = CENTER
        total = ws.cell(row=r, column=6, value=(
            f"=SUMIF($C${first}:$C${DETAIL_LAST_ROW},$C{r},"
            f"$F${first}:$F${DETAIL_LAST_ROW})"))
        total.number_format = "#,##0.000"
        total.alignment = RIGHT
        total.fill = TOTAL_FILL
        for col in (3, 6):
            ws.cell(row=r, column=col).border = BORDER
    note = ws.cell(row=row + len(units) + 1, column=1, value=(
        "These update themselves. Correct a quantity above and this block, the "
        "Summary row and the set total all follow. To add a line, insert it "
        "among the rows above and give it a description — if that description "
        "is not already on the Summary, put it in the Summary's spare rows and "
        "it will link itself."))
    note.alignment = LEFT
    ws.merge_cells(start_row=row + len(units) + 1, start_column=1,
                   end_row=row + len(units) + 1, end_column=9)


def write_queue_report(jobs, out_path: str | Path, *, project_name: str = "") -> Path:
    """What the queue did, as a workbook.

    The screen shows this while you are watching it. This is the version you
    send to someone who was not: which drawings were read, how long each took,
    which came back from the saved extraction rather than the model, and what
    went wrong with the ones that failed.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Extraction log"

    ws["A1"] = "EXTRACTION LOG"
    ws["A1"].font = TITLE_FONT
    done = [j for j in jobs if j.state == "done"]
    failed = [j for j in jobs if j.state == "failed"]
    cached = [j for j in done if j.from_cache]
    model_time = sum(j.elapsed_s for j in done if not j.from_cache)
    ws["A2"] = (f"{project_name or '(project)'}   ·   {len(done)} read, "
                f"{len(failed)} failed, {len(cached)} served from a saved "
                f"extraction   ·   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    ws.merge_cells("A2:H2")
    ws["A3"] = (f"{model_time / 60:.1f} minutes of extraction. The {len(cached)} "
                f"cached drawing(s) cost none of it — a drawing is read once and "
                f"reused by content, so a reissue is re-read and a copy is not.")
    ws["A3"].alignment = LEFT
    ws.merge_cells("A3:H3")

    headers = ["#", "Drawing", "State", "Seconds", "From cache", "Profile",
               "Result file", "Problem"]
    for col, text in enumerate(headers, start=1):
        c = ws.cell(row=5, column=col, value=text)
        c.fill, c.font, c.alignment, c.border = HEADER_FILL, HEADER_FONT, CENTER, BORDER
    for i, job in enumerate(jobs, start=1):
        r = 5 + i
        values = [i, job.drawing_name, job.state, round(job.elapsed_s, 1),
                  "yes" if job.from_cache else "", job.profile,
                  Path(job.json_path).name if job.json_path else "", job.error]
        for col, value in enumerate(values, start=1):
            c = ws.cell(row=r, column=col, value=value)
            c.border = BORDER
            c.alignment = RIGHT if col == 4 else (
                CENTER if col in (1, 3, 5) else LEFT)
        if job.state == "failed":
            ws.cell(row=r, column=3).fill = ASSUMED_FILL
            ws.cell(row=r, column=8).fill = ASSUMED_FILL
    for col, width in {1: 5, 2: 44, 3: 11, 4: 10, 5: 12, 6: 11, 7: 40,
                       8: 60}.items():
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A6"
    wb.save(out_path)
    return out_path


# ----------------------------------------------------------------------
# Location Based Report
# ----------------------------------------------------------------------
# The Summary answers "how much of each item across the set". This sheet
# answers a different question that the tender asks first: how much of it is on
# new ground, how much is inside a live plant, and how much is making good what
# is already there. Those three carry different rates, so a total that blends
# them is the wrong number however accurate the arithmetic.
#
# Every quantity here is a link, never a copy — the same SUMIF into the
# drawing's own activity sheet that the Summary uses. Correct a net quantity on
# `0107_Concrete` and it moves here too. The sub-totals are `=SUM` over the
# rows immediately above them, so inserting a drawing row inside a group is
# picked up without touching the formula.

LOCATION_SHEET = "Location Based Report"
CLASSIFICATION_ORDER = ("Brown Field", "Green Field", "Repair")
UNFILED = "Unclassified"
READ_COMPONENT = "Read from the drawing"

# Components appear in the order a foundation is built, the same order the
# detail tabs already use, with the items the drawing merely specifies last.
# Those carry blank quantities more often than not, and a report that opens on
# forty rows of "area not stated" reads as though nothing was extracted.
_COMPONENT_ORDER = {cat: i for i, cat in enumerate(CATEGORY_TO_ACTIVITY)}


def _component_rank(component: str) -> tuple[int, str]:
    if component == READ_COMPONENT:
        return (len(_COMPONENT_ORDER) + 1, component)
    return (_COMPONENT_ORDER.get(component, len(_COMPONENT_ORDER)), component)


def classification_order(entries: list[DrawingEntry]) -> list[str]:
    """The classifications present, in a fixed order, unknown ones last."""
    seen = {(e.classification or UNFILED) for e in entries}
    known = [c for c in CLASSIFICATION_ORDER if c in seen]
    return known + sorted(seen - set(CLASSIFICATION_ORDER))


def _location_rows(entry: DrawingEntry, code: str) -> list[dict]:
    """Every priced line and every read item on one drawing, as report rows."""
    rows: list[dict] = []
    for line in entry.bom.lines:
        if line.category == "ROLLUP":
            continue
        activity = CATEGORY_TO_ACTIVITY.get(line.category, "")
        sheet = _sheet_name(code, activity) if activity else ""
        rows.append({
            "component": line.category,
            "description": line.item,
            "uom": line.unit,
            "sheet": sheet,
            "desc_col": DETAIL_DESC_COL,
            "value_col": DETAIL_GROSS_COL,
            "first": DETAIL_FIRST_ROW,
            "last": DETAIL_LAST_ROW,
            "fallback": round(line.qty_gross, 3),
            "note": _assumption(entry, line),
        })
    for item in entry.discovered:
        rows.append({
            "component": READ_COMPONENT,
            "description": item.get("description", ""),
            "uom": item.get("uom", ""),
            "sheet": _sheet_name(code, "Drawing items"),
            "desc_col": ITEMS_DESC_COL,
            "value_col": ITEMS_QTY_COL,
            "first": ITEMS_FIRST_ROW,
            "last": ITEMS_LAST_ROW,
            "fallback": item.get("qty"),
            "note": item.get("basis", ""),
        })
    return rows


def _write_location_report(wb: Workbook, entries: list[DrawingEntry],
                           codes: dict[str, str]) -> None:
    """Quantities grouped by site condition, then by component, then by drawing."""
    ws = wb.create_sheet(LOCATION_SHEET, 2)      # behind Summary and Drawings

    ws["A1"] = "LOCATION BASED REPORT"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = ("Grouped by the site classification set against each drawing on "
                "the Extract page, then by component, then by drawing.")
    ws["A2"].alignment = LEFT
    ws.merge_cells("A2:F2")
    ws["A3"] = ("Every quantity is a live link into that drawing's own activity "
                "sheet, and every total is a formula over the rows above it. "
                "Correct a figure on a detail sheet and this report follows.")
    ws["A3"].alignment = LEFT
    ws.merge_cells("A3:F3")

    headers = ["Sl. #", "Drawing No", "Description", "UoM", "Quantity",
               "Notes / assumption"]
    r = 5
    _location_header(ws, r, headers)
    r += 1

    grand: dict[str, list[str]] = {}       # unit -> total cell references

    for classification in classification_order(entries):
        members = [e for e in entries
                   if (e.classification or UNFILED) == classification]
        rows_by_component: "OrderedDict[str, list[tuple[DrawingEntry, dict]]]" = \
            OrderedDict()
        for entry in members:
            for row in _location_rows(entry, codes[entry.drawing_no]):
                rows_by_component.setdefault(row["component"], []).append(
                    (entry, row))
        if not rows_by_component:
            continue
        rows_by_component = OrderedDict(
            sorted(rows_by_component.items(),
                   key=lambda kv: _component_rank(kv[0])))

        # ---- classification band ----
        cell = ws.cell(row=r, column=1, value=classification.upper())
        cell.font = Font(bold=True, size=12, color="FFFFFF")
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).fill = HEADER_FILL
            ws.cell(row=r, column=col).border = BORDER
        ws.cell(row=r, column=2, value=f"{len(members)} drawing(s)").font = Font(
            bold=True, color="FFFFFF", size=10)
        r += 1
        class_first = r

        for component, pairs in rows_by_component.items():
            band = ws.cell(row=r, column=1, value=component)
            band.font = Font(bold=True, color="1F4E78")
            for col in range(1, len(headers) + 1):
                ws.cell(row=r, column=col).fill = GROUP_FILL
                ws.cell(row=r, column=col).border = BORDER
            r += 1
            group_first = r

            for i, (entry, row) in enumerate(pairs, start=1):
                value = (
                    _sumif(row["sheet"], f"$C{r}", desc_col=row["desc_col"],
                           value_col=row["value_col"], first=row["first"],
                           last=row["last"])
                    if row["sheet"] and row["sheet"] in wb.sheetnames
                    else row["fallback"])
                values = [i, entry.drawing_no, row["description"], row["uom"],
                          value, row["note"]]
                for col, v in enumerate(values, start=1):
                    c = ws.cell(row=r, column=col, value=v)
                    c.border = BORDER
                    c.alignment = (RIGHT if col == 5
                                   else CENTER if col in (1, 4) else LEFT)
                    if col == 5:
                        c.number_format = "#,##0.000"
                    if row["note"] and col in (5, 6):
                        c.fill = ASSUMED_FILL
                r += 1

            # ---- one sub-total per unit in this component ----
            # The data rows end here. Captured before the totals are written,
            # because each total advances the cursor and a range that grew with
            # it would start including the totals themselves.
            group_last = r - 1
            units: list[str] = []
            for _, row in pairs:
                if row["uom"] not in units:
                    units.append(row["uom"])
            for unit in units:
                label = f"Total {component} ({classification})"
                if len(units) > 1:
                    label += f" — {unit}"
                lab = ws.cell(row=r, column=3, value=label)
                lab.font = Font(bold=True)
                lab.alignment = LEFT
                ws.cell(row=r, column=4, value=unit).alignment = CENTER
                # SUMIF over the unit column, so a component that mixes m3 and
                # m2 totals each honestly instead of adding them together.
                total = ws.cell(row=r, column=5, value=(
                    f"=SUMIF($D${group_first}:$D${group_last},$D{r},"
                    f"$E${group_first}:$E${group_last})"))
                total.number_format = "#,##0.000"
                total.alignment = RIGHT
                total.fill = TOTAL_FILL
                total.font = Font(bold=True)
                for col in range(1, len(headers) + 1):
                    ws.cell(row=r, column=col).border = BORDER
                grand.setdefault(unit, []).append(f"$E${r}")
                r += 1
            r += 1                                   # air between components

        _ = class_first
        r += 1                                       # air between classifications

    # ---- across every classification ----
    if grand:
        cell = ws.cell(row=r, column=1, value="ALL CLASSIFICATIONS")
        cell.font = Font(bold=True, size=12, color="FFFFFF")
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).fill = HEADER_FILL
            ws.cell(row=r, column=col).border = BORDER
        r += 1
        ws.cell(row=r, column=3, value=(
            "Set totals by unit. These add the component sub-totals above, so "
            "they move with any correction made on a detail sheet.")).alignment = LEFT
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=6)
        r += 1
        for unit, refs in grand.items():
            ws.cell(row=r, column=3, value=f"Set total — {unit}").font = Font(bold=True)
            ws.cell(row=r, column=4, value=unit).alignment = CENTER
            total = ws.cell(row=r, column=5, value="=" + "+".join(refs))
            total.number_format = "#,##0.000"
            total.alignment = RIGHT
            total.fill = TOTAL_FILL
            total.font = Font(bold=True)
            for col in range(1, len(headers) + 1):
                ws.cell(row=r, column=col).border = BORDER
            r += 1

    for col, width in {1: 7, 2: 30, 3: 46, 4: 8, 5: 15, 6: 44}.items():
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A6"


def _location_header(ws, row: int, headers: list[str]) -> None:
    for col, text in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col, value=text)
        c.fill, c.font, c.alignment, c.border = HEADER_FILL, HEADER_FONT, CENTER, BORDER
