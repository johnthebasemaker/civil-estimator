"""Pydantic models for all BOM element types.

One model per physical element the user enters in the Input page.
Formulas engine consumes these; BOM builder aggregates them.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, PositiveFloat, PositiveInt

ConcreteGrade = Literal["PCC_M15", "RCC_M25", "RCC_M30", "RCC_M40"]
ElementCategory = Literal[
    "pedestal", "grade_slab", "sump", "curb_wall",
    "excavation", "pcc_blinding", "hdpe_liner",
    "epoxy_coating", "joint", "embedment", "waterstop", "drain"
]


# ---------- 1. Excavation ----------
class Excavation(BaseModel):
    tag: str = Field(..., description="e.g. EXC-01")
    length_m: PositiveFloat
    width_m: PositiveFloat
    depth_m: PositiveFloat
    quantity: PositiveInt = 1
    soil_type: Literal["ordinary", "hard_murrum", "soft_rock", "hard_rock"] = "ordinary"


# ---------- 2. PCC / Blinding ----------
class PCCBlinding(BaseModel):
    tag: str
    length_m: PositiveFloat
    width_m: PositiveFloat
    thickness_m: PositiveFloat = 0.100
    grade: ConcreteGrade = "PCC_M15"
    quantity: PositiveInt = 1


# ---------- 3. Structural Concrete ----------
class Pedestal(BaseModel):
    tag: str = Field(..., description="e.g. P1, P2 ... P7")
    length_m: PositiveFloat
    width_m: PositiveFloat
    height_m: PositiveFloat
    quantity: PositiveInt
    grade: ConcreteGrade = "RCC_M30"
    rebar_coefficient_kg_per_m3: PositiveFloat = 120.0


class GradeSlab(BaseModel):
    tag: str
    length_m: PositiveFloat
    width_m: PositiveFloat
    thickness_m: PositiveFloat
    grade: ConcreteGrade = "RCC_M30"
    rebar_coefficient_kg_per_m3: PositiveFloat = 80.0
    has_top_formwork: bool = False


class Sump(BaseModel):
    tag: str
    outer_length_m: PositiveFloat
    outer_width_m: PositiveFloat
    depth_m: PositiveFloat
    wall_thickness_m: PositiveFloat = 0.200
    base_thickness_m: PositiveFloat = 0.200
    grade: ConcreteGrade = "RCC_M30"
    rebar_coefficient_kg_per_m3: PositiveFloat = 100.0


class CurbWall(BaseModel):
    tag: str
    length_m: PositiveFloat
    height_m: PositiveFloat
    thickness_m: PositiveFloat
    grade: ConcreteGrade = "RCC_M30"
    rebar_coefficient_kg_per_m3: PositiveFloat = 90.0


# ---------- 4. Formwork (derived, but user can add loose items) ----------
class FormworkLoose(BaseModel):
    tag: str
    area_m2: PositiveFloat
    description: str = ""


# ---------- 5. Rebar (manual BBS entries, optional) ----------
class RebarBar(BaseModel):
    tag: str = Field(..., description="Bar mark, e.g. B1")
    parent_element: str = Field(..., description="Tag of parent, e.g. P1")
    diameter_mm: Literal[6, 8, 10, 12, 16, 20, 25, 28, 32, 40]
    cut_length_m: PositiveFloat
    nos_per_element: PositiveInt
    parent_quantity: PositiveInt = 1


# ---------- 6. HDPE Liner ----------
class HDPELiner(BaseModel):
    tag: str
    length_m: PositiveFloat
    width_m: PositiveFloat
    thickness_mm: PositiveFloat = 1.5


# ---------- 7. Compacted Soil / Fill ----------
class CompactedSoil(BaseModel):
    tag: str
    volume_m3: PositiveFloat
    description: str = ""


# ---------- 8. Epoxy / Acid-Resistant Coating ----------
class EpoxyCoating(BaseModel):
    tag: str
    area_m2: PositiveFloat
    thickness_mm: PositiveFloat = 3.0
    coats: PositiveInt = 2


# ---------- 9. Joints ----------
class Joint(BaseModel):
    tag: str
    joint_type: Literal["expansion", "contraction", "construction"]
    length_m: PositiveFloat
    has_waterstop: bool = False
    has_sealant: bool = True
    has_backer_rod: bool = False


# ---------- 10. Embedments ----------
class Embedment(BaseModel):
    tag: str
    embedment_type: Literal["insert_plate", "anchor_bolt", "dowel", "sleeve"]
    quantity: PositiveInt
    size_description: str = Field(..., description="e.g. 200x200x10 THK, M20x300")
    unit_weight_kg: Optional[PositiveFloat] = None


# ---------- 11. Waterstop / Sealant runs (standalone) ----------
class WaterstopRun(BaseModel):
    tag: str
    length_m: PositiveFloat
    material: Literal["PVC", "hydrophilic", "bentonite"] = "PVC"
    width_mm: PositiveFloat = 200


# ---------- 12. Sump ancillaries: gratings, drain pipes ----------
class SumpAncillary(BaseModel):
    tag: str
    item_type: Literal["grating", "drain_pipe", "cover"]
    quantity: PositiveInt = 1
    size_description: str = ""
    length_m: Optional[PositiveFloat] = None  # for pipes


# ---------- Project container ----------
class Project(BaseModel):
    project_name: str
    drawing_no: str
    revision: str = ""
    prepared_by: str = ""
    date: str = ""

    excavations: list[Excavation] = []
    pcc_blindings: list[PCCBlinding] = []
    pedestals: list[Pedestal] = []
    grade_slabs: list[GradeSlab] = []
    sumps: list[Sump] = []
    curb_walls: list[CurbWall] = []
    formwork_loose: list[FormworkLoose] = []
    rebar_bars: list[RebarBar] = []
    hdpe_liners: list[HDPELiner] = []
    compacted_soils: list[CompactedSoil] = []
    epoxy_coatings: list[EpoxyCoating] = []
    joints: list[Joint] = []
    embedments: list[Embedment] = []
    waterstop_runs: list[WaterstopRun] = []
    sump_ancillaries: list[SumpAncillary] = []

    # Editable-per-run wastage overrides (start = defaults from json)
    wastage_concrete_pct: float = 3.0
    wastage_rebar_pct: float = 5.0
    wastage_formwork_pct: float = 10.0
    wastage_hdpe_pct: float = 8.0
    wastage_epoxy_pct: float = 15.0
    wastage_pcc_pct: float = 5.0
    wastage_soil_pct: float = 10.0
        # ---------- Metadata added in Step 5 ----------
    pdf_source_filename: str = ""       # original uploaded filename
    pdf_source_path: str = ""           # local saved path (output/uploads/...)
    created_at: str = ""                # ISO timestamp, stamped at BOM generation