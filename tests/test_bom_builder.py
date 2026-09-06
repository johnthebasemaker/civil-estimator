"""Integration tests for bom_builder against a mini project.

Uses P1 pedestal + a grade slab + a sump + a joint to verify:
- Line generation across categories
- Wastage application
- Rebar coefficient warning when manual bars exist for same parent
- Rollup lines
"""
from __future__ import annotations
import pytest
from core.models import (
    Project, Pedestal, GradeSlab, Sump, Joint, RebarBar, Excavation,
    PCCBlinding, EpoxyCoating,
)
from core.bom_builder import build_bom


def _mini_project() -> Project:
    return Project(
        project_name="TEST", drawing_no="TEST-001",
        pedestals=[Pedestal(tag="P1", length_m=0.6, width_m=0.5,
                            height_m=0.8, quantity=2)],
        grade_slabs=[GradeSlab(tag="GS-01", length_m=20, width_m=10,
                               thickness_m=0.25)],
        sumps=[Sump(tag="SP-01", outer_length_m=2.0, outer_width_m=2.0,
                    depth_m=1.5)],
        joints=[Joint(tag="EJ-01", joint_type="expansion", length_m=25.5,
                      has_waterstop=True)],
        excavations=[Excavation(tag="EXC-01", length_m=25, width_m=15, depth_m=1.5)],
        pcc_blindings=[PCCBlinding(tag="PCC-01", length_m=25, width_m=15)],
        epoxy_coatings=[EpoxyCoating(tag="EP-01", area_m2=250, coats=2)],
    )


def test_bom_has_lines():
    bom = build_bom(_mini_project())
    assert len(bom.lines) > 0

def test_pedestal_generates_concrete_formwork_rebar():
    bom = build_bom(_mini_project())
    cats = [l.category for l in bom.lines if l.source_tag == "P1"]
    assert "Structural Concrete" in cats
    assert "Formwork" in cats
    assert "Rebar (coefficient)" in cats

def test_wastage_applied_to_concrete():
    bom = build_bom(_mini_project())
    conc = next(l for l in bom.lines
                if l.source_tag == "P1" and l.category == "Structural Concrete")
    # net 0.48, wastage 3% → 0.4944
    assert conc.qty_net == pytest.approx(0.48, abs=0.001)
    assert conc.qty_gross == pytest.approx(0.4944, abs=0.001)

def test_excavation_zero_wastage():
    bom = build_bom(_mini_project())
    exc = next(l for l in bom.lines if l.category == "Earthwork")
    assert exc.wastage_pct == 0.0
    assert exc.qty_net == exc.qty_gross

def test_joint_with_waterstop_generates_accessory():
    bom = build_bom(_mini_project())
    items = [l.item for l in bom.lines if l.source_tag == "EJ-01"]
    assert any("Expansion joint" in i for i in items)
    assert any("Waterstop" in i for i in items)
    assert any("Sealant" in i for i in items)

def test_epoxy_uses_coats_multiplier():
    bom = build_bom(_mini_project())
    ep = next(l for l in bom.lines if l.source_tag == "EP-01")
    # 250 * 2 coats = 500 net, +15% wastage = 575
    assert ep.qty_net == pytest.approx(500.0)
    assert ep.qty_gross == pytest.approx(575.0)

def test_rebar_bbs_has_coefficient_rows():
    bom = build_bom(_mini_project())
    coeff_rows = [r for r in bom.rebar_bbs if r.bar_mark == "COEFF"]
    assert len(coeff_rows) == 3  # P1, GS-01, SP-01

def test_double_counting_warning():
    proj = _mini_project()
    proj.rebar_bars = [RebarBar(tag="B1", parent_element="P1",
                                diameter_mm=16, cut_length_m=1.5,
                                nos_per_element=8, parent_quantity=2)]
    bom = build_bom(proj)
    assert any("P1" in w and "both" in w for w in bom.warnings)

def test_manual_rebar_bbs_row():
    proj = _mini_project()
    proj.rebar_bars = [RebarBar(tag="B1", parent_element="P1",
                                diameter_mm=16, cut_length_m=1.5,
                                nos_per_element=8, parent_quantity=2)]
    bom = build_bom(proj)
    manual = [r for r in bom.rebar_bbs if r.bar_mark == "B1"]
    assert len(manual) == 1
    assert manual[0].diameter_mm == 16
    assert manual[0].nos == 16  # 8 per elem × 2 elems

def test_rollup_lines_present():
    bom = build_bom(_mini_project())
    rollups = [l for l in bom.lines if l.category == "ROLLUP"]
    assert len(rollups) >= 2

def test_pedestal_detail_captured():
    bom = build_bom(_mini_project())
    assert len(bom.pedestals_detail) == 1
    assert bom.pedestals_detail[0]["tag"] == "P1"
    assert bom.pedestals_detail[0]["Concrete_m3"] == 0.48