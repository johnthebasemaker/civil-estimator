"""Clear session, the search box, and landing on the Extract page.

All three exist because of the same complaint: the page accumulated state
across three areas and there was no way to put it back. A half-cleared page is
worse than an uncleared one — a filter left on from the last drawing silently
hides rows, and nobody suspects the filter.
"""
from __future__ import annotations

from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

ROOT = Path(__file__).resolve().parent.parent
from ui.auth import SESSION_KEY as AUTH_KEY          # noqa: E402

PAGE = str(ROOT / "pages" / "0_Extract.py")
DRAWINGS = ROOT / "Drawings"
pytestmark = pytest.mark.skipif(not DRAWINGS.is_dir(),
                                reason="drawings not present")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Queue, cache and uploads of the test's own — with one upload in it, so
    "the uploads are kept" is checked against a folder that has something to
    lose."""
    monkeypatch.setenv("CIVIL_ESTIMATOR_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("CIVIL_ESTIMATOR_CACHE_DIR", str(tmp_path / "cache"))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    sheet = DRAWINGS / "MD-522-8110-EG-CV-LAD-0102_C01.pdf"
    if sheet.exists():
        (uploads / "ZZ-UPLOADED.pdf").write_bytes(sheet.read_bytes())
    monkeypatch.setenv("CIVIL_ESTIMATOR_UPLOAD_DIR", str(uploads))
    # Workbooks, check prints and SET_BOQ.xlsx go to the test's own folder.
    # Before this, every run wrote them into the real output/ — overwriting
    # the SET_BOQ.xlsx someone had just built.
    from ui.workspace import common as _C
    monkeypatch.setattr(_C, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(_C, "PROJECT_DIR", tmp_path / "output" / "projects")
    return tmp_path


def _run(timeout: int = 300) -> "AppTest":
    at = AppTest.from_file(PAGE, default_timeout=timeout)
    at.session_state[AUTH_KEY] = True
    at.run()
    return at


class TestTheButtonIsFindable:
    def test_clear_session_is_offered_at_the_top(self, sandbox):
        at = _run()
        assert not at.exception
        assert any(b.label.startswith("🧹") and "Clear session" in b.label
                   for b in at.button)

    def test_it_asks_before_it_clears(self, sandbox):
        at = _run()
        next(b for b in at.button if "Clear session" in b.label).click().run()
        assert any("Clear the whole session" in w.value for w in at.warning)
        assert any("Yes, clear the session" in b.label for b in at.button)

    def test_saying_no_changes_nothing(self, sandbox):
        at = _run()
        at.session_state["sel_search"] = "epoxy"
        next(b for b in at.button if "Clear session" in b.label).click().run()
        next(b for b in at.button if b.label == "Keep everything").click().run()
        assert at.session_state["sel_search"] == "epoxy"


class TestItClearsAllThreeAreas:
    @pytest.fixture
    def dirty(self, sandbox):
        """A page carrying state in every area, the way it looks after use."""
        from core import extract_cache as CACHE
        from core import jobstore as JS
        from extractors import qwen_vision as QV

        pdf = DRAWINGS / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"
        if not pdf.exists():
            pytest.skip("sample drawing missing")
        job = JS.enqueue([pdf], owner="shared")[0]
        CACHE.store(job.fingerprint, QV.extract_from_pdf(pdf))
        JS.claim_next("w1")
        JS.finish(job.id)

        at = _run()
        # Area 1: a ticked drawing and a filed classification.
        at.session_state[f"pick::{pdf}"] = True
        at.session_state[f"class::{pdf}"] = "Repair"
        # Area 2: an extraction and the workbook it produced.
        at.session_state["last_workbook"] = "output/whatever.xlsx"
        # Area 3: filters, picks and a built BOQ.
        at.session_state["sel_search"] = "epoxy"
        at.session_state["sel_dwg"] = ["MD-522-8110-EG-CV-LAD-0101"]
        at.session_state["drawing_picks"] = {"MD-522-8110-EG-CV-LAD-0101": True}
        at.session_state["line_picks"] = {"a|boq|b|c|d": True}
        at.session_state["set_boq_path"] = "output/SET_BOQ.xlsx"
        at.run()
        return at

    def _clear(self, at):
        next(b for b in at.button if "Clear session" in b.label).click().run()
        next(b for b in at.button
             if b.label == "Yes, clear the session").click().run()
        return at

    def test_area_one_is_reset(self, dirty):
        # Checked by name rather than by scanning: AppTest's session state is a
        # SafeSessionState and iterating it yields positions, not keys.
        pdf = DRAWINGS / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"
        at = self._clear(dirty)
        assert f"pick::{pdf}" not in at.session_state
        assert f"class::{pdf}" not in at.session_state
        # The page recreates this one empty on the very next render, which is
        # the reset working rather than surviving it.
        assert not at.session_state["_seen_uploads"]

    def test_area_two_is_reset(self, dirty):
        at = self._clear(dirty)
        for key in ("extraction", "last_workbook", "last_check_print"):
            assert key not in at.session_state

    def test_area_three_is_reset(self, dirty):
        at = self._clear(dirty)
        for key in ("sel_search", "sel_dwg", "drawing_picks", "line_picks",
                    "set_boq_path"):
            assert key not in at.session_state

    def test_the_finished_queue_rows_go_too(self, dirty):
        """Area 3 is built from finished jobs. Leaving them would refill the
        section the moment the page redrew — the ghost rendering to avoid."""
        from core import jobstore as JS

        at = self._clear(dirty)
        assert not JS.list_jobs(owner="shared")
        titles = [h.value for h in at.header] + [s.value for s in at.subheader]
        assert "Choose what goes in the BOQ" not in titles

    def test_the_saved_extractions_are_kept(self, dirty, sandbox):
        """Clearing costs no model time to undo."""
        cache = sandbox / "cache"
        before = len(list(cache.glob("*.json")))
        self._clear(dirty)
        assert len(list(cache.glob("*.json"))) == before
        assert before > 0

    def test_the_uploaded_files_are_kept_unless_asked(self, dirty, sandbox):
        upload_dir = sandbox / "uploads"
        before = len(list(upload_dir.glob("*.pdf")))
        self._clear(dirty)
        assert len(list(upload_dir.glob("*.pdf"))) == before
        assert before > 0

    def test_the_classification_survives_a_clear(self, dirty):
        """It describes the drawing, not the session. The drawings stay, so the
        filing stays with them."""
        from core import classification as C

        pdf = DRAWINGS / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"
        C.set_for(pdf, C.BROWN_FIELD)
        self._clear(dirty)
        assert C.get(pdf) == C.BROWN_FIELD

    def test_it_says_what_it_did(self, dirty):
        at = self._clear(dirty)
        assert any("Session cleared" in s.value for s in at.success)

    def test_the_login_survives(self, dirty):
        """A clear that signs you out is a clear nobody presses twice."""
        at = self._clear(dirty)
        assert at.session_state[AUTH_KEY] is True
        assert not at.exception


class TestSearch:
    def test_the_matcher_needs_every_word(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("xp", PAGE)
        _ = spec  # the page cannot be imported outside Streamlit; test the rule
        rows = [
            {"Drawing": "MD-0107", "Source": "BOQ", "Group": "Rebar (coefficient)",
             "Description": "Rebar for P1", "UoM": "kg"},
            {"Drawing": "MD-0106", "Source": "BOQ", "Group": "Epoxy Coating",
             "Description": "Epoxy to slab", "UoM": "m2"},
        ]
        from core.search import search_rows      # noqa: PLC0415

        assert len(search_rows(rows, "rebar")) == 1
        assert len(search_rows(rows, "rebar 0107")) == 1
        assert len(search_rows(rows, "rebar 0106")) == 0
        assert len(search_rows(rows, "")) == 2

    def test_it_ignores_case_and_order(self):
        from core.search import search_rows

        rows = [{"Drawing": "MD-0107", "Source": "BOQ", "Group": "Epoxy Coating",
                 "Description": "Epoxy to slab", "UoM": "m2"}]
        assert search_rows(rows, "EPOXY slab") == rows
        assert search_rows(rows, "slab epoxy") == rows

    def test_the_box_is_on_the_page(self, sandbox):
        from core import extract_cache as CACHE
        from core import jobstore as JS
        from extractors import qwen_vision as QV

        pdf = DRAWINGS / "MD-522-8110-EG-CV-LAD-0106_C01.pdf"
        if not pdf.exists():
            pytest.skip("sample drawing missing")
        job = JS.enqueue([pdf], owner="shared")[0]
        CACHE.store(job.fingerprint, QV.extract_from_pdf(pdf))
        JS.claim_next("w1")
        JS.finish(job.id)

        at = _run()
        assert any(t.label == "Search" for t in at.text_input), \
            [t.label for t in at.text_input]
