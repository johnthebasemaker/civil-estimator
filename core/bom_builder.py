"""BOM aggregator.

Walks a Project, calls formula functions per element, and produces:
- A flat list of BOMLine rows (for the Summary sheet)
- Per-category breakdowns (for per-element sheets)
- A rebar BBS with two sections: coefficient-derived lines and manual bar lines

Wastage is applied here, not in formulas. Formulas return raw quantities.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
from collections import defaultdict
from core.models import Project
from core import formulas as F


Unit = Literal["m3", "m2", "m", "kg", "Nos"]


@dataclass
class BOMLine:
    """One line item in the flat Summary sheet."""
    category: str                # e.g. "Structural Concrete"
    item: str                    # e.g. "RCC M30 - Pedestal P1"
    unit: Unit
    qty_net: float               # before wastage
    wastage_pct: float
    qty_gross: float             # after wastage
    source_tag: str              # element tag(s), e.g. "P1"
    notes: str = ""


@dataclass
class RebarBBSLine:
    """One row on the Rebar_BBS sheet."""
    parent_element: str          # e.g. "P1" or "Grade Slab GS-01"
    bar_mark: str                # "B1", or "COEFF" for coefficient path
    diameter_mm: int | str       # int for manual, "-" for coefficient
    cut_length_m: float | str
    nos: int | str
    total_length_m: float | str
    weight_kg: float             # net weight before wastage


@dataclass
class BOM:
    """Full BOM output — consumed by excel_writer."""
    lines: list[BOMLine] = field(default_factory=list)
    rebar_bbs: list[RebarBBSLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Per-category breakdowns (element -> list of dicts) for detail sheets
    pedestals_detail: list[dict] = field(default_factory=list)
    slabs_detail: list[dict] = field(default_factory=list)
    sumps_detail: list[dict] = field(default_factory=list)
    joints_detail: list[dict] = field(default_factory=list)
    coating_detail: list[dict] = field(default_factory=list)
    embedments_detail: list[dict] = field(default_factory=list)


# ---------- Public entry point ----------
def build_bom(project: Project) -> BOM:
    bom = BOM()
    _validate_rebar_paths(project, bom)

    _add_excavations(project, bom)
    _add_pcc(project, bom)
    _add_pedestals(project, bom)
    _add_grade_slabs(project, bom)
    _add_sumps(project, bom)
    _add_curb_walls(project, bom)
    _add_loose_formwork(project, bom)
    _add_manual_rebar(project, bom)
    _add_hdpe(project, bom)
    _add_compacted_soil(project, bom)
    _add_epoxy(project, bom)
    _add_joints(project, bom)
    _add_embedments(project, bom)
    _add_waterstop(project, bom)
    _add_sump_ancillaries(project, bom)

    _rollup_concrete_by_grade(bom)
    _rollup_rebar_by_diameter(bom)
    return bom


# ---------- Validation ----------
def _validate_rebar_paths(project: Project, bom: BOM) -> None:
    """Warn if an element has both a coefficient AND manual bars — user should
    pick one path per element to avoid double-counting."""
    parents_with_manual = {b.parent_element for b in project.rebar_bars}
    coefficient_tags = set()
    for p in project.pedestals:
        if p.rebar_coefficient_kg_per_m3 > 0:
            coefficient_tags.add(p.tag)
    for s in project.grade_slabs:
        if s.rebar_coefficient_kg_per_m3 > 0:
            coefficient_tags.add(s.tag)
    for s in project.sumps:
        if s.rebar_coefficient_kg_per_m3 > 0:
            coefficient_tags.add(s.tag)
    for c in project.curb_walls:
        if c.rebar_coefficient_kg_per_m3 > 0:
            coefficient_tags.add(c.tag)
    overlap = parents_with_manual & coefficient_tags
    for tag in sorted(overlap):
        bom.warnings.append(
            f"Element '{tag}' has both a rebar coefficient AND manual bars — "
            f"weights will be summed. Set coefficient to 0 to use manual only."
        )


# ---------- Line builders ----------
def _line(bom: BOM, *, category: str, item: str, unit: Unit,
          qty_net: float, wastage_pct: float, source_tag: str, notes: str = "") -> None:
    qty_gross = F.apply_wastage(qty_net, wastage_pct)
    bom.lines.append(BOMLine(
        category=category, item=item, unit=unit,
        qty_net=round(qty_net, 3), wastage_pct=wastage_pct,
        qty_gross=round(qty_gross, 3), source_tag=source_tag, notes=notes,
    ))


def _add_excavations(p: Project, bom: BOM) -> None:
    for e in p.excavations:
        vol = F.excavation_volume(e)
        _line(bom, category="Earthwork", item=f"Excavation ({e.soil_type})",
              unit="m3", qty_net=vol, wastage_pct=0.0, source_tag=e.tag)


def _add_pcc(p: Project, bom: BOM) -> None:
    for pcc in p.pcc_blindings:
        vol = F.pcc_volume(pcc)
        _line(bom, category="PCC / Blinding", item=f"{pcc.grade} blinding",
              unit="m3", qty_net=vol, wastage_pct=p.wastage_pcc_pct, source_tag=pcc.tag)


def _add_pedestals(p: Project, bom: BOM) -> None:
    for ped in p.pedestals:
        vol = F.pedestal_concrete_volume(ped)
        fw = F.pedestal_formwork_area(ped)
        rebar_kg = F.rebar_weight_by_coefficient(vol, ped.rebar_coefficient_kg_per_m3)

        _line(bom, category="Structural Concrete",
              item=f"{ped.grade} - Pedestal {ped.tag}", unit="m3",
              qty_net=vol, wastage_pct=p.wastage_concrete_pct, source_tag=ped.tag,
              notes=f"{ped.quantity} Nos, {ped.length_m}x{ped.width_m}x{ped.height_m}")
        _line(bom, category="Formwork", item=f"Pedestal {ped.tag}",
              unit="m2", qty_net=fw, wastage_pct=p.wastage_formwork_pct, source_tag=ped.tag)
        if rebar_kg > 0:
            _line(bom, category="Rebar (coefficient)",
                  item=f"Rebar for {ped.tag} @ {ped.rebar_coefficient_kg_per_m3} kg/m³",
                  unit="kg", qty_net=rebar_kg, wastage_pct=p.wastage_rebar_pct,
                  source_tag=ped.tag)
            bom.rebar_bbs.append(RebarBBSLine(
                parent_element=f"Pedestal {ped.tag}", bar_mark="COEFF",
                diameter_mm="-", cut_length_m="-", nos="-", total_length_m="-",
                weight_kg=round(rebar_kg, 2),
            ))

        bom.pedestals_detail.append({
            "tag": ped.tag, "L": ped.length_m, "W": ped.width_m, "H": ped.height_m,
            "Nos": ped.quantity, "Grade": ped.grade,
            "Concrete_m3": round(vol, 3), "Formwork_m2": round(fw, 3),
            "Rebar_kg_coeff": round(rebar_kg, 2),
        })


def _add_grade_slabs(p: Project, bom: BOM) -> None:
    for s in p.grade_slabs:
        vol = F.grade_slab_concrete_volume(s)
        fw = F.grade_slab_formwork_area(s)
        rebar_kg = F.rebar_weight_by_coefficient(vol, s.rebar_coefficient_kg_per_m3)

        _line(bom, category="Structural Concrete",
              item=f"{s.grade} - Grade Slab {s.tag}", unit="m3",
              qty_net=vol, wastage_pct=p.wastage_concrete_pct, source_tag=s.tag,
              notes=f"{s.length_m}x{s.width_m}x{s.thickness_m}")
        _line(bom, category="Formwork", item=f"Grade Slab {s.tag} (edges{' + top' if s.has_top_formwork else ''})",
              unit="m2", qty_net=fw, wastage_pct=p.wastage_formwork_pct, source_tag=s.tag)
        if rebar_kg > 0:
            _line(bom, category="Rebar (coefficient)",
                  item=f"Rebar for {s.tag} @ {s.rebar_coefficient_kg_per_m3} kg/m³",
                  unit="kg", qty_net=rebar_kg, wastage_pct=p.wastage_rebar_pct,
                  source_tag=s.tag)
            bom.rebar_bbs.append(RebarBBSLine(
                parent_element=f"Grade Slab {s.tag}", bar_mark="COEFF",
                diameter_mm="-", cut_length_m="-", nos="-", total_length_m="-",
                weight_kg=round(rebar_kg, 2),
            ))

        bom.slabs_detail.append({
            "tag": s.tag, "L": s.length_m, "W": s.width_m, "Thk": s.thickness_m,
            "Grade": s.grade, "Concrete_m3": round(vol, 3),
            "Formwork_m2": round(fw, 3), "Rebar_kg_coeff": round(rebar_kg, 2),
        })


def _add_sumps(p: Project, bom: BOM) -> None:
    for s in p.sumps:
        vol = F.sump_concrete_volume(s)
        fw = F.sump_formwork_area(s)
        rebar_kg = F.rebar_weight_by_coefficient(vol, s.rebar_coefficient_kg_per_m3)

        _line(bom, category="Structural Concrete",
              item=f"{s.grade} - Sump {s.tag}", unit="m3",
              qty_net=vol, wastage_pct=p.wastage_concrete_pct, source_tag=s.tag,
              notes=f"Outer {s.outer_length_m}x{s.outer_width_m}, depth {s.depth_m}, wall {s.wall_thickness_m}")
        _line(bom, category="Formwork", item=f"Sump {s.tag}",
              unit="m2", qty_net=fw, wastage_pct=p.wastage_formwork_pct, source_tag=s.tag)
        if rebar_kg > 0:
            _line(bom, category="Rebar (coefficient)",
                  item=f"Rebar for {s.tag} @ {s.rebar_coefficient_kg_per_m3} kg/m³",
                  unit="kg", qty_net=rebar_kg, wastage_pct=p.wastage_rebar_pct,
                  source_tag=s.tag)
            bom.rebar_bbs.append(RebarBBSLine(
                parent_element=f"Sump {s.tag}", bar_mark="COEFF",
                diameter_mm="-", cut_length_m="-", nos="-", total_length_m="-",
                weight_kg=round(rebar_kg, 2),
            ))

        bom.sumps_detail.append({
            "tag": s.tag, "Outer_L": s.outer_length_m, "Outer_W": s.outer_width_m,
            "Depth": s.depth_m, "Wall_Thk": s.wall_thickness_m,
            "Base_Thk": s.base_thickness_m, "Grade": s.grade,
            "Concrete_m3": round(vol, 3), "Formwork_m2": round(fw, 3),
            "Rebar_kg_coeff": round(rebar_kg, 2),
        })


def _add_curb_walls(p: Project, bom: BOM) -> None:
    for c in p.curb_walls:
        vol = F.curb_wall_concrete_volume(c)
        fw = F.curb_wall_formwork_area(c)
        rebar_kg = F.rebar_weight_by_coefficient(vol, c.rebar_coefficient_kg_per_m3)

        _line(bom, category="Structural Concrete",
              item=f"{c.grade} - Curb Wall {c.tag}", unit="m3",
              qty_net=vol, wastage_pct=p.wastage_concrete_pct, source_tag=c.tag,
              notes=f"{c.length_m}L x {c.height_m}H x {c.thickness_m}T")
        _line(bom, category="Formwork", item=f"Curb Wall {c.tag}",
              unit="m2", qty_net=fw, wastage_pct=p.wastage_formwork_pct, source_tag=c.tag)
        if rebar_kg > 0:
            _line(bom, category="Rebar (coefficient)",
                  item=f"Rebar for {c.tag} @ {c.rebar_coefficient_kg_per_m3} kg/m³",
                  unit="kg", qty_net=rebar_kg, wastage_pct=p.wastage_rebar_pct,
                  source_tag=c.tag)
            bom.rebar_bbs.append(RebarBBSLine(
                parent_element=f"Curb Wall {c.tag}", bar_mark="COEFF",
                diameter_mm="-", cut_length_m="-", nos="-", total_length_m="-",
                weight_kg=round(rebar_kg, 2),
            ))


def _add_loose_formwork(p: Project, bom: BOM) -> None:
    for f in p.formwork_loose:
        _line(bom, category="Formwork", item=f"Loose formwork: {f.description or f.tag}",
              unit="m2", qty_net=f.area_m2, wastage_pct=p.wastage_formwork_pct,
              source_tag=f.tag)


def _add_manual_rebar(p: Project, bom: BOM) -> None:
    for bar in p.rebar_bars:
        wt = F.rebar_bar_weight(bar)
        total_len = bar.cut_length_m * bar.nos_per_element * bar.parent_quantity
        _line(bom, category="Rebar (manual BBS)",
              item=f"{bar.diameter_mm}mm bar {bar.tag} for {bar.parent_element}",
              unit="kg", qty_net=wt, wastage_pct=p.wastage_rebar_pct,
              source_tag=bar.tag,
              notes=f"{bar.nos_per_element} nos/elem × {bar.parent_quantity} elems × {bar.cut_length_m}m")
        bom.rebar_bbs.append(RebarBBSLine(
            parent_element=bar.parent_element, bar_mark=bar.tag,
            diameter_mm=bar.diameter_mm, cut_length_m=bar.cut_length_m,
            nos=bar.nos_per_element * bar.parent_quantity,
            total_length_m=round(total_len, 2), weight_kg=round(wt, 2),
        ))


def _add_hdpe(p: Project, bom: BOM) -> None:
    for h in p.hdpe_liners:
        area = F.hdpe_liner_area(h)
        _line(bom, category="HDPE Liner",
              item=f"HDPE liner {h.thickness_mm}mm", unit="m2",
              qty_net=area, wastage_pct=p.wastage_hdpe_pct, source_tag=h.tag)


def _add_compacted_soil(p: Project, bom: BOM) -> None:
    for s in p.compacted_soils:
        _line(bom, category="Earthwork",
              item=f"Compacted fill: {s.description or s.tag}", unit="m3",
              qty_net=s.volume_m3, wastage_pct=p.wastage_soil_pct, source_tag=s.tag)


def _add_epoxy(p: Project, bom: BOM) -> None:
    for e in p.epoxy_coatings:
        area = F.epoxy_coating_area(e)
        _line(bom, category="Epoxy Coating",
              item=f"Acid-resistant epoxy {e.thickness_mm}mm × {e.coats} coats",
              unit="m2", qty_net=area, wastage_pct=p.wastage_epoxy_pct,
              source_tag=e.tag)
        bom.coating_detail.append({
            "tag": e.tag, "Area_single_coat_m2": e.area_m2,
            "Thickness_mm": e.thickness_mm, "Coats": e.coats,
            "Total_area_m2": round(area, 3),
        })


def _add_joints(p: Project, bom: BOM) -> None:
    for j in p.joints:
        _line(bom, category="Joints", item=f"{j.joint_type.capitalize()} joint",
              unit="m", qty_net=j.length_m, wastage_pct=0.0, source_tag=j.tag)
        if j.has_waterstop:
            _line(bom, category="Joint accessories",
                  item=f"Waterstop for {j.tag}", unit="m",
                  qty_net=j.length_m, wastage_pct=0.0, source_tag=j.tag)
        if j.has_sealant:
            _line(bom, category="Joint accessories",
                  item=f"Sealant for {j.tag}", unit="m",
                  qty_net=j.length_m, wastage_pct=0.0, source_tag=j.tag)
        if j.has_backer_rod:
            _line(bom, category="Joint accessories",
                  item=f"Backer rod for {j.tag}", unit="m",
                  qty_net=j.length_m, wastage_pct=0.0, source_tag=j.tag)
        bom.joints_detail.append({
            "tag": j.tag, "Type": j.joint_type, "Length_m": j.length_m,
            "Waterstop": j.has_waterstop, "Sealant": j.has_sealant,
            "Backer_rod": j.has_backer_rod,
        })


def _add_embedments(p: Project, bom: BOM) -> None:
    for e in p.embedments:
        _line(bom, category="Embedments",
              item=f"{e.embedment_type.replace('_', ' ').title()}: {e.size_description}",
              unit="Nos", qty_net=e.quantity, wastage_pct=0.0, source_tag=e.tag)
        wt = F.embedment_total_weight(e)
        if wt is not None:
            _line(bom, category="Embedments",
                  item=f"Weight — {e.embedment_type.replace('_', ' ').title()} {e.tag}",
                  unit="kg", qty_net=wt, wastage_pct=0.0, source_tag=e.tag)
        bom.embedments_detail.append({
            "tag": e.tag, "Type": e.embedment_type, "Size": e.size_description,
            "Qty_Nos": e.quantity,
            "Unit_wt_kg": e.unit_weight_kg if e.unit_weight_kg else "-",
            "Total_wt_kg": round(wt, 2) if wt else "-",
        })


def _add_waterstop(p: Project, bom: BOM) -> None:
    for w in p.waterstop_runs:
        _line(bom, category="Joint accessories",
              item=f"Waterstop {w.material} {w.width_mm}mm (standalone)",
              unit="m", qty_net=w.length_m, wastage_pct=0.0, source_tag=w.tag)


def _add_sump_ancillaries(p: Project, bom: BOM) -> None:
    for a in p.sump_ancillaries:
        if a.item_type == "drain_pipe" and a.length_m:
            _line(bom, category="Sump Ancillaries",
                  item=f"Drain pipe: {a.size_description}", unit="m",
                  qty_net=a.length_m * a.quantity, wastage_pct=0.0, source_tag=a.tag)
        else:
            _line(bom, category="Sump Ancillaries",
                  item=f"{a.item_type.replace('_', ' ').title()}: {a.size_description}",
                  unit="Nos", qty_net=a.quantity, wastage_pct=0.0, source_tag=a.tag)


# ---------- Rollups (append summary lines at end) ----------
def _rollup_concrete_by_grade(bom: BOM) -> None:
    totals: dict[str, float] = defaultdict(float)
    for line in bom.lines:
        if line.category == "Structural Concrete" or line.category == "PCC / Blinding":
            grade = line.item.split(" - ")[0] if " - " in line.item else line.item.split(" ")[0]
            totals[grade] += line.qty_gross
    for grade, qty in sorted(totals.items()):
        bom.lines.append(BOMLine(
            category="ROLLUP", item=f"Total concrete — {grade}",
            unit="m3", qty_net=qty, wastage_pct=0.0,
            qty_gross=round(qty, 3), source_tag="rollup",
            notes="gross (wastage already included in source lines)",
        ))


def _rollup_rebar_by_diameter(bom: BOM) -> None:
    totals: dict[int, float] = defaultdict(float)
    for r in bom.rebar_bbs:
        if isinstance(r.diameter_mm, int):
            totals[r.diameter_mm] += r.weight_kg
    coefficient_total = sum(r.weight_kg for r in bom.rebar_bbs if r.diameter_mm == "-")
    for dia, wt in sorted(totals.items()):
        bom.lines.append(BOMLine(
            category="ROLLUP", item=f"Total rebar — {dia}mm",
            unit="kg", qty_net=wt, wastage_pct=0.0,
            qty_gross=round(wt, 2), source_tag="rollup",
            notes="from manual BBS entries only (net, before wastage)",
        ))
    if coefficient_total > 0:
        bom.lines.append(BOMLine(
            category="ROLLUP", item="Total rebar — coefficient-based (unclassified dia)",
            unit="kg", qty_net=coefficient_total, wastage_pct=0.0,
            qty_gross=round(coefficient_total, 2), source_tag="rollup",
            notes="from coefficient method, diameter not tracked",
        ))