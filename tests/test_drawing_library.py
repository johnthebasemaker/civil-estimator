"""The drawing library on the Extract page: ticking, select-all, clear, delete.

The bug these guard against: `st.file_uploader` hands back its whole file list
on *every* rerun, not just the run the files arrived on. Re-applying that list
each time re-ticked every uploaded drawing, so unticking one was undone
immediately and "Clear" was reverted before it could be seen.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ui.auth import SESSION_KEY as AUTH_KEY

ROOT = Path(__file__).resolve().parent.parent
PAGE = str(ROOT / "pages" / "0_Extract.py")
from tests.sample_drawing import sample_pdf

SAMPLE = sample_pdf()

pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="sample drawing missing")


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """An uploads folder and a drawings folder of the test's own.

    These tests used to copy their stand-ins into the real `output/uploads`,
    which meant every run briefly put three fake drawings in front of anyone
    using the app — and the tests' own counts depended on what else was there.
    """
    uploads, drawings = tmp_path / "uploads", tmp_path / "Drawings"
    uploads.mkdir()
    drawings.mkdir()
    monkeypatch.setenv("CIVIL_ESTIMATOR_UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("CIVIL_ESTIMATOR_DRAWING_DIRS", str(drawings))
    monkeypatch.setenv("CIVIL_ESTIMATOR_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("CIVIL_ESTIMATOR_CACHE_DIR", str(tmp_path / "cache"))
    return uploads, drawings


@pytest.fixture
def library(folders):
    """Three stand-in uploads, plus one drawing in the Drawings folder.

    Real copies, not symlinks: the library lists each file once by identity, so
    three links to one file would rightly show as one drawing.
    """
    uploads, drawings = folders
    names = [f"ZZ-TEST-{i}.pdf" for i in range(1, 4)]
    for name in names:
        shutil.copy(SAMPLE, uploads / name)
    (drawings / "ZZ-KEPT.pdf").symlink_to(SAMPLE.resolve())
    return names


def _run():
    at = AppTest.from_file(PAGE, default_timeout=300)
    at.session_state[AUTH_KEY] = True
    return at.run()


def _boxes(at, names):
    return [c for c in at.checkbox if any(c.label.startswith(n) for n in names)]


class TestSelection:
    def test_a_drawing_can_be_ticked(self, library):
        at = _run()
        box = _boxes(at, library)[0]
        box.set_value(True).run()
        assert _boxes(at, library)[0].value is True

    def test_a_ticked_drawing_can_be_unticked(self, library):
        """The reported bug: unticking was undone on the next rerun."""
        at = _run()
        _boxes(at, library)[0].set_value(True).run()
        assert _boxes(at, library)[0].value is True
        _boxes(at, library)[0].set_value(False).run()
        assert _boxes(at, library)[0].value is False

    def test_a_few_can_be_ticked_out_of_many(self, library):
        at = _run()
        boxes = _boxes(at, library)
        boxes[0].set_value(True).run()
        _boxes(at, library)[2].set_value(True).run()
        values = [b.value for b in _boxes(at, library)]
        assert values == [True, False, True]

    def test_selection_survives_an_unrelated_rerun(self, library):
        at = _run()
        _boxes(at, library)[1].set_value(True).run()
        at.run()                       # a rerun that touches nothing
        assert [b.value for b in _boxes(at, library)] == [False, True, False]


class TestBulkButtons:
    def _button(self, at, label):
        return next(b for b in at.button if b.label == label)

    def test_select_all_ticks_every_drawing(self, library):
        at = _run()
        self._button(at, "Select all").click().run()
        assert all(b.value for b in _boxes(at, library))

    def test_clear_unticks_every_drawing(self, library):
        """Clear was reverted immediately by the uploader re-applying its list."""
        at = _run()
        self._button(at, "Select all").click().run()
        assert any(b.value for b in _boxes(at, library))
        self._button(at, "Clear").click().run()
        assert not any(b.value for b in _boxes(at, library))

    def test_select_all_then_untick_one(self, library):
        at = _run()
        self._button(at, "Select all").click().run()
        _boxes(at, library)[0].set_value(False).run()
        values = [b.value for b in _boxes(at, library)]
        assert values[0] is False and all(values[1:])


class TestDeleteGuards:
    def test_delete_asks_before_removing_anything(self, library):
        at = _run()
        self._button = TestBulkButtons()._button
        next(b for b in at.button if b.label == "Select all").click().run()
        delete = next((b for b in at.button if b.label.startswith("🗑")), None)
        assert delete is not None
        delete.click().run()
        assert any("permanently" in w.value for w in at.warning)
        # nothing removed until confirmed
        uploads = Path(os.environ["CIVIL_ESTIMATOR_UPLOAD_DIR"])
        assert all((uploads / n).exists() for n in library)

    def test_cancel_leaves_every_file_in_place(self, library):
        at = _run()
        next(b for b in at.button if b.label == "Select all").click().run()
        next(b for b in at.button if b.label.startswith("🗑")).click().run()
        next(b for b in at.button if b.label == "Cancel").click().run()
        uploads = Path(os.environ["CIVIL_ESTIMATOR_UPLOAD_DIR"])
        assert all((uploads / n).exists() for n in library)

    def test_drawings_outside_uploads_are_never_offered_for_deletion(self, library):
        """This page manages its own uploads; quietly removing a drawing someone
        put in the Drawings folder or the project is not its business."""
        at = _run()
        next(b for b in at.button if b.label == "Select all").click().run()
        assert any(c.label.startswith("ZZ-KEPT") and c.value for c in at.checkbox)
        delete = next(b for b in at.button if b.label.startswith("🗑"))
        count = int("".join(ch for ch in delete.label if ch.isdigit()))
        assert count == len(library)               # the three uploads, not four
        next(b for b in at.button if b.label.startswith("🗑")).click().run()
        next(b for b in at.button if b.label == "Yes, delete").click().run()
        drawings = Path(os.environ["CIVIL_ESTIMATOR_DRAWING_DIRS"])
        assert (drawings / "ZZ-KEPT.pdf").exists()
        assert SAMPLE.exists()


class TestTheDrawingsFolderIsListed:
    """The set lives in Drawings/. The page used to look only at its uploads and
    the project root, so with the uploads cleared it showed one drawing while
    eleven sat beside it."""

    def test_a_drawing_in_the_drawings_folder_is_offered(self, library):
        at = _run()
        assert any(c.label.startswith("ZZ-KEPT") for c in at.checkbox)

    def test_it_says_where_each_drawing_lives(self, library):
        at = _run()
        captions = " ".join(c.value for c in at.caption)
        assert "Drawings folder" in captions and "uploaded" in captions


class TestBatchSwitch:
    def test_several_ticked_drawings_switch_to_the_queue(self, library):
        at = _run()
        next(b for b in at.button if b.label == "Select all").click().run()
        headers = [h.value for h in at.header]
        assert any("Extraction queue" in h for h in headers)

    def test_one_ticked_drawing_keeps_the_single_flow(self, library):
        at = _run()
        _boxes(at, library)[0].set_value(True).run()
        headers = [h.value for h in at.header]
        assert any("Sheet" in h for h in headers)
        assert not any("Extraction queue" in h for h in headers)

    def test_the_queue_offers_to_add_the_ticked_drawings(self, library):
        at = _run()
        next(b for b in at.button if b.label == "Select all").click().run()
        assert any("Add" in b.label and "queue" in b.label for b in at.button)
