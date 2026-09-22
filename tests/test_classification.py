"""Which site condition a drawing belongs to.

The classification is an input rather than something read off the sheet:
nothing on a drawing reliably says whether the ground is virgin, live, or being
made good, and the three carry different rates. So the tests here are mostly
about the store not losing what somebody filed.
"""
from __future__ import annotations

import json

import pytest

from core import classification as C


@pytest.fixture
def store(tmp_path):
    return tmp_path / "classifications.json"


class TestRoundTrip:
    def test_what_is_filed_comes_back(self, store):
        C.set_for("Drawings/A.pdf", C.BROWN_FIELD, store=store)
        assert C.get("Drawings/A.pdf", store=store) == C.BROWN_FIELD

    def test_an_unfiled_drawing_gets_the_default(self, store):
        assert C.get("Drawings/never-seen.pdf", store=store) == C.DEFAULT

    def test_only_the_three_values_are_accepted(self, store):
        with pytest.raises(ValueError):
            C.set_for("Drawings/A.pdf", "Somewhere else", store=store)

    def test_a_value_that_is_no_longer_offered_falls_back(self, store):
        store.write_text(json.dumps({"A.pdf": "Retired category"}))
        assert C.get("Drawings/A.pdf", store=store) == C.DEFAULT


class TestTheSameDrawingInTwoFolders:
    """The app copies an upload into output/uploads, so one drawing routinely
    exists twice. Keying on the path made those two different drawings, and a
    classification set in the browser never reached the rebuilt workbook."""

    def test_a_copy_elsewhere_reads_the_same(self, store):
        C.set_for("Drawings/MD-0101.pdf", C.REPAIR, store=store)
        assert C.get("output/uploads/MD-0101.pdf", store=store) == C.REPAIR

    def test_an_absolute_path_reads_the_same(self, store):
        C.set_for("Drawings/MD-0101.pdf", C.REPAIR, store=store)
        assert C.get("/somewhere/else/MD-0101.pdf", store=store) == C.REPAIR

    def test_entries_written_under_the_old_path_key_still_work(self, store):
        """An upgrade must not quietly lose what was already filed."""
        store.write_text(json.dumps(
            {"/Users/x/civil-estimator/Drawings/MD-0101.pdf": C.BROWN_FIELD}))
        assert C.get("Drawings/MD-0101.pdf", store=store) == C.BROWN_FIELD


class TestDurability:
    def test_a_damaged_file_does_not_stop_a_workbook_being_written(self, store):
        store.write_text("{ not json at all")
        assert C.get("Drawings/A.pdf", store=store) == C.DEFAULT

    def test_filing_one_drawing_leaves_the_others_alone(self, store):
        C.set_for("A.pdf", C.BROWN_FIELD, store=store)
        C.set_for("B.pdf", C.REPAIR, store=store)
        assert C.get("A.pdf", store=store) == C.BROWN_FIELD
        assert C.get("B.pdf", store=store) == C.REPAIR

    def test_refiling_replaces_rather_than_appends(self, store):
        C.set_for("A.pdf", C.BROWN_FIELD, store=store)
        C.set_for("A.pdf", C.GREEN_FIELD, store=store)
        assert C.load(store) == {"A.pdf": C.GREEN_FIELD}

    def test_forgetting_a_drawing_returns_it_to_the_default(self, store):
        C.set_for("A.pdf", C.REPAIR, store=store)
        C.forget("A.pdf", store=store)
        assert C.get("A.pdf", store=store) == C.DEFAULT


class TestTheStoreCanMove:
    """A server keeps it on a data volume; the test suite keeps it in a temp
    folder, so running the tests never refiles someone's real drawings."""

    def test_the_environment_variable_is_honoured(self, tmp_path, monkeypatch):
        target = tmp_path / "elsewhere.json"
        monkeypatch.setenv(C.STORE_ENV, str(target))
        C.set_for("Drawings/A.pdf", C.REPAIR)
        assert target.exists()
        assert C.get("Drawings/A.pdf") == C.REPAIR

    def test_the_suite_never_writes_the_real_store(self):
        assert C.store_path() != C.STORE, \
            "tests/conftest.py should point every test at its own store"
