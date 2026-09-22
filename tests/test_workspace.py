"""The workspace: one page, four tabs, badges, and a guard around each tab.

Before this, the app was five pages plus an Extract page whose shape depended on
how many drawings were ticked. These tests pin the replacement: the tabs and
their order, that Input / Review / BOM / Costing live on inside them, that a
tick selects and never opens, what the badges say, that a drawing read before
is ready the moment it is added, which buttons carry the primary colour, and
that one broken tab cannot blank the others.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from ui.auth import SESSION_KEY as AUTH_KEY          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGE = str(ROOT / "pages" / "0_Extract.py")
DRAWINGS = ROOT / "Drawings"
A = DRAWINGS / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"      # has a text layer
B = DRAWINGS / "MD-522-8110-EG-CV-LAD-0102_C01.pdf"

pytestmark = pytest.mark.skipif(not (A.exists() and B.exists()),
                                reason="drawings not present")

TOP_TABS = ["📄 Drawings", "⏳ Queue", "🧾 BOQ", "💰 Pricing"]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace of the test's own: queue, cache, folders and output."""
    from ui.workspace import common as C

    monkeypatch.setenv("CIVIL_ESTIMATOR_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("CIVIL_ESTIMATOR_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CIVIL_ESTIMATOR_UPLOAD_DIR", str(tmp_path / "uploads"))
    drawings = tmp_path / "Drawings"
    drawings.mkdir()
    for pdf in (A, B):
        (drawings / pdf.name).symlink_to(pdf.resolve())
    monkeypatch.setenv("CIVIL_ESTIMATOR_DRAWING_DIRS", str(drawings))
    monkeypatch.setattr(C, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(C, "PROJECT_DIR", tmp_path / "output" / "projects")
    return tmp_path


def _run(project=None) -> "AppTest":
    at = AppTest.from_file(PAGE, default_timeout=300)
    at.session_state[AUTH_KEY] = True
    if project is not None:
        at.session_state["project"] = project
    return at.run()


def _titles(at) -> list[str]:
    return [h.value for h in at.header] + [s.value for s in at.subheader]


def _badges(at) -> list[str]:
    return [m.value for m in at.markdown if m.value.startswith('<span class="ce-badge')]


def _saved_reading(pdf: Path):
    """Store a real reading of `pdf` in the sandbox cache, as the worker would."""
    from core import extract_cache as CACHE
    from core import jobstore as JS
    from extractors import qwen_vision as QV

    CACHE.store(JS.fingerprint(pdf, "thorough"), QV.extract_from_pdf(pdf))


def _box(at, pdf: Path):
    return next(c for c in at.checkbox if c.label.startswith(pdf.name))


class TestTheFourTabs:
    def test_they_are_there_in_order(self, ws):
        at = _run()
        assert not at.exception
        assert [t.label for t in at.tabs if t.label in TOP_TABS] == TOP_TABS

    def test_the_other_pages_are_gone(self):
        assert [p.name for p in (ROOT / "pages").glob("*.py")] == ["0_Extract.py"]

    def test_input_review_and_bom_live_on_in_the_boq_tab(self, ws):
        titles = _titles(_run())
        for section in ("1 · Elements", "2 · Wastage", "3 · Check the bill",
                        "4 · Download"):
            assert section in titles, section
        labels = {t.label for t in _run().tabs}
        assert {"Combined BOQ", "Project estimate"} <= labels

    def test_costing_lives_on_as_pricing(self, ws):
        assert "Pricing" in _titles(_run())

    def test_the_tab_labels_never_change(self):
        """Streamlit identifies a tab by its label; a live count in one would
        snap the view back to the first tab whenever the count moved."""
        source = (ROOT / "pages" / "0_Extract.py").read_text()
        assert 'TABS = ("📄 Drawings", "⏳ Queue", "🧾 BOQ", "💰 Pricing")' in source


class TestStatusBadges:
    def test_an_unread_set_says_so(self, ws):
        badges = _badges(_run())
        assert len(badges) == 2
        assert all(">Not read<" in b for b in badges)

    def test_a_drawing_read_before_shows_its_reading(self, ws):
        _saved_reading(A)
        badges = _badges(_run())
        assert sum(">Not read<" in b for b in badges) == 1
        assert sum(">Ready<" in b or ">Needs review<" in b for b in badges) == 1

    def test_a_queued_drawing_says_queued(self, ws):
        from core import jobstore as JS

        JS.enqueue([ws / "Drawings" / A.name], owner="shared")
        assert any(">Queued<" in b for b in _badges(_run()))

    def test_a_failed_drawing_says_failed(self, ws):
        from core import jobstore as JS

        job = JS.enqueue([ws / "Drawings" / A.name], owner="shared")[0]
        JS.claim_next("w")
        JS.fail(job.id, "boom")
        assert any(">Failed<" in b for b in _badges(_run()))

    def test_the_list_summarises_the_set(self, ws):
        _saved_reading(A)
        at = _run()
        assert any("2 drawing(s)" in c.value and "not read" in c.value
                   for c in at.caption)


class TestAddingToTheQueue:
    def test_a_drawing_read_before_is_ready_at_once(self, ws):
        """No worker runs in this test. The saved reading already exists, so
        filing it as done is bookkeeping, and the combined BOQ can use it."""
        from core import jobstore as JS

        _saved_reading(A)
        at = _run()
        _box(at, A).set_value(True).run()
        _box(at, B).set_value(True).run()
        next(b for b in at.button if b.label.startswith("➕")).click().run()
        assert not at.exception
        states = {j.drawing_name: (j.state, j.from_cache) for j in JS.list_jobs()}
        assert states[A.name] == (JS.DONE, True)
        assert states[B.name][0] == JS.QUEUED
        assert any("ready now" in s.value for s in at.success)
        assert "Choose what goes in the BOQ" in _titles(at)

    def test_re_read_even_if_saved_really_queues_it(self, ws):
        from core import jobstore as JS

        _saved_reading(A)
        at = _run()
        _box(at, A).set_value(True).run()
        next(c for c in at.checkbox if c.label == "Re-read even if saved").check().run()
        next(b for b in at.button if b.label.startswith("➕")).click().run()
        assert JS.list_jobs()[0].state == JS.QUEUED


class TestButtons:
    """The filled, coloured button is the next step of the work."""

    WORKFLOW = ("➕", "🔍", "🧾", "💾 Save rates")

    def test_clear_session_is_outlined(self, ws):
        clear = next(b for b in _run().button if b.label == "🧹 Clear session")
        assert clear.proto.type != "primary"

    def test_its_confirmation_is_not_primary_either(self, ws):
        at = _run()
        next(b for b in at.button if b.label == "🧹 Clear session").click().run()
        yes = next(b for b in at.button if b.label == "Yes, clear the session")
        assert yes.proto.type != "primary"

    def test_only_workflow_steps_are_primary(self, ws):
        _saved_reading(A)
        at = _run()
        _box(at, A).set_value(True).run()
        primary = [b.label for b in at.button if b.proto.type == "primary"]
        assert primary, "the next step should stand out"
        for label in primary:
            assert label.startswith(self.WORKFLOW), label


class TestAFailingTabIsContained:
    @pytest.fixture
    def broken_pricing(self, ws, monkeypatch):
        from ui.workspace import pricing_view

        def boom():
            raise RuntimeError("rates table is on fire")

        monkeypatch.setattr(pricing_view, "render", boom)
        monkeypatch.delenv("CIVIL_ESTIMATOR_STRICT", raising=False)

    def test_the_failure_is_said_in_words(self, broken_pricing):
        at = _run()
        assert not at.exception
        headline = " ".join(e.value for e in at.error)
        assert "Something went wrong in the Pricing tab" in headline
        assert "on fire" not in headline

    def test_the_detail_is_one_click_away(self, broken_pricing):
        at = _run()
        assert any(e.label == "Technical details" for e in at.expander)
        assert any("rates table is on fire" in c.value for c in at.code)

    def test_the_other_tabs_still_work(self, broken_pricing):
        at = _run()
        assert any(b.label == "Open" for b in at.button)
        assert "Reading queue" in _titles(at)

    def test_in_a_test_it_is_loud(self, ws, monkeypatch):
        from ui.workspace import pricing_view

        monkeypatch.setattr(pricing_view, "render",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert _run().exception


def test_no_workspace_module_stops_the_script():
    """`st.stop()` inside a tab ends the whole run, so every tab after it
    silently disappears. Views return instead."""
    offenders = []
    for path in sorted((ROOT / "ui" / "workspace").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "stop"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


class TestTheProjectEstimate:
    """What the Input, Review, BOM and Costing pages did, inside the tabs."""

    @pytest.fixture
    def estimate(self):
        from core.models import Pedestal, Project

        p = Project(project_name="Test", drawing_no="MD-TEST-0001")
        p.pedestals = [Pedestal(tag="P1", length_m=0.5, width_m=0.5, height_m=1.2,
                                quantity=4)]
        return p

    def test_the_bill_is_checked_and_downloadable(self, ws, estimate):
        at = _run(estimate)
        assert not at.exception
        assert {"Total concrete", "Total rebar"} <= {m.label for m in at.metric}
        next(b for b in at.button if b.label == "🧾 Generate Excel BOQ").click().run()
        assert not at.exception
        written = Path(at.session_state["last_output"])
        assert written.exists() and written.parent == ws / "output"

    def test_wastage_is_edited_where_the_bill_is(self, ws, estimate):
        at = _run(estimate)
        box = next(n for n in at.number_input if n.label == "Concrete %")
        box.set_value(7.5).run()
        assert at.session_state["project"].wastage_concrete_pct == 7.5

    def test_pricing_prices_it(self, ws, estimate):
        at = _run(estimate)
        assert any(m.label == "Grand total" for m in at.metric)

    def test_an_empty_estimate_says_where_to_start(self, ws):
        at = _run()
        assert any("Project estimate" in i.value or "Elements" in i.value
                   for i in at.info)
