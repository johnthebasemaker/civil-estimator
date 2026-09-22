"""Quantities an estimator derives from the drawing rather than reads off it.

A foundation layout prints the slab extent and the pedestal callouts. It does
not print the excavation volume, the blinding area, the liner area or the joint
run — those follow from the slab geometry plus site convention, and an estimator
works them out by hand every time.

This module does that arithmetic explicitly, so it can be shown, argued with and
overridden. Every rule carries:

  * the formula in words, so a checker can see what was assumed;
  * its parameters, editable (working space, blinding offset, panel size…);
  * the elements it produces, as ordinary `core.models` objects.

Nothing here is enabled by default. A derived quantity is an assumption, and an
assumption that reaches a priced BOQ without someone consciously accepting it is
how a tender goes wrong. The project estimate (`ui/workspace/boq_view.py`) shows
each rule with its formula and a checkbox.

Phase 1 modules are untouched; this is additive.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from core.models import (
    CompactedSoil, CurbWall, EpoxyCoating, Excavation, GradeSlab, HDPELiner,
    Joint, PCCBlinding, Project,
)


@dataclass
class DerivedItem:
    """One produced element, with the reasoning that produced it."""
    rule_key: str
    target_field: str          # the Project list it belongs in
    element: object            # a core.models instance
    explanation: str           # "24.43 x 8.60 m slab + 0.30 m working space ..."


@dataclass
class Rule:
    key: str
    label: str
    formula: str               # human-readable, shown in the UI
    params: dict[str, float] = field(default_factory=dict)
    enabled: bool = False
    build: Callable[[Project, dict], list[DerivedItem]] | None = None


def _slabs(project: Project) -> list[GradeSlab]:
    return list(project.grade_slabs)


# ---------- Individual rules ----------
def _excavation(project: Project, p: dict) -> list[DerivedItem]:
    """Slab footprint + working space each side, to formation level."""
    out = []
    ws = p["working_space_m"]
    for s in _slabs(project):
        depth = s.thickness_m + p["blinding_thickness_m"] + p["fill_thickness_m"]
        length, width = s.length_m + 2 * ws, s.width_m + 2 * ws
        out.append(DerivedItem(
            "excavation", "excavations",
            Excavation(tag=f"EXC-{s.tag}", length_m=round(length, 3),
                       width_m=round(width, 3), depth_m=round(depth, 3),
                       quantity=1, soil_type=p.get("soil_type", "ordinary")),
            f"({s.length_m:g} + 2x{ws:g}) x ({s.width_m:g} + 2x{ws:g}) x "
            f"({s.thickness_m:g} slab + {p['blinding_thickness_m']:g} blinding + "
            f"{p['fill_thickness_m']:g} fill) = {length * width * depth:,.2f} m3"))
    return out


def _blinding(project: Project, p: dict) -> list[DerivedItem]:
    """PCC blinding under the slab, projecting past it each side."""
    out = []
    off = p["offset_m"]
    for s in _slabs(project):
        length, width = s.length_m + 2 * off, s.width_m + 2 * off
        out.append(DerivedItem(
            "blinding", "pcc_blindings",
            PCCBlinding(tag=f"PCC-{s.tag}", length_m=round(length, 3),
                        width_m=round(width, 3),
                        thickness_m=p["thickness_m"], quantity=1),
            f"({s.length_m:g} + 2x{off:g}) x ({s.width_m:g} + 2x{off:g}) x "
            f"{p['thickness_m']:g} = {length * width * p['thickness_m']:,.2f} m3"))
    return out


def _fill(project: Project, p: dict) -> list[DerivedItem]:
    """Compacted fill between formation and blinding."""
    out = []
    for s in _slabs(project):
        vol = s.length_m * s.width_m * p["thickness_m"]
        out.append(DerivedItem(
            "fill", "compacted_soils",
            CompactedSoil(tag=f"FILL-{s.tag}", volume_m3=round(vol, 3),
                          description=f"Compacted fill under {s.tag}"),
            f"{s.length_m:g} x {s.width_m:g} x {p['thickness_m']:g} = {vol:,.2f} m3"))
    return out


def _liner(project: Project, p: dict) -> list[DerivedItem]:
    """HDPE liner over the slab footprint (laps come out of wastage %)."""
    out = []
    for s in _slabs(project):
        out.append(DerivedItem(
            "liner", "hdpe_liners",
            HDPELiner(tag=f"HDPE-{s.tag}", length_m=s.length_m, width_m=s.width_m,
                      thickness_mm=p["thickness_mm"]),
            f"{s.length_m:g} x {s.width_m:g} = {s.length_m * s.width_m:,.2f} m2 "
            f"at {p['thickness_mm']:g} mm"))
    return out


def _epoxy(project: Project, p: dict) -> list[DerivedItem]:
    """Acid-resistant coating to the slab top surface."""
    out = []
    for s in _slabs(project):
        area = s.length_m * s.width_m
        out.append(DerivedItem(
            "epoxy", "epoxy_coatings",
            EpoxyCoating(tag=f"EP-{s.tag}", area_m2=round(area, 3),
                         thickness_mm=p["thickness_mm"],
                         coats=int(p["coats"])),
            f"top surface {s.length_m:g} x {s.width_m:g} = {area:,.2f} m2 "
            f"x {int(p['coats'])} coats"))
    return out


def _curb(project: Project, p: dict) -> list[DerivedItem]:
    """Curb wall running round the slab perimeter."""
    out = []
    for s in _slabs(project):
        run = 2 * (s.length_m + s.width_m)
        out.append(DerivedItem(
            "curb", "curb_walls",
            CurbWall(tag=f"CW-{s.tag}", length_m=round(run, 3),
                     height_m=p["height_m"], thickness_m=p["thickness_m"]),
            f"perimeter 2 x ({s.length_m:g} + {s.width_m:g}) = {run:,.2f} m "
            f"at {p['height_m']:g} high x {p['thickness_m']:g} thick"))
    return out


def _joints(project: Project, p: dict) -> list[DerivedItem]:
    """Contraction joints on a panel grid across the slab."""
    out = []
    panel = max(p["panel_size_m"], 0.5)
    for s in _slabs(project):
        n_across = max(math.ceil(s.length_m / panel) - 1, 0)
        n_along = max(math.ceil(s.width_m / panel) - 1, 0)
        total = n_across * s.width_m + n_along * s.length_m
        if total <= 0:
            continue
        out.append(DerivedItem(
            "joints", "joints",
            Joint(tag=f"CJ-{s.tag}", joint_type="contraction",
                  length_m=round(total, 3), has_sealant=True,
                  has_backer_rod=bool(p.get("backer_rod", 0))),
            f"{panel:g} m panels: {n_across} joint(s) x {s.width_m:g} m + "
            f"{n_along} x {s.length_m:g} m = {total:,.2f} m"))
    return out


# ---------- Rule table ----------
def default_rules() -> list[Rule]:
    """Fresh rule set, all disabled. Defaults follow common KSA site practice."""
    return [
        Rule("excavation", "Excavation to formation level",
             "slab footprint + working space each side x (slab + blinding + fill)",
             {"working_space_m": 0.300, "blinding_thickness_m": 0.075,
              "fill_thickness_m": 0.150}, build=_excavation),
        Rule("fill", "Compacted fill / sub-base",
             "slab footprint x fill thickness",
             {"thickness_m": 0.150}, build=_fill),
        Rule("blinding", "PCC blinding",
             "slab footprint + offset each side x blinding thickness",
             {"offset_m": 0.100, "thickness_m": 0.075}, build=_blinding),
        Rule("liner", "HDPE liner",
             "slab footprint (laps covered by wastage %)",
             {"thickness_mm": 1.5}, build=_liner),
        Rule("epoxy", "Acid-resistant epoxy coating",
             "slab top surface x number of coats",
             {"thickness_mm": 4.0, "coats": 2}, build=_epoxy),
        Rule("curb", "Curb wall to slab perimeter",
             "2 x (L + W) at the callout's height and thickness",
             {"height_m": 0.150, "thickness_m": 0.150}, build=_curb),
        Rule("joints", "Contraction joints",
             "panel grid across the slab; joint runs summed",
             {"panel_size_m": 5.0, "backer_rod": 0}, build=_joints),
    ]


def rules_from_findings(rules: list[Rule], findings: dict | None) -> list[Rule]:
    """Seed rule parameters from things the extractor actually read.

    A curb wall callout of "150THK x 150HIGH" should set the curb rule's
    defaults; an epoxy note of "4MM THK" should set the coating thickness. The
    rule stays disabled — this only makes its defaults match the drawing rather
    than a generic assumption.
    """
    if not findings:
        return rules
    by_key = {r.key: r for r in rules}

    curbs = findings.get("curb_walls") or []
    if curbs and "curb" in by_key:
        c = curbs[0]
        if c.get("thickness_mm"):
            by_key["curb"].params["thickness_m"] = round(c["thickness_mm"] / 1000, 3)
        if c.get("height_mm"):
            by_key["curb"].params["height_m"] = round(c["height_mm"] / 1000, 3)

    ep = findings.get("epoxy") or []
    if ep and "epoxy" in by_key and ep[0].get("thickness_mm"):
        by_key["epoxy"].params["thickness_mm"] = float(ep[0]["thickness_mm"])

    for t in findings.get("thicknesses") or []:
        if t.get("kind") == "blinding" and "blinding" in by_key:
            by_key["blinding"].params["thickness_m"] = round(
                t["thickness_mm"] / 1000, 3)
            if "excavation" in by_key:
                by_key["excavation"].params["blinding_thickness_m"] = round(
                    t["thickness_mm"] / 1000, 3)
            break
    return rules


# ---------- Application ----------
def derive(project: Project, rules: list[Rule]) -> list[DerivedItem]:
    """Run the enabled rules. Produces items; does not modify the project."""
    out: list[DerivedItem] = []
    for rule in rules:
        if not rule.enabled or rule.build is None:
            continue
        try:
            out.extend(rule.build(project, rule.params))
        except Exception:                       # noqa: BLE001 — a bad param
            continue                            # must not kill the whole run
    return out


def apply_derived(project: Project, items: list[DerivedItem]) -> list[str]:
    """Add derived elements to the project, skipping tags already present.

    Non-destructive in the same sense as the extraction merge: anything already
    entered by hand wins.
    """
    added: list[str] = []
    for item in items:
        target = getattr(project, item.target_field, None)
        if target is None:
            continue
        tag = getattr(item.element, "tag", "")
        if any(getattr(existing, "tag", "") == tag for existing in target):
            continue
        target.append(item.element)
        added.append(f"{item.target_field}: {tag}")
    return added
