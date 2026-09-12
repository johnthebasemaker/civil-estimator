"""Open-vocabulary discovery.

The point of `extractors.discovery` is that it does not know what a pedestal
is. These tests therefore lean hard on items nobody enumerated anywhere in the
codebase — bitumen screed, a manhole, a kerb — because a grammar that only
works on the vocabulary its author had in mind is the thing this module exists
to replace.
"""
from __future__ import annotations

import fitz
import pytest
from pathlib import Path

from extractors import discovery as D
from extractors import text_layer as TL

SHEET = "Drawings/MD-522-8110-EG-CV-LAD-0101_C01.pdf"


def one(line: str, **kw) -> D.DiscoveredItem:
    items = D.scan_line(line, **kw)
    assert items, f"nothing discovered in {line!r}"
    return items[0]


# ---------------------------------------------------------------- vocabulary
class TestOpenVocabulary:
    """Items the codebase has never heard of must still come out."""

    @pytest.mark.parametrize("line,expect_in_description", [
        ("40 THK BITUMEN SCREED", "Bitumen Screed"),
        ("2MM THK POLYUREA MEMBRANE", "Polyurea Membrane"),
        ("150 THK GEOTEXTILE SEPARATOR", "Geotextile Separator"),
        ("25mm DIA WEEP HOLES", "Weep Holes"),
        ("90% COMPACTED CRUSHED AGGREGATE", "Crushed Aggregate"),
    ])
    def test_names_come_from_the_drawing(self, line, expect_in_description):
        assert expect_in_description in one(line).description

    def test_an_element_type_nobody_listed_still_gets_a_volume(self):
        item = one("MANHOLE 1200x1200x1800")
        assert item.kind == "box3"
        assert item.uom == "m3"
        assert item.qty == pytest.approx(1.2 * 1.2 * 1.8, rel=1e-6)
        assert "Manhole" in item.description

    def test_no_element_name_is_hard_coded_in_the_module(self):
        """A regression guard on the whole design.

        Docstrings and comments name pedestals freely — they explain why the
        module exists. What must stay clean is the code: no pattern, set or
        lookup table may mention an element type, because the moment one does,
        the next drawing that omits it is back to producing nothing.
        """
        import ast

        tree = ast.parse(Path(D.__file__).read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        literals = [n.value.upper() for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value not in docstrings]
        for banned in ("PEDESTAL", "GRADE SLAB", "SUMP", "TRENCH", "PLINTH",
                       "CURB", "KERB", "MANHOLE"):
            offenders = [s for s in literals if banned in s]
            assert not offenders, f"{banned} is enumerated in discovery.py: {offenders}"


# ------------------------------------------------------------------ grammars
class TestGrammars:
    def test_thickness_leading(self):
        item = one("30 THK GROUT")
        assert item.spec["thickness_mm"] == 30
        assert "30 mm thick" in item.description

    def test_thickness_trailing(self):
        item = one("IP 20mm THK")
        assert item.spec["thickness_mm"] == 20
        assert item.description.startswith("IP")

    def test_bar_count_is_per_element_not_a_total(self):
        item = one("16-D25")
        assert item.kind == "rebar"
        assert item.qty == 16
        assert "per element" in item.description

    def test_bar_spacing_has_no_quantity_without_a_run_length(self):
        item = one("D10 @ 150 LINK")
        assert item.qty is None
        assert "run length" in item.qty_basis

    def test_anchor_assembly_is_parsed_into_its_parts(self):
        item = one("AR-(4)-B-M36-1345-N2")
        assert item.qty == 4
        assert item.spec["size_mm"] == 36
        assert item.spec["length_mm"] == 1345

    def test_per_set_count(self):
        items = D.scan_line("D10-100 LINKS (2 Nos./SET)")
        per_set = [i for i in items if i.kind == "set"]
        assert per_set and per_set[0].qty == 2

    def test_two_dimensions_give_an_area_and_three_give_a_volume(self):
        assert one("KERB 300x450").uom == "m2"
        assert one("KERB 300x450x900").uom == "m3"


# ------------------------------------------------------------------- honesty
class TestNothingIsInvented:
    def test_a_thickness_alone_is_not_a_quantity(self):
        item = one("4MM THK ACID RESISTANT EPOXY COATING")
        assert item.qty is None
        assert item.confirm
        assert "area not stated" in item.qty_basis

    def test_a_diameter_alone_is_not_a_count(self):
        assert one("20mm DIA LUGS").qty is None

    def test_occurrences_are_never_used_as_a_quantity(self):
        block = {"lines": ["30 THK GROUT"], "grid_ref": "A-1"}
        items = D.discover([block, block, block])
        assert len(items) == 1
        assert items[0].occurrences == 3
        assert items[0].qty is None

    @pytest.mark.parametrize("line", [
        "SECTION A-A",
        "(SCALE: 1:25 mm)",
        "LOOKING NORTH",
        "KEY PLAN",
        "REV",
        "MD-522-8110-EG-CV-LAD-0101",
        "N 3046406.500 FOR 37112-TNK-002",
        "PROPRIETARY & CONFIDENTIAL",
        "DIMENSIONS SHALL BE VERIFED AT SITE BEFORE CONSTRUCTION",
    ])
    def test_drafting_furniture_produces_no_items(self, line):
        assert D.scan_line(line) == []

    def test_a_chained_plan_dimension_is_not_an_item(self):
        """"1200 x 3100" between two pedestals names nothing, so it is nothing."""
        assert D.scan_line("1200 x 3100") == []


# ------------------------------------------------------------------- context
class TestBlockContext:
    def test_a_dimension_is_named_by_the_label_above_it(self):
        item = one("P1(600x500) 2Nos", context=["TYP DETAIL OF PEDESTAL"])
        assert "Pedestal" in item.description

    def test_reinforcement_does_not_borrow_a_neighbour_s_words(self):
        """The fix for descriptions like "Reference Documents — D16 @ 150"."""
        item = one("16-D25", context=["REFERENCE DOCUMENTS"])
        assert "Reference" not in item.description

    def test_a_phrase_that_runs_off_the_end_continues_onto_the_next_label(self):
        item = one("4MM THK ACID RESISTANT",
                   following=["EPOXY COATING (TYP)"])
        assert "Epoxy Coating" in item.description

    def test_a_continuation_that_only_repeats_is_dropped(self):
        item = one("4MM THK ACID RESISTANT", following=["ACID RESISTANT"])
        assert item.description.count("Acid Resistant") == 1


# ----------------------------------------------------------------- specifics
class TestSheetContext:
    def test_a_bolt_thread_is_not_reported_as_a_concrete_grade(self):
        assert D.scan_grades(["AR-(4)-B-M36-1345-N2"]) == []

    def test_a_concrete_grade_is(self):
        assert D.scan_grades(["GRADE C35 CONCRETE"]) == ["C35"]

    def test_strength_notes_are_read_from_the_general_notes(self):
        specs = D.scan_specifications([
            "8. CONCRETE MIX SHALL HAVE MINIMUM CYLINDRICAL COMPRESSIVE "
            "STRENGTH OF 35 MPa FOR CAST IN SITU CONCRETE."])
        assert specs and specs[0]["value"] == 35

    def test_a_height_pairs_a_top_level_with_a_bottom_one(self):
        levels = D.scan_levels(["BOBP EL. 98.380", "BOC EL. 95.500"])
        heights = D.height_candidates(levels)
        assert any(abs(h["height_m"] - 2.880) < 1e-6 for h in heights)

    def test_two_levels_of_the_same_kind_do_not_make_a_height(self):
        levels = D.scan_levels(["BOC EL. 95.500", "BOC EL. 95.850"])
        assert D.height_candidates(levels) == []


# ---------------------------------------------------------------- real sheet
class TestAgainstTheRealDrawing:
    """…-0101 is the sheet that produced nothing before this module existed."""

    @pytest.fixture(scope="class")
    def summary(self):
        page = fitz.open(SHEET)[0]
        assert TL.has_usable_text(page)
        return D.summarise(TL.label_blocks(page))

    def test_it_now_produces_takeoff_lines(self, summary):
        assert len(summary["items"]) >= 10

    def test_it_finds_the_coating_the_preset_vocabulary_missed(self, summary):
        assert any("Acid Resistant" in i["description"] for i in summary["items"])

    def test_it_finds_the_bar_callouts(self, summary):
        assert any(i["description"].startswith("D25 bars")
                   for i in summary["items"])

    def test_every_item_carries_the_text_it_came_from(self, summary):
        assert all(i["source"] for i in summary["items"])

    def test_it_reads_the_concrete_strength_from_the_notes(self, summary):
        assert any(s["value"] == 35 for s in summary["specifications"])

    def test_it_does_not_report_a_neighbour_s_words_in_a_description(self, summary):
        """The bug that made "Acid Resistant" read "THK Acid Resistant"."""
        for item in summary["items"]:
            assert not item["description"].upper().startswith("THK")
