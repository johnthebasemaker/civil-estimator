"""The status badge on each drawing (core/drawing_status.py)."""
from __future__ import annotations

import pytest

from core import drawing_status as DS
from core import jobstore as JS


def _job(state: str, pct: float = 0.0) -> JS.Job:
    return JS.Job(id="j", batch_id="b", owner="shared", drawing_path="x.pdf",
                  drawing_name="x.pdf", state=state, progress_pct=pct)


class TestEachState:
    def test_never_read(self):
        assert DS.status_for(latest=None, has_saved=False).key == DS.NOT_READ

    def test_waiting(self):
        assert DS.status_for(latest=_job(JS.QUEUED), has_saved=False).key == DS.QUEUED

    def test_being_read_shows_how_far(self):
        s = DS.status_for(latest=_job(JS.RUNNING, 42.4), has_saved=False)
        assert s.key == DS.READING and s.label == "Reading 42%"

    def test_read_with_nothing_flagged(self):
        s = DS.status_for(latest=_job(JS.DONE), has_saved=True, attention=0)
        assert s.key == DS.READY

    def test_read_with_gaps_needs_review(self):
        s = DS.status_for(latest=_job(JS.DONE), has_saved=True, attention=3)
        assert s.key == DS.NEEDS_REVIEW
        assert "3" in s.detail

    def test_failed(self):
        assert DS.status_for(latest=_job(JS.FAILED), has_saved=False).key == DS.FAILED

    def test_a_saved_reading_counts_even_with_the_queue_cleared(self):
        """Clearing the queue forgets the rows, not the readings."""
        assert DS.status_for(latest=None, has_saved=True).key == DS.READY


class TestWhatWins:
    def test_a_re_read_in_progress_shows_as_reading(self):
        s = DS.status_for(latest=_job(JS.RUNNING, 10), has_saved=True)
        assert s.key == DS.READING

    def test_a_failed_re_read_is_not_hidden_behind_the_old_reading(self):
        s = DS.status_for(latest=_job(JS.FAILED), has_saved=True)
        assert s.key == DS.FAILED

    def test_a_cancelled_attempt_is_no_news(self):
        assert DS.status_for(latest=_job(JS.CANCELLED), has_saved=True).key == DS.READY
        assert DS.status_for(latest=_job(JS.CANCELLED), has_saved=False).key == DS.NOT_READ

    def test_a_done_job_without_a_saved_reading_is_not_read(self):
        """The reading belongs to an older version of the file."""
        assert DS.status_for(latest=_job(JS.DONE), has_saved=False).key == DS.NOT_READ


class TestLabelsAndColours:
    @pytest.mark.parametrize("key", list(DS.LABELS))
    def test_every_state_has_a_label_and_a_tone(self, key):
        s = DS.Status(key)
        assert s.label and s.tone in {"idle", "busy", "warn", "good", "bad"}

    def test_the_labels_asked_for(self):
        assert {DS.LABELS[k] for k in DS.LABELS} >= {
            "Not read", "Queued", "Ready", "Needs review", "Failed"}

    def test_tally_is_worst_first_and_skips_zeros(self):
        counts = DS.tally([DS.Status(DS.READY), DS.Status(DS.FAILED),
                           DS.Status(DS.READY)])
        assert list(counts) == [DS.FAILED, DS.READY]
        assert counts[DS.READY] == 2
