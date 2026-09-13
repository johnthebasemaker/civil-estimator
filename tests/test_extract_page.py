"""Execution tests for the Drawing → BOQ page.

Streamlit pages fail at *render* time, not import time: a widget argument that
does not exist in the installed version, or a None slipping into a data editor,
looks fine to a linter and dies when someone opens the tab. That is exactly how
`st.image(use_container_width=...)` shipped broken.

`AppTest` runs the page script in-process, so these are real executions with no
browser and no model.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.models import Project
from extractors.models import (
    ExtractionResult, GradeSlabExtraction, PedestalExtraction,
    RegionResponse, TitleBlockExtraction,
)

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

ROOT = Path(__file__).resolve().parent.parent
from ui.auth import SESSION_KEY as AUTH_KEY

PAGE = str(ROOT / "pages" / "0_Extract.py")
SAMPLE_PDF = ROOT / "MD-522-8110-EG-CV-LAD-0107_C01.pdf"

TRUTH = {"P1": (600, 500, 2), "P2": (500, 500, 20), "P3": (350, 350, 11),
         "P6": (450, 450, 6), "P7": (500, 500, 4)}


def _extraction() -> ExtractionResult:
    r = ExtractionResult(
        source_pdf=str(SAMPLE_PDF), model="qwen2.5vl:7b", profile="thorough",
        image_hash="5110010820424080", total_elapsed_s=250.0,
        montages_sent=4, text_blocks_found=171,
        title_block=TitleBlockExtraction(
            drawing_no="MD-522-8110-EG-CV-LAD-0107", revision="C01",
            project_name="MAADEN PHOSPHATE 3 PHASE 1", date="2025-09-30",
            prepared_by="P.PARDULE", confidence="high"),
        pedestals=[PedestalExtraction(
            tag=t, length_mm=l, width_mm=w, quantity=q, regex_validated=True,
            raw_text=f"TYP DETAIL OF PEDESTAL {t}({l}x{w}) {q}Nos",
            confidence="medium", seen_in_regions=["transcript"])
            for t, (l, w, q) in TRUTH.items()],
        grade_slabs=[GradeSlabExtraction(
            tag="GS-01", length_mm=24430, width_mm=8600, thickness_mm=300,
            toc_level="97.550", confidence="medium")],
        responses=[RegionResponse(region="montage_1", stage="transcribe",
                                  px_width=1200, px_height=631, elapsed_s=44.0)],
        transcribed_lines=["TYP DETAIL OF PEDESTAL", "P1(600x500) 2Nos",
                           "CURB WALL 150THK X 150HIGH", "TOC EL 97.850"],
        findings={"curb_walls": [{"thickness_mm": 150.0, "height_mm": 150.0,
                                  "raw_text": "CURB WALL 150THK X 150HIGH"}],
                  "levels": [{"kind": "TOC", "level_m": 97.85,
                              "raw_text": "TOC EL 97.850"}],
                  "sumps": [], "insert_plates": [], "epoxy": [], "rebar": [],
                  "thicknesses": []},
        confidence_notes=["located 171 text blocks from vector geometry"],
    )
    return r


def _page_with_drawing(extraction=None, project=None):
    """Drive the page the way a person does: pick a drawing, then extract.

    Order matters. The page clears any stale extraction when the selected
    drawing changes, so seeding `extraction` before the drawing is chosen would
    be wiped — exactly as it should be.
    """
    at = AppTest.from_file(PAGE, default_timeout=300)
    at.session_state[AUTH_KEY] = True          # the page is behind a login gate
    if project is not None:
        at.session_state["project"] = project
    at.run()

    # Tick the sample drawing. The label carries a location suffix when the same
    # filename exists in both output/uploads and the project root, so match on
    # the prefix rather than the bare name.
    box = next((c for c in at.checkbox
                if c.label.startswith(SAMPLE_PDF.name)), None)
    if box is not None:
        box.set_value(True).run()

    if extraction is not None:
        at.session_state["extraction"] = extraction
        at.run()
    return at


@pytest.fixture(autouse=True)
def _sandboxed_queue(tmp_path, monkeypatch):
    """A queue of this test's own.

    Without this the page renders against whatever is in the developer's real
    `data/jobs.db`, so the same test passes or fails depending on what someone
    extracted this morning.
    """
    monkeypatch.setenv("CIVIL_ESTIMATOR_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("CIVIL_ESTIMATOR_CACHE_DIR", str(tmp_path / "cache"))


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample drawing not in repo root")
class TestExtractPageRenders:
    def test_stops_cleanly_with_no_drawing_ticked(self):
        at = AppTest.from_file(PAGE, default_timeout=120)
        at.session_state[AUTH_KEY] = True
        at.run()
        assert not at.exception
        assert any("Tick a drawing" in i.value or "Upload a drawing" in i.value
                   for i in at.info)

    def test_page_is_behind_the_login_gate(self):
        """Every page carries its own gate: Streamlit runs each as a separate
        script, so a gate on the home page alone would be bypassed by
        navigating straight here."""
        at = AppTest.from_file(PAGE, default_timeout=120).run()
        assert not at.exception
        assert not any("1 · Drawings" in h.value for h in at.header)

    def test_renders_sheet_metrics_without_calling_the_model(self):
        at = _page_with_drawing()
        assert not at.exception
        labels = {m.label: m.value for m in at.metric}
        assert labels["Sheet"] == "A0"
        assert int(labels["Text blocks found"]) > 0
        assert int(labels["Model calls needed"]) < 10

    def test_review_and_generate_render_once_extraction_exists(self):
        at = _page_with_drawing(_extraction(), Project(project_name="", drawing_no=""))
        assert not at.exception
        headers = [h.value for h in at.header]
        assert "4 · Review and correct" in headers
        assert "5 · Generate BOQ" in headers
        assert any("Generate BOQ Excel" in b.label for b in at.button)

    def test_pedestal_grid_is_prefilled_and_flags_placeholder_height(self):
        at = _page_with_drawing(_extraction(), Project(project_name="", drawing_no=""))
        assert not at.exception
        assert any("height" in w.value.lower() for w in at.warning)
        assert any("placeholder" in w.value.lower() for w in at.warning)

    def test_extra_findings_are_shown_but_not_in_the_project(self):
        at = _page_with_drawing(_extraction(), Project(project_name="", drawing_no=""))
        assert not at.exception
        assert at.session_state.project.curb_walls == []

    def test_generate_button_writes_a_workbook(self):
        at = _page_with_drawing(_extraction(), Project(project_name="", drawing_no=""))
        btn = next(b for b in at.button if "Generate BOQ Excel" in b.label)
        btn.click().run()
        assert not at.exception, [e.value for e in at.exception]
        written = at.session_state["last_workbook"]
        assert written and Path(written).exists()

        from openpyxl import load_workbook
        wb = load_workbook(written)
        # The mandated ten-sheet template, in order. Gaps_and_Assumptions is
        # inserted straight after the Summary when the drawing left something
        # unstated, which is almost always — so it is skipped here rather than
        # allowed to shift the ten.
        template = [n for n in wb.sheetnames if n != "Gaps_and_Assumptions"]
        expected = ["Summary", "Assumptions", "Pedestals", "Slab", "Sump",
                    "Joints", "Coating", "Embedments", "Rebar_BBS", "Costing"]
        assert template[:10] == expected
        assert wb.sheetnames[1] == "Gaps_and_Assumptions", \
            "the gaps belong where they will be read, not at the end"
        rows = [r for r in wb["Pedestals"].iter_rows(values_only=True)
                if r and r[0] in TRUTH]
        assert len(rows) == len(TRUTH)

    def test_generated_project_carries_the_extracted_metadata(self):
        at = _page_with_drawing(_extraction(), Project(project_name="", drawing_no=""))
        next(b for b in at.button if "Generate BOQ Excel" in b.label).click().run()
        p = at.session_state.project
        assert p.drawing_no == "MD-522-8110-EG-CV-LAD-0107"
        assert {x.tag for x in p.pedestals} == set(TRUTH)
        assert p.grade_slabs[0].length_m == 24.430


class TestStreamlitCompat:
    def test_image_flag_matches_installed_streamlit(self):
        import streamlit as st
        from extractors.st_compat import _IMAGE_FULL_WIDTH
        import inspect
        params = inspect.signature(st.image).parameters
        assert _IMAGE_FULL_WIDTH in params, (
            f"st.image on streamlit {st.__version__} has no {_IMAGE_FULL_WIDTH}")


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample drawing not in repo root")
class TestDiscoveryPanel:
    """Items the drawing names itself must reach the reviewer, not just the file.

    The five sheets that carry no pedestal and no slab have nothing else to show
    on this page — if the panel does not render, the person checking the takeoff
    sees an empty review grid and concludes the extractor failed.
    """

    def _extraction_with_discovery(self):
        r = _extraction()
        r.discovery = {
            "items": [
                {"kind": "thickness",
                 "description": "Acid Resistant Epoxy Coating, 4 mm thick",
                 "spec": {"thickness_mm": 4.0}, "uom": "m2", "qty": None,
                 "basis": "4 mm thick — area not stated on this sheet",
                 "occurrences": 3, "confirm": True,
                 "source": "4MM THK ACID RESISTANT", "grid_ref": "J-15 → P-14"},
                {"kind": "rebar", "description": "D25 bars — 16 Nos per element",
                 "spec": {"count": 16.0, "diameter_mm": 25.0}, "uom": "Nos",
                 "qty": 16.0, "basis": "16-D25 — bars per element",
                 "occurrences": 5, "confirm": False, "source": "16-D25",
                 "grid_ref": "I-9 → P-7"},
            ],
            "levels": [], "grades": [],
            "heights": [{"height_m": 2.88, "top": "BOBP EL. 98.380",
                         "bottom": "BOC EL. 95.500"}],
            "specifications": [{"value": 35.0, "unit": "MPa",
                                "text": "MINIMUM STRENGTH OF 35 MPa"}],
            "measured": 1, "to_confirm": 1,
        }
        return r

    def test_the_page_renders_with_discovered_items(self):
        at = _page_with_drawing(extraction=self._extraction_with_discovery())
        assert not at.exception

    def test_the_panel_is_shown_and_open(self):
        at = _page_with_drawing(extraction=self._extraction_with_discovery())
        panel = next((e for e in at.expander
                      if "Read from this drawing" in e.label), None)
        assert panel is not None, [e.label for e in at.expander]
        # AppTest does not expose the open/closed flag, so check the content
        # instead: the table of items has to be inside this panel.
        assert panel.dataframe, "the panel rendered without its item table"

    def test_the_panel_counts_what_carries_a_quantity(self):
        at = _page_with_drawing(extraction=self._extraction_with_discovery())
        label = next(e.label for e in at.expander
                     if "Read from this drawing" in e.label)
        assert "2 item(s)" in label and "1 with a stated quantity" in label

    def test_no_panel_when_the_sheet_specified_nothing(self):
        at = _page_with_drawing(extraction=_extraction())
        assert not any("Read from this drawing" in e.label for e in at.expander)


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample drawing not in repo root")
class TestAMissingFigureNeverBlocks:
    """The button used to be disabled unless the drawing produced a pedestal or
    a slab. Five sheets in this set produce neither, so five sheets produced
    nothing — and the missing figure was still missing afterwards."""

    def _bare(self):
        """An extraction with no pedestal and no slab, like a sections sheet."""
        r = _extraction()
        r.pedestals, r.grade_slabs = [], []
        r.discovery = {
            "items": [{"kind": "thickness",
                       "description": "Acid Resistant Epoxy Coating, 4 mm thick",
                       "spec": {"thickness_mm": 4.0}, "uom": "m2", "qty": None,
                       "basis": "4 mm thick — area not stated on this sheet",
                       "occurrences": 3, "confirm": True,
                       "source": "4MM THK ACID RESISTANT", "grid_ref": "J-15"}],
            "levels": [], "heights": [], "grades": [], "specifications": [],
            "measured": 0, "to_confirm": 1}
        return r

    def test_the_generate_button_is_offered(self):
        at = _page_with_drawing(extraction=self._bare())
        assert not at.exception
        gen = next((b for b in at.button
                    if b.label.startswith("🧾") and "Generate BOQ" in b.label), None)
        assert gen is not None, [b.label for b in at.button]
        assert not gen.disabled, "a sections sheet still deserves a workbook"

    def test_the_page_says_why_the_workbook_is_still_worth_having(self):
        at = _page_with_drawing(extraction=self._bare())
        assert any("still worth having" in i.value for i in at.info)

    def test_the_gaps_are_shown_before_the_button(self):
        at = _page_with_drawing(extraction=self._bare())
        assert any("gap(s) and assumption(s)" in e.label for e in at.expander)

    def test_it_generates(self, tmp_path):
        at = _page_with_drawing(extraction=self._bare())
        next(b for b in at.button
             if b.label.startswith("🧾") and "Generate BOQ" in b.label).click().run()
        assert not at.exception
        # AppTest's session state is a SafeSessionState and has no .get().
        assert "last_workbook" in at.session_state
        assert Path(at.session_state["last_workbook"]).exists()

    def test_the_workbook_carries_the_gaps_sheet(self):
        from openpyxl import load_workbook

        at = _page_with_drawing(extraction=self._bare())
        next(b for b in at.button
             if b.label.startswith("🧾") and "Generate BOQ" in b.label).click().run()
        names = load_workbook(at.session_state["last_workbook"]).sheetnames
        assert "Gaps_and_Assumptions" in names
        assert "Drawing_Items" in names


TEXT_LAYER_PDF = ROOT / "Drawings" / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"


@pytest.mark.skipif(not TEXT_LAYER_PDF.exists(), reason="drawings not present")
class TestASheetThatNeedsNoModel:
    """Found by driving the page with Ollama stopped: a drawing that is read
    from its own text layer was still refusing to extract, because the button
    was gated on the model being reachable rather than on the model being
    needed."""

    def _page(self):
        at = AppTest.from_file(PAGE, default_timeout=300)
        at.session_state[AUTH_KEY] = True
        at.run()
        box = next((c for c in at.checkbox
                    if c.label.startswith(TEXT_LAYER_PDF.name)), None)
        if box is None:
            pytest.skip("drawing not in the library")
        box.set_value(True).run()
        return at

    def test_the_page_says_no_model_calls_are_needed(self):
        at = self._page()
        calls = next(m for m in at.metric if m.label == "Model calls needed")
        assert calls.value == "0"

    def test_it_says_so_in_words_too(self):
        at = self._page()
        assert any("No model calls at all" in s.value for s in at.success)

    def test_the_extract_button_is_not_blocked_by_a_stopped_model(self):
        at = self._page()
        button = next(b for b in at.button if b.label.startswith("🔍"))
        assert not button.disabled

    def test_extracting_works_and_costs_no_model_time(self):
        at = self._page()
        next(b for b in at.button if b.label.startswith("🔍")).click().run()
        assert not at.exception
        result = at.session_state["extraction"]
        assert result.used_text_layer
        assert result.montages_sent == 0
