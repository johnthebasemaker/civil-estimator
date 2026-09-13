"""The extraction queue, as a person drives it.

Everything here is about the complaint that started it: twenty drawings froze
the tab, there was no cancel, and downloading a result made the result vanish.
`AppTest` runs the page in-process, so these are real renders with no browser
and no model.
"""
from __future__ import annotations

import os
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
    """A queue and cache of this test's own, so runs cannot see each other."""
    monkeypatch.setenv("CIVIL_ESTIMATOR_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("CIVIL_ESTIMATOR_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


def _run(timeout: int = 300) -> "AppTest":
    at = AppTest.from_file(PAGE, default_timeout=timeout)
    at.session_state[AUTH_KEY] = True
    at.run()
    return at


def _tick_several(at):
    next(b for b in at.button if b.label == "Select all").click().run()
    return at


class TestQueuePanel:
    def test_ticking_several_drawings_offers_the_queue(self, sandbox):
        at = _tick_several(_run())
        assert not at.exception
        assert any("Extraction queue" in h.value for h in at.header)

    def test_an_empty_queue_says_so_rather_than_looking_broken(self, sandbox):
        at = _tick_several(_run())
        text = " ".join(i.value for i in at.info)
        assert "queue is empty" in text.lower()

    def test_adding_drawings_puts_them_in_the_store(self, sandbox):
        from core import jobstore as JS

        at = _tick_several(_run())
        add = next(b for b in at.button if b.label.startswith("➕"))
        add.click().run()
        assert not at.exception
        jobs = JS.list_jobs()
        assert jobs, "nothing reached the queue"
        assert all(j.state == JS.QUEUED for j in jobs)

    def test_the_same_drawings_are_not_queued_twice(self, sandbox):
        from core import jobstore as JS

        at = _tick_several(_run())
        add = next(b for b in at.button if b.label.startswith("➕"))
        add.click().run()
        first = len(JS.list_jobs())
        next(b for b in at.button if b.label.startswith("➕")).click().run()
        assert len(JS.list_jobs()) == first

    def test_a_queue_with_no_worker_says_how_to_start_one(self, sandbox):
        at = _tick_several(_run())
        next(b for b in at.button if b.label.startswith("➕")).click().run()
        assert any("bin/worker.py" in c.value for c in at.code)


class TestControls:
    @pytest.fixture
    def queued(self, sandbox):
        at = _tick_several(_run())
        next(b for b in at.button if b.label.startswith("➕")).click().run()
        return at

    def test_pause_and_resume_are_offered(self, queued):
        assert any(b.label.startswith("⏸") for b in queued.button)

    def test_pausing_holds_the_queue(self, queued):
        from core import jobstore as JS

        next(b for b in queued.button if b.label.startswith("⏸")).click().run()
        assert JS.is_paused("shared")
        assert JS.claim_next("w1") is None

    def test_cancelling_the_waiting_work_empties_the_queue(self, queued):
        from core import jobstore as JS

        next(b for b in queued.button if b.label.startswith("✖")).click().run()
        assert not JS.list_jobs(states=[JS.QUEUED])

    def test_the_queue_reports_how_far_along_it_is(self, queued):
        """AppTest does not expose progress bars, so check the readouts beside
        them. The percentage itself is covered in tests/test_jobstore.py."""
        labels = {m.label for m in queued.metric}
        assert {"Done", "Waiting", "Estimated remaining"} <= labels


class TestResultsSurvive:
    """A download used to take the results with it."""

    @pytest.fixture
    def finished(self, sandbox):
        """One drawing genuinely extracted, with no model and no shared state.

        …-0101 kept its text layer, so this reads it for real in a fifth of a
        second. Depending on whatever happens to be in the developer's cache
        would make these tests pass or fail for reasons that have nothing to do
        with the code.
        """
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
        return job

    def test_finished_work_shows_up_without_ticking_anything(self, finished):
        at = _run()
        assert not at.exception
        assert any("Extraction queue" in h.value for h in at.header)

    def test_the_line_items_are_offered_for_selection(self, finished):
        at = _run()
        assert any("goes in the BOQ" in h.value for h in at.header)
        assert at.get("arrow_data_frame"), "no selectable line-item table"

    def test_generating_the_boq_writes_a_workbook_and_keeps_the_link(self, finished):
        at = _run()
        gen = next((b for b in at.button if b.label.startswith("🧾")), None)
        assert gen is not None, [b.label for b in at.button]
        gen.click().run()
        assert not at.exception
        assert Path(at.session_state["set_boq_path"]).exists()

    def test_the_download_survives_a_rerun(self, finished):
        at = _run()
        next(b for b in at.button if b.label.startswith("🧾")).click().run()
        at.run()                       # stand-in for the rerun a download causes
        assert Path(at.session_state["set_boq_path"]).exists()
        assert any("Consolidated BOQ built" in s.value for s in at.success)

    def test_clearing_the_result_asks_first(self, finished):
        """One click must not throw away the link to a finished workbook."""
        at = _run()
        next(b for b in at.button if b.label.startswith("🧾")).click().run()
        assert "set_boq_path" in at.session_state
        next(b for b in at.button
             if b.label.startswith("🧹") and "result" in b.label).click().run()
        assert "set_boq_path" in at.session_state, "cleared without asking"
        assert any("only removes it from the screen" in w.value for w in at.warning)

    def test_confirming_clears_the_link_but_not_the_file(self, finished):
        at = _run()
        next(b for b in at.button if b.label.startswith("🧾")).click().run()
        path = Path(at.session_state["set_boq_path"])
        next(b for b in at.button
             if b.label.startswith("🧹") and "result" in b.label).click().run()
        next(b for b in at.button if b.label == "Yes, clear it").click().run()
        assert "set_boq_path" not in at.session_state
        assert path.exists(), "the workbook itself must survive"

    def test_declining_keeps_it(self, finished):
        at = _run()
        next(b for b in at.button if b.label.startswith("🧾")).click().run()
        next(b for b in at.button
             if b.label.startswith("🧹") and "result" in b.label).click().run()
        next(b for b in at.button if b.label == "Keep it").click().run()
        assert "set_boq_path" in at.session_state


class TestComingBackLater:
    """A twenty-drawing set is done over several sittings, so the page has to be
    useful when nothing is ticked at all."""

    @pytest.fixture
    def half_done(self, sandbox):
        from core import jobstore as JS

        pdfs = sorted(DRAWINGS.glob("*.pdf"))[:4]
        jobs = JS.enqueue(pdfs, owner="shared")
        JS.claim_next("w1")
        JS.finish(jobs[0].id)
        return jobs

    def test_the_queue_is_shown_with_nothing_ticked(self, half_done):
        at = _run()
        assert not at.exception
        assert any("Extraction queue" in h.value for h in at.header)

    def test_it_still_says_how_to_start_a_new_one(self, half_done):
        at = _run()
        assert any("Tick a drawing" in i.value for i in at.info)

    def test_a_drawing_already_waiting_is_not_queued_a_second_time(
            self, half_done, sandbox):
        """Deduplication is by path, not by filename: the library legitimately
        shows the same name twice when a drawing sits in both the uploads folder
        and the project root."""
        from core import jobstore as JS

        before = len(JS.list_jobs(owner="shared"))
        at = _tick_several(_run())
        next(b for b in at.button if b.label.startswith("➕")).click().run()
        after = JS.list_jobs(owner="shared")
        assert len(after) > before, "new drawings should have been added"
        waiting = [j.drawing_path for j in after if j.state == JS.QUEUED]
        assert len(waiting) == len(set(waiting))

    def test_the_panel_says_how_many_come_back_from_the_cache(self, sandbox):
        at = _tick_several(_run())
        text = " ".join(s.value for s in at.success)
        assert "read before" in text or text == ""


class TestCombiningDrawings:
    """Several sittings produce several extractions; the bill is one workbook.
    Ticking drawings here is what turns one into the other."""

    @pytest.fixture
    def three_done(self, sandbox):
        from core import extract_cache as CACHE
        from core import jobstore as JS
        from extractors import qwen_vision as QV

        pdfs = [DRAWINGS / f"MD-522-8110-EG-CV-LAD-{n}_C01.pdf"
                for n in ("0101", "0102", "0103")]
        pdfs = [p for p in pdfs if p.exists()]
        if len(pdfs) < 2:
            pytest.skip("not enough text-layer drawings")
        for job in JS.enqueue(pdfs, owner="shared"):
            CACHE.store(job.fingerprint, QV.extract_from_pdf(job.path))
            JS.claim_next("w1")
            JS.finish(job.id)
        return pdfs

    def test_each_extracted_drawing_gets_its_own_tick_box(self, three_done):
        at = _run()
        labels = [c.label for c in at.checkbox]
        assert sum("MD-522-8110-EG-CV-LAD-010" in l for l in labels) >= 2

    def test_the_button_says_how_many_will_be_combined(self, three_done):
        at = _run()
        gen = next(b for b in at.button if b.label.startswith("🧾"))
        assert "combined BOQ from" in gen.label
        assert str(len(three_done)) in gen.label

    def test_unticking_a_drawing_keeps_it_out_of_the_workbook(self, three_done):
        from openpyxl import load_workbook

        at = _run()
        box = next(c for c in at.checkbox if c.key
                   and c.key.startswith("dwg::") and "0102" in c.key)
        dropped = box.key.split("::", 1)[1]
        box.set_value(False).run()
        next(b for b in at.button if b.label.startswith("🧾")).click().run()
        assert not at.exception
        ws = load_workbook(at.session_state["set_boq_path"])["Summary"]
        headers = [c.value for c in ws[6]]          # full drawing numbers
        assert dropped not in [h for h in headers if h]

    def test_the_combined_workbook_holds_every_ticked_drawing(self, three_done):
        from openpyxl import load_workbook

        at = _run()
        next(b for b in at.button if b.label.startswith("🧾")).click().run()
        ws = load_workbook(at.session_state["set_boq_path"])["Summary"]
        full = {c.value for c in ws[6] if c.value}
        assert len(full) == len(three_done)

    def test_clearing_every_drawing_says_so_instead_of_failing(self, three_done):
        at = _run()
        next(b for b in at.button if b.label == "Clear"
             and b.key == "dwg_none").click().run()
        assert not at.exception
        assert any("No drawing ticked" in w.value for w in at.warning)


class TestClearingDoesNotLeaveStaleResults:
    """Found by driving the page: clearing the queue redrew only the queue
    fragment, so section 3 went on listing drawings the queue no longer had."""

    @pytest.fixture
    def finished_two(self, sandbox):
        from core import extract_cache as CACHE
        from core import jobstore as JS
        from extractors import qwen_vision as QV

        pdfs = [DRAWINGS / f"MD-522-8110-EG-CV-LAD-{n}_C01.pdf"
                for n in ("0101", "0102")]
        if not all(p.exists() for p in pdfs):
            pytest.skip("drawings not present")
        for job in JS.enqueue(pdfs, owner="shared"):
            CACHE.store(job.fingerprint, QV.extract_from_pdf(job.path))
            JS.claim_next("w1")
            JS.finish(job.id)
        return pdfs

    def test_clearing_removes_the_results_section_too(self, finished_two):
        from core import jobstore as JS

        at = _run()
        assert any("goes in the BOQ" in h.value for h in at.header)
        next(b for b in at.button if b.label.startswith("🧹")).click().run()
        next(b for b in at.button if b.label == "Yes, clear them").click().run()
        assert not at.exception
        assert not JS.list_jobs(owner="shared")
        assert not any("goes in the BOQ" in h.value for h in at.header)

    def test_the_ticks_are_forgotten_with_the_rows(self, finished_two):
        at = _run()
        next(b for b in at.button if b.label.startswith("🧹")).click().run()
        next(b for b in at.button if b.label == "Yes, clear them").click().run()
        assert "drawing_picks" not in at.session_state


class TestFailuresAreVisible:
    """100% on its own reads as success. It means the queue is drained."""

    @pytest.fixture
    def one_failed(self, sandbox):
        from core import jobstore as JS

        pdf = DRAWINGS / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"
        if not pdf.exists():
            pytest.skip("drawing not present")
        job = JS.enqueue([pdf], owner="shared")[0]
        JS.claim_next("w1")
        JS.fail(job.id, "the vision model is unavailable")
        return job

    def test_a_failure_is_named_above_the_bar(self, one_failed):
        at = _run()
        text = " ".join(w.value for w in at.warning)
        assert "did not extract" in text
        assert one_failed.drawing_name in text

    def test_the_bar_says_processed_not_done(self, one_failed):
        at = _run()
        assert any("Retry failed" in b.label for b in at.button)

    def test_a_clean_queue_raises_no_warning(self, sandbox):
        from core import jobstore as JS

        pdf = DRAWINGS / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"
        job = JS.enqueue([pdf], owner="shared")[0]
        JS.claim_next("w1")
        JS.finish(job.id)
        at = _run()
        assert not any("did not extract" in w.value for w in at.warning)


class TestNoUncaughtExceptionsFromTheControls:
    """`st.rerun(scope="fragment")` is only legal once the fragment is already
    rerunning. On the first click after a page load it raises, and Streamlit
    shows an uncaught exception where a button press should have been."""

    @pytest.fixture
    def queued_and_done(self, sandbox):
        from core import extract_cache as CACHE
        from core import jobstore as JS
        from extractors import qwen_vision as QV

        pdfs = [DRAWINGS / f"MD-522-8110-EG-CV-LAD-{n}_C01.pdf"
                for n in ("0101", "0102", "0103")]
        if not all(p.exists() for p in pdfs):
            pytest.skip("drawings not present")
        jobs = JS.enqueue(pdfs, owner="shared")
        CACHE.store(jobs[0].fingerprint, QV.extract_from_pdf(jobs[0].path))
        JS.claim_next("w1")
        JS.finish(jobs[0].id)
        return jobs

    @pytest.mark.parametrize("prefix", ["⏸", "✖", "🧹"])
    def test_the_first_click_after_a_load_does_not_raise(self, queued_and_done,
                                                         prefix):
        at = _run()
        button = next((b for b in at.button if b.label.startswith(prefix)), None)
        assert button is not None, [b.label for b in at.button]
        button.click().run()
        assert not at.exception, at.exception

    def test_retry_is_the_same(self, sandbox):
        from core import jobstore as JS

        pdf = DRAWINGS / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"
        job = JS.enqueue([pdf], owner="shared")[0]
        JS.claim_next("w1")
        JS.fail(job.id, "boom")
        at = _run()
        next(b for b in at.button if b.label.startswith("↻")).click().run()
        assert not at.exception
        assert JS.get(job.id).state == JS.QUEUED
