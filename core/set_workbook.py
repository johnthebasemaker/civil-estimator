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
                discovery: dict | None = None) -> DrawingEntry:
    return DrawingEntry(drawing_no=drawing_no or "(no number)", project=project,
                        bom=build_bom(project), source_pdf=source_pdf,
                        derived_tags=set(derived_tags or ()),
                        placeholder_tags=set(placeholder_tags or ()),
                        notes=list(notes or ()),
                        position_marks=dict(position_marks or {}),
                        discovery=dict(discovery or {}))


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
            for i, entry in enumerate(entries):
                qty = data["qty"].get(entry.drawing_no)
                cell = ws.cell(row=r, column=4 + i,
                               value=round(qty, 3) if qty else None)
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

    r, sl = _write_discovered_block(ws, entries, headers, r, sl)

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


def _write_discovered_block(ws, entries: list[DrawingEntry], headers: list[str],
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
            qty = data["qty"].get(entry.drawing_no)
            cell = ws.cell(row=r, column=4 + i, value=qty)
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
                      line.wastage_pct, round(line.qty_gross, 3),
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
        for col, width in {1: 7, 2: 44, 3: 7, 4: 12, 5: 11, 6: 13, 7: 12,
                           8: 40, 9: 34}.items():
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.freeze_panes = "A5"
