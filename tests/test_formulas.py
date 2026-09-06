"""Unit tests for core.formulas.

Numbers cross-checked against pedestal/slab/sump geometry in the sample
Maaden drawing (MD-522-8110-EG-CV-LAD-0107).
"""
from __future__ import annotations
import pytest
from core.models import (
    Excavation, PCCBlinding, Pedestal, GradeSlab, Sump, CurbWall,
    RebarBar, HDPELiner, EpoxyCoating, Joint, Embedment,
)
from core import formulas as F


# ---------- Excavation ----------
def test_excavation_volume():
    e = Excavation(tag="EXC-01", length_m=10, width_m=5, depth_m=2, quantity=1)
    assert F.excavation_volume(e) == 100.0

def test_excavation_volume_multi():
    e = Excavation(tag="EXC-02", length_m=2, width_m=2, depth_m=1, quantity=3)
    assert F.excavation_volume(e) == 12.0


# ---------- PCC ----------
def test_pcc_volume():
    p = PCCBlinding(tag="PCC-01", length_m=5, width_m=3, thickness_m=0.1, quantity=1)
    assert F.pcc_volume(p) == pytest.approx(1.5)


# ---------- Pedestal (P1: 600x500x800, 2 Nos from drawing) ----------
def test_pedestal_concrete_p1():
    p = Pedestal(tag="P1", length_m=0.6, width_m=0.5, height_m=0.8, quantity=2)
    assert F.pedestal_concrete_volume(p) == pytest.approx(0.48)

def test_pedestal_formwork_p1():
    p = Pedestal(tag="P1", length_m=0.6, width_m=0.5, height_m=0.8, quantity=2)
    # perimeter = 2.2, x height 0.8 = 1.76 per pedestal, x 2 = 3.52
    assert F.pedestal_formwork_area(p) == pytest.approx(3.52)

def test_pedestal_rebar_coefficient():
    p = Pedestal(tag="P1", length_m=0.6, width_m=0.5, height_m=0.8, quantity=2,
                 rebar_coefficient_kg_per_m3=120.0)
    vol = F.pedestal_concrete_volume(p)
    assert F.rebar_weight_by_coefficient(vol, p.rebar_coefficient_kg_per_m3) == pytest.approx(57.6)


# ---------- Grade Slab ----------
def test_grade_slab_volume():
    s = GradeSlab(tag="GS-01", length_m=20, width_m=10, thickness_m=0.25)
    assert F.grade_slab_concrete_volume(s) == pytest.approx(50.0)

def test_grade_slab_formwork_edge_only():
    s = GradeSlab(tag="GS-01", length_m=20, width_m=10, thickness_m=0.25,
                  has_top_formwork=False)
    # perimeter 60 x 0.25 = 15
    assert F.grade_slab_formwork_area(s) == pytest.approx(15.0)

def test_grade_slab_formwork_with_top():
    s = GradeSlab(tag="GS-01", length_m=20, width_m=10, thickness_m=0.25,
                  has_top_formwork=True)
    assert F.grade_slab_formwork_area(s) == pytest.approx(15.0 + 200.0)


# ---------- Sump ----------
def test_sump_volume():
    s = Sump(tag="SP-01", outer_length_m=2.0, outer_width_m=2.0, depth_m=1.5,
             wall_thickness_m=0.2, base_thickness_m=0.2)
    # base = 2*2*0.2 = 0.8
    # wall height = 1.3
    # long walls: 2 * 2.0 * 0.2 * 1.3 = 1.04
    # short walls: 2 * (2.0 - 0.4) * 0.2 * 1.3 = 0.832
    assert F.sump_concrete_volume(s) == pytest.approx(0.8 + 1.04 + 0.832)

def test_sump_formwork_area():
    s = Sump(tag="SP-01", outer_length_m=2.0, outer_width_m=2.0, depth_m=1.5,
             wall_thickness_m=0.2, base_thickness_m=0.2)
    # wall height 1.3
    # outer perim 8, outer face = 8 * 1.3 = 10.4
    # inner perim = 2*(1.6+1.6) = 6.4, inner face = 6.4 * 1.3 = 8.32
    # inner base top = 1.6*1.6 = 2.56
    assert F.sump_formwork_area(s) == pytest.approx(10.4 + 8.32 + 2.56)


# ---------- Curb Wall ----------
def test_curb_wall_volume():
    c = CurbWall(tag="CW-01", length_m=15, height_m=0.3, thickness_m=0.15)
    assert F.curb_wall_concrete_volume(c) == pytest.approx(0.675)

def test_curb_wall_formwork():
    c = CurbWall(tag="CW-01", length_m=15, height_m=0.3, thickness_m=0.15)
    # 2 faces x 15 x 0.3 = 9
    assert F.curb_wall_formwork_area(c) == pytest.approx(9.0)


# ---------- Rebar ----------
def test_rebar_bar_weight_16mm():
    # 16mm bar, 6m cut length, 8 nos per pedestal, 2 pedestals
    # 6 * 8 * 2 * 1.578 = 151.488
    b = RebarBar(tag="B1", parent_element="P1", diameter_mm=16,
                 cut_length_m=6.0, nos_per_element=8, parent_quantity=2)
    assert F.rebar_bar_weight(b) == pytest.approx(151.488)

def test_rebar_bar_weight_10mm_links():
    b = RebarBar(tag="L1", parent_element="P1", diameter_mm=10,
                 cut_length_m=1.8, nos_per_element=12, parent_quantity=2)
    # 1.8 * 12 * 2 * 0.617 = 26.6544
    assert F.rebar_bar_weight(b) == pytest.approx(26.6544)


# ---------- HDPE ----------
def test_hdpe_area():
    h = HDPELiner(tag="HDPE-01", length_m=10, width_m=5)
    assert F.hdpe_liner_area(h) == 50.0


# ---------- Epoxy ----------
def test_epoxy_two_coats():
    e = EpoxyCoating(tag="EP-01", area_m2=100, coats=2)
    assert F.epoxy_coating_area(e) == 200.0


# ---------- Joints ----------
def test_joint_length():
    j = Joint(tag="EJ-01", joint_type="expansion", length_m=25.5)
    assert F.joint_length(j) == 25.5


# ---------- Embedments ----------
def test_embedment_weight_provided():
    e = Embedment(tag="IP-01", embedment_type="insert_plate", quantity=10,
                  size_description="200x200x10", unit_weight_kg=3.14)
    assert F.embedment_total_weight(e) == pytest.approx(31.4)

def test_embedment_weight_none():
    e = Embedment(tag="AB-01", embedment_type="anchor_bolt", quantity=40,
                  size_description="M20x300")
    assert F.embedment_total_weight(e) is None


# ---------- Wastage ----------
def test_apply_wastage_concrete():
    assert F.apply_wastage(100.0, 3.0) == pytest.approx(103.0)

def test_apply_wastage_rebar():
    assert F.apply_wastage(1000.0, 5.0) == pytest.approx(1050.0)

def test_apply_wastage_zero():
    assert F.apply_wastage(50.0, 0.0) == 50.0