"""The extraction queue.

What this is defending: a batch used to run inside the Streamlit script thread,
which froze the tab for the length of the batch and made a cancel button
impossible. Moving the work out is only worth it if the queue is trustworthy —
so the tests here are mostly about the three things that go wrong in queues:
two workers taking one job, a worker dying mid-job, and a stop request arriving
while something is already running.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from core import jobstore as JS

SHEET = Path("Drawings/MD-522-8110-EG-CV-LAD-0101_C01.pdf")
OTHER = Path("Drawings/MD-522-8110-EG-CV-LAD-0102_C01.pdf")


@pytest.fixture
def db(tmp_path):
    return tmp_path / "jobs.db"


@pytest.fixture
def two(db):
    return JS.enqueue([SHEET, OTHER], owner="a", db_path=db)


pytestmark = pytest.mark.skipif(not SHEET.exists(), reason="drawings not present")


class TestFingerprint:
    def test_the_same_bytes_give_the_same_key(self, tmp_path):
        copy = tmp_path / "renamed.pdf"
        copy.write_bytes(SHEET.read_bytes())
        assert JS.fingerprint(copy) == JS.fingerprint(SHEET)

    def test_different_drawings_differ(self):
        assert JS.fingerprint(SHEET) != JS.fingerprint(OTHER)

    def test_a_reissue_is_a_different_job(self, tmp_path):
        """The failure this prevents: last revision's numbers served silently."""
        reissued = tmp_path / SHEET.name
        reissued.write_bytes(SHEET.read_bytes() + b"%revised")
        assert JS.fingerprint(reissued) != JS.fingerprint(SHEET)

    def test_the_profile_is_part_of_the_key(self):
        assert JS.fingerprint(SHEET, "thorough") != JS.fingerprint(SHEET, "sweep")


class TestEnqueue:
    def test_jobs_come_back_in_order(self, two):
        assert [j.drawing_name for j in two] == [SHEET.name, OTHER.name]
        assert two[0].position < two[1].position

    def test_a_drawing_already_waiting_is_not_queued_twice(self, db, two):
        again = JS.enqueue([SHEET], owner="a", db_path=db)
        assert again == []
        assert len(JS.list_jobs(owner="a", db_path=db)) == 2

    def test_another_owner_may_queue_the_same_drawing(self, db, two):
        mine = JS.enqueue([SHEET], owner="b", db_path=db)
        assert len(mine) == 1

    def test_a_finished_drawing_can_be_queued_again(self, db, two):
        JS.finish(two[0].id, db_path=db)
        again = JS.enqueue([SHEET], owner="a", db_path=db)
        assert len(again) == 1


class TestClaiming:
    def test_two_workers_never_get_the_same_job(self, db, two):
        first = JS.claim_next("w1", db_path=db)
        second = JS.claim_next("w2", db_path=db)
        assert first is not None and second is not None
        assert first.id != second.id
        assert JS.claim_next("w3", db_path=db) is None

    def test_claiming_marks_it_running_with_the_worker_that_took_it(self, db, two):
        job = JS.claim_next("w1", db_path=db)
        stored = JS.get(job.id, db_path=db)
        assert stored.state == JS.RUNNING
        assert stored.worker_id == "w1"

    def test_a_worker_can_be_pinned_to_one_owner(self, db, two):
        JS.enqueue([SHEET], owner="b", db_path=db)
        job = JS.claim_next("w1", owner="b", db_path=db)
        assert job.owner == "b"


class TestStopping:
    def test_a_queued_job_cancels_immediately(self, db, two):
        assert JS.request_cancel(two[1].id, db_path=db) == 1
        assert JS.get(two[1].id, db_path=db).state == JS.CANCELLED

    def test_a_running_job_is_asked_rather_than_killed(self, db, two):
        """Half a workbook looks finished and is not, so nothing is killed."""
        job = JS.claim_next("w1", db_path=db)
        JS.request_cancel(job.id, db_path=db)
        assert JS.get(job.id, db_path=db).state == JS.RUNNING
        assert JS.heartbeat(job.id, progress_pct=40, db_path=db) is False

    def test_the_heartbeat_says_keep_going_until_it_does_not(self, db, two):
        job = JS.claim_next("w1", db_path=db)
        assert JS.heartbeat(job.id, progress_pct=10, stage="x", db_path=db) is True
        JS.request_cancel(job.id, db_path=db)
        assert JS.heartbeat(job.id, progress_pct=20, db_path=db) is False

    def test_pause_stops_new_work_without_abandoning_the_current_drawing(self, db, two):
        running = JS.claim_next("w1", db_path=db)
        JS.set_paused(True, owner="a", db_path=db)
        assert JS.claim_next("w2", db_path=db) is None
        assert JS.get(running.id, db_path=db).state == JS.RUNNING
        assert JS.heartbeat(running.id, db_path=db) is True

    def test_resume_lets_the_queue_move_again(self, db, two):
        JS.set_paused(True, owner="a", db_path=db)
        assert JS.claim_next("w1", db_path=db) is None
        JS.set_paused(False, owner="a", db_path=db)
        assert JS.claim_next("w1", db_path=db) is not None

    def test_one_owner_s_pause_does_not_hold_another_s(self, db, two):
        JS.enqueue([SHEET], owner="b", db_path=db)
        JS.set_paused(True, owner="a", db_path=db)
        job = JS.claim_next("w1", db_path=db)
        assert job is not None and job.owner == "b"


class TestWorkerDeath:
    def test_a_silent_worker_s_job_goes_back_to_the_queue(self, db, two):
        job = JS.claim_next("w1", db_path=db)
        assert JS.reap_stale(stale_after=0.0, db_path=db) == 1
        assert JS.get(job.id, db_path=db).state == JS.QUEUED

    def test_a_drawing_that_kills_two_workers_fails_honestly(self, db, two):
        job = JS.claim_next("w1", db_path=db)
        JS.reap_stale(stale_after=0.0, db_path=db)
        JS.claim_next("w2", db_path=db)
        JS.reap_stale(stale_after=0.0, db_path=db)
        stored = JS.get(job.id, db_path=db)
        assert stored.state == JS.FAILED
        assert "twice" in stored.error

    def test_a_live_worker_is_left_alone(self, db, two):
        JS.claim_next("w1", db_path=db)
        assert JS.reap_stale(stale_after=600.0, db_path=db) == 0


class TestRecovery:
    def test_a_failed_job_can_be_retried(self, db, two):
        JS.fail(two[0].id, "boom", db_path=db)
        assert JS.retry(two[0].id, db_path=db) == 1
        stored = JS.get(two[0].id, db_path=db)
        assert stored.state == JS.QUEUED and stored.error == ""

    def test_retry_puts_it_at_the_back_not_the_front(self, db, two):
        JS.fail(two[0].id, "boom", db_path=db)
        JS.retry(two[0].id, db_path=db)
        assert (JS.get(two[0].id, db_path=db).position
                > JS.get(two[1].id, db_path=db).position)

    def test_a_running_job_cannot_be_retried_out_from_under_its_worker(self, db, two):
        job = JS.claim_next("w1", db_path=db)
        assert JS.retry(job.id, db_path=db) == 0


class TestResultsDoNotVanish:
    """The complaint this queue grew out of: a download triggered a rerun, and
    the results went with it."""

    def test_finished_jobs_survive_until_someone_clears_them(self, db, two):
        JS.finish(two[0].id, json_path="out.json", db_path=db)
        for _ in range(3):                       # stand-in for repeated reruns
            assert len(JS.list_jobs(owner="a", states=[JS.DONE], db_path=db)) == 1

    def test_clearing_is_explicit_and_only_touches_finished_work(self, db, two):
        JS.finish(two[0].id, db_path=db)
        assert JS.clear(owner="a", db_path=db) == 1
        left = JS.list_jobs(owner="a", db_path=db)
        assert [j.state for j in left] == [JS.QUEUED]


class TestProgressReporting:
    def test_overall_percent_counts_the_running_drawing_s_own_progress(self, db, two):
        """Otherwise a twenty-drawing bar looks stuck for minutes at a time."""
        job = JS.claim_next("w1", db_path=db)
        JS.heartbeat(job.id, progress_pct=50.0, db_path=db)
        # Half of one drawing out of two queued is a quarter of the batch.
        assert JS.summary(owner="a", db_path=db)["percent"] == pytest.approx(25.0)

    def test_a_finished_queue_reads_a_hundred(self, db, two):
        for job in two:
            JS.finish(job.id, db_path=db)
        assert JS.summary(owner="a", db_path=db)["percent"] == 100.0

    def test_an_estimate_appears_once_something_has_been_timed(self, db, two):
        job = JS.claim_next("w1", db_path=db)
        time.sleep(0.15)
        JS.finish(job.id, db_path=db)
        s = JS.summary(owner="a", db_path=db)
        assert s["mean_seconds"] > 0
        assert s["eta_seconds"] > 0        # one drawing still to go

    def test_cached_runs_do_not_drag_the_estimate_down(self, db, two):
        """A cache hit takes milliseconds and says nothing about the next
        drawing's model time."""
        JS.finish(two[0].id, from_cache=True, db_path=db)
        assert JS.summary(owner="a", db_path=db)["mean_seconds"] == 0.0


class TestPages:
    """A job reads one page of a PDF. Page 1 must keep the fingerprint it always
    had, or every extraction saved before pages were tracked goes missing."""

    def test_page_one_hashes_exactly_as_before(self):
        import hashlib

        digest = hashlib.sha256(b"thorough")
        digest.update(SHEET.read_bytes())
        assert JS.fingerprint(SHEET) == digest.hexdigest()[:32]
        assert JS.fingerprint(SHEET, "thorough", 0) == JS.fingerprint(SHEET)

    def test_another_page_is_another_reading(self):
        assert JS.fingerprint(SHEET, "thorough", 1) != JS.fingerprint(SHEET)

    def test_the_page_is_stored_on_the_job(self, db):
        job = JS.enqueue([SHEET], owner="a", page=2, db_path=db)[0]
        assert JS.get(job.id, db_path=db).page == 2
        assert job.fingerprint == JS.fingerprint(SHEET, "thorough", 2)

    def test_two_pages_of_one_drawing_are_two_jobs(self, db):
        JS.enqueue([SHEET], owner="a", page=0, db_path=db)
        JS.enqueue([SHEET], owner="a", page=1, db_path=db)
        JS.enqueue([SHEET], owner="a", page=1, db_path=db)      # a duplicate
        assert sorted(j.page for j in JS.list_jobs(db_path=db)) == [0, 1]


class TestAnOlderDatabase:
    """`CREATE TABLE IF NOT EXISTS` never alters a table that is already there,
    so a queue made before pages existed has to be upgraded in place."""

    OLD_SCHEMA = JS.SCHEMA.replace(
        "    page            INTEGER NOT NULL DEFAULT 0,\n", "")

    def test_the_page_column_is_added(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.db"
        assert "page            INTEGER" not in self.OLD_SCHEMA
        with sqlite3.connect(path) as conn:
            conn.executescript(self.OLD_SCHEMA)
            conn.execute(
                "INSERT INTO jobs (id, batch_id, drawing_path, drawing_name, "
                "created_at) VALUES ('j1', 'b', 'x.pdf', 'x.pdf', 1.0)")
        job = JS.get("j1", db_path=path)
        assert job is not None and job.page == 0

    def test_upgrading_twice_is_harmless(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(self.OLD_SCHEMA)
        JS.list_jobs(db_path=path)
        JS.list_jobs(db_path=path)


class TestLatestPerDrawing:
    def test_the_newest_job_wins(self, db):
        first = JS.enqueue([SHEET], owner="a", db_path=db)[0]
        JS.claim_next("w", db_path=db)
        JS.fail(first.id, "boom", db_path=db)
        second = JS.enqueue([SHEET], owner="a", db_path=db)[0]
        latest = JS.latest_by_drawing(owner="a", db_path=db)
        assert latest[(JS.drawing_key(SHEET), 0)].id == second.id

    def test_a_relative_and_an_absolute_path_are_one_drawing(self, db):
        JS.enqueue([SHEET.resolve()], owner="a", db_path=db)
        latest = JS.latest_by_drawing(owner="a", db_path=db)
        assert (JS.drawing_key(SHEET), 0) in latest


class TestOneDrawingOneLiveJob:
    def test_two_spellings_of_one_path_are_one_job(self, db):
        JS.enqueue([SHEET], owner="a", db_path=db)
        JS.enqueue([SHEET.resolve()], owner="a", db_path=db)
        assert len(JS.list_jobs(db_path=db)) == 1

    def test_the_same_drawing_twice_in_one_call_is_one_job(self, db):
        JS.enqueue([SHEET, SHEET.resolve(), SHEET], owner="a", db_path=db)
        assert len(JS.list_jobs(db_path=db)) == 1
