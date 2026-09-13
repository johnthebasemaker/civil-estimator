"""The extraction worker.

Driven end to end against real drawings. The five sheets that kept their text
layer need no model at all, so the whole flow — claim, read, cache, finish — is
testable without Ollama running and without minutes of GPU time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import extract_cache as CACHE
from core import jobstore as JS

# bin/ is a script directory, not a package, so the worker is loaded by path.
import importlib.util  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("worker", ROOT / "bin" / "worker.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)

TEXT_SHEET = ROOT / "Drawings" / "MD-522-8110-EG-CV-LAD-0101_C01.pdf"
pytestmark = pytest.mark.skipif(not TEXT_SHEET.exists(),
                                reason="drawings not present")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CIVIL_ESTIMATOR_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("CIVIL_ESTIMATOR_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(ROOT)
    return tmp_path


class TestNoModelNeeded:
    def test_a_text_layer_sheet_is_read_without_ollama(self, env):
        """The worker must not demand a model for a drawing that needs none."""
        JS.enqueue([TEXT_SHEET], owner="t")
        W.run(once=True)
        job = JS.list_jobs(owner="t")[0]
        assert job.state == JS.DONE, job.error
        assert not job.from_cache

    def test_it_is_recognised_as_a_no_model_sheet(self, env):
        assert W._reads_from_text(TEXT_SHEET) is True

    def test_the_result_is_saved_where_the_queue_says(self, env):
        JS.enqueue([TEXT_SHEET], owner="t")
        W.run(once=True)
        job = JS.list_jobs(owner="t")[0]
        assert Path(job.json_path).is_file()


class TestCaching:
    def test_the_second_run_is_served_from_the_cache(self, env):
        JS.enqueue([TEXT_SHEET], owner="t")
        W.run(once=True)
        JS.clear(owner="t")
        JS.enqueue([TEXT_SHEET], owner="t")
        W.run(once=True)
        job = JS.list_jobs(owner="t")[0]
        assert job.state == JS.DONE
        assert job.from_cache is True

    def test_forcing_a_re_read_bypasses_it(self, env):
        JS.enqueue([TEXT_SHEET], owner="t")
        W.run(once=True)
        JS.clear(owner="t")
        JS.enqueue([TEXT_SHEET], owner="t", force=True)
        W.run(once=True)
        assert JS.list_jobs(owner="t")[0].from_cache is False

    def test_a_cache_hit_still_writes_the_readable_json(self, env):
        JS.enqueue([TEXT_SHEET], owner="t")
        W.run(once=True)
        JS.clear(owner="t")
        JS.enqueue([TEXT_SHEET], owner="t")
        W.run(once=True)
        assert Path(JS.list_jobs(owner="t")[0].json_path).is_file()


class TestModelIsOnlyUsedWhenNeeded:
    """Ollama being down must not fail a drawing the worker can answer without
    it. That is most of a re-run, and all of a text-layer sheet."""

    def test_a_cached_vision_drawing_needs_no_model(self, env):
        from core import extract_cache as CACHE
        from extractors.models import ExtractionResult

        vision = ROOT / "Drawings" / "MD-522-8110-EG-CV-LAD-0107_C01.pdf"
        if not vision.exists():
            pytest.skip("sample drawing missing")
        assert W._reads_from_text(vision) is False, "pick a sheet with no text layer"

        job = JS.enqueue([vision], owner="t")[0]
        CACHE.store(job.fingerprint, ExtractionResult(source_pdf=str(vision)))
        assert W._served_from_cache(job) is True

        W.run(once=True)                       # Ollama is not running in tests
        stored = JS.get(job.id)
        assert stored.state == JS.DONE, stored.error
        assert stored.from_cache

    def test_forcing_a_re_read_does_need_one(self, env):
        vision = ROOT / "Drawings" / "MD-522-8110-EG-CV-LAD-0107_C01.pdf"
        if not vision.exists():
            pytest.skip("sample drawing missing")
        job = JS.enqueue([vision], owner="t", force=True)[0]
        assert W._served_from_cache(job) is False


class TestStopping:
    def test_a_cancel_requested_before_the_work_starts_is_honoured(self, env):
        job = JS.enqueue([TEXT_SHEET], owner="t")[0]
        JS.request_cancel(job.id)
        W.run(once=True)
        assert JS.get(job.id).state == JS.CANCELLED

    def test_a_paused_queue_is_left_alone(self, env):
        JS.enqueue([TEXT_SHEET], owner="t")
        JS.set_paused(True, owner="t")
        W.run(once=True)
        assert JS.list_jobs(owner="t")[0].state == JS.QUEUED


class TestFailures:
    def test_a_missing_file_fails_the_job_rather_than_the_worker(self, env, tmp_path):
        gone = tmp_path / "not-here.pdf"
        gone.write_bytes(TEXT_SHEET.read_bytes())
        job = JS.enqueue([gone], owner="t")[0]
        gone.unlink()
        W.run(once=True)
        stored = JS.get(job.id)
        assert stored.state == JS.FAILED
        assert "no longer" in stored.error

    def test_one_bad_drawing_does_not_stop_the_queue(self, env, tmp_path):
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4 this is not a drawing")
        JS.enqueue([broken, TEXT_SHEET], owner="t")
        W.run(once=True)
        states = {j.drawing_name: j.state for j in JS.list_jobs(owner="t")}
        assert states["broken.pdf"] == JS.FAILED
        assert states[TEXT_SHEET.name] == JS.DONE


class TestProgress:
    def test_a_finished_job_reads_a_hundred_percent(self, env):
        JS.enqueue([TEXT_SHEET], owner="t")
        W.run(once=True)
        assert JS.list_jobs(owner="t")[0].progress_pct == 100
