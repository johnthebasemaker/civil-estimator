"""Civil calculation engine.

Pure functions — no I/O, no state. Each function takes a model instance
(or primitives) and returns a quantity. BOM builder consumes these.

Conventions:
- All linear inputs in metres, areas in m², volumes in m³, weights in kg.
- Formwork = contact area of concrete with shuttering (bottom of suspended
  slabs excluded; sides + optional top only).
- Rebar coefficient path returns a single weight; manual BBS path is handled
  separately in bom_builder.
"""
from __future__ import annotations
import json
from pathlib import Path
from core.models import (
    Excavation, PCCBlinding, Pedestal, GradeSlab, Sump, CurbWall,
    RebarBar, HDPELiner, EpoxyCoating, Joint, Embedment,
)

# ---------- Constants ----------
_REBAR_WEIGHTS_PATH = Path(__file__).parent.parent / "data" / "rebar_weights.json"
with open(_REBAR_WEIGHTS_PATH) as f:
    REBAR_KG_PER_M: dict[int, float] = {int(k): v for k, v in json.load(f).items()}


# ---------- 1. Excavation ----------
def excavation_volume(e: Excavation) -> float:
    """Bulk excavation volume in m³ (in-situ, no bulking factor)."""
    return e.length_m * e.width_m * e.depth_m * e.quantity


# ---------- 2. PCC / Blinding ----------
def pcc_volume(p: PCCBlinding) -> float:
    return p.length_m * p.width_m * p.thickness_m * p.quantity


# ---------- 3. Structural Concrete Volumes ----------
def pedestal_concrete_volume(p: Pedestal) -> float:
    return p.length_m * p.width_m * p.height_m * p.quantity


def grade_slab_concrete_volume(s: GradeSlab) -> float:
    return s.length_m * s.width_m * s.thickness_m


def sump_concrete_volume(s: Sump) -> float:
    """Base slab + 4 walls (walls sit on top of base, outer dims given)."""
    base_vol = s.outer_length_m * s.outer_width_m * s.base_thickness_m
    wall_height = s.depth_m - s.base_thickness_m
    # Two long walls (full outer length) + two short walls (between long walls)
    long_walls = 2 * s.outer_length_m * s.wall_thickness_m * wall_height
    short_walls = 2 * (s.outer_width_m - 2 * s.wall_thickness_m) * s.wall_thickness_m * wall_height
    return base_vol + long_walls + short_walls


def curb_wall_concrete_volume(c: CurbWall) -> float:
    return c.length_m * c.height_m * c.thickness_m


# ---------- 4. Formwork (contact area) ----------
def pedestal_formwork_area(p: Pedestal) -> float:
    """4 vertical faces per pedestal (top and bottom cast against slab/soil)."""
    perimeter = 2 * (p.length_m + p.width_m)
    return perimeter * p.height_m * p.quantity


def grade_slab_formwork_area(s: GradeSlab) -> float:
    """Edge shuttering only; top and bottom typically not formed for grade slab."""
    perimeter = 2 * (s.length_m + s.width_m)
    edge = perimeter * s.thickness_m
    top = s.length_m * s.width_m if s.has_top_formwork else 0.0
    return edge + top


def sump_formwork_area(s: Sump) -> float:
    """Outer wall faces + inner wall faces + inner base (top of base slab).
    Outer excavation face counted; if cast against soil in reality, user can
    subtract via a FormworkLoose negative entry.
    """
    wall_height = s.depth_m - s.base_thickness_m
    outer_perimeter = 2 * (s.outer_length_m + s.outer_width_m)
    inner_l = s.outer_length_m - 2 * s.wall_thickness_m
    inner_w = s.outer_width_m - 2 * s.wall_thickness_m
    inner_perimeter = 2 * (inner_l + inner_w)
    outer_face = outer_perimeter * wall_height
    inner_face = inner_perimeter * wall_height
    inner_base_top = inner_l * inner_w  # top of base slab visible inside pit
    return outer_face + inner_face + inner_base_top


def curb_wall_formwork_area(c: CurbWall) -> float:
    """Both vertical faces along the length."""
    return 2 * c.length_m * c.height_m


# ---------- 5. Rebar ----------
def rebar_weight_by_coefficient(concrete_vol_m3: float, coefficient_kg_per_m3: float) -> float:
    return concrete_vol_m3 * coefficient_kg_per_m3


def rebar_bar_weight(bar: RebarBar) -> float:
    """Weight for a single RebarBar entry across all parent instances."""
    kg_per_m = REBAR_KG_PER_M[bar.diameter_mm]
    return bar.cut_length_m * bar.nos_per_element * bar.parent_quantity * kg_per_m


# ---------- 6. HDPE Liner ----------
def hdpe_liner_area(h: HDPELiner) -> float:
    return h.length_m * h.width_m


# ---------- 7. Epoxy Coating ----------
def epoxy_coating_area(e: EpoxyCoating) -> float:
    """Total area including coats (2 coats = 2× area for material takeoff)."""
    return e.area_m2 * e.coats


# ---------- 8. Joints ----------
def joint_length(j: Joint) -> float:
    return j.length_m


# ---------- 9. Embedments ----------
def embedment_total_weight(e: Embedment) -> float | None:
    """Returns None when unit weight isn't provided (BOM shows Nos only)."""
    if e.unit_weight_kg is None:
        return None
    return e.unit_weight_kg * e.quantity


# ---------- Wastage helper ----------
def apply_wastage(qty: float, wastage_pct: float) -> float:
    return qty * (1 + wastage_pct / 100.0)