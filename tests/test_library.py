"""Where the app finds drawings (core/library.py).

The failure this guards: the page listed its uploads folder and the project
root, never `Drawings/`. When the uploads were cleared the app showed one
drawing while the set of eleven sat next to it, and 22 page tests went red
because the drawing each one ticked was no longer on the list.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import library as LIB


def _pdf(folder: Path, name: str, body: bytes = b"%PDF-1.4\n") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(body + name.encode())
    return path


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    uploads, drawings = tmp_path / "uploads", tmp_path / "Drawings"
    uploads.mkdir()
    drawings.mkdir()
    monkeypatch.setenv(LIB.UPLOAD_ENV, str(uploads))
    monkeypatch.setenv(LIB.DIRS_ENV, str(drawings))
    return uploads, drawings


class TestDefaults:
    def test_the_drawings_folder_is_searched_by_default(self, monkeypatch):
        monkeypatch.delenv(LIB.DIRS_ENV, raising=False)
        assert Path("Drawings") in LIB.drawing_dirs()

    def test_uploads_come_first(self, monkeypatch):
        monkeypatch.delenv(LIB.DIRS_ENV, raising=False)
        monkeypatch.delenv(LIB.UPLOAD_ENV, raising=False)
        assert LIB.drawing_dirs()[0] == LIB.UPLOAD_DIR

    def test_both_lists_can_be_moved(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LIB.UPLOAD_ENV, str(tmp_path / "u"))
        monkeypatch.setenv(LIB.DIRS_ENV, f"{tmp_path / 'a'}:{tmp_path / 'b'}")
        assert LIB.drawing_dirs() == [tmp_path / "u", tmp_path / "a", tmp_path / "b"]


class TestFinding:
    def test_drawings_in_every_folder_are_found(self, dirs):
        uploads, drawings = dirs
        _pdf(uploads, "U1.pdf")
        _pdf(drawings, "D1.pdf")
        assert [d.name for d in LIB.find()] == ["U1.pdf", "D1.pdf"]

    def test_only_uploads_are_deletable(self, dirs):
        uploads, drawings = dirs
        _pdf(uploads, "U1.pdf")
        _pdf(drawings, "D1.pdf")
        by_name = {d.name: d for d in LIB.find()}
        assert by_name["U1.pdf"].deletable is True
        assert by_name["D1.pdf"].deletable is False

    def test_each_folder_is_named_for_people(self, dirs):
        uploads, drawings = dirs
        _pdf(uploads, "U1.pdf")
        _pdf(drawings, "D1.pdf")
        assert [d.folder for d in LIB.find()] == ["uploaded", "Drawings folder"]

    def test_a_missing_folder_is_not_an_error(self, dirs, tmp_path, monkeypatch):
        monkeypatch.setenv(LIB.DIRS_ENV, str(tmp_path / "nowhere"))
        assert LIB.find() == []

    def test_hidden_files_and_other_types_are_ignored(self, dirs):
        _, drawings = dirs
        _pdf(drawings, ".~lock.pdf")
        (drawings / "notes.txt").write_text("x")
        _pdf(drawings, "D1.pdf")
        assert [d.name for d in LIB.find()] == ["D1.pdf"]

    def test_sorted_by_name_within_a_folder(self, dirs):
        _, drawings = dirs
        for name in ("b.pdf", "A.pdf", "c.pdf"):
            _pdf(drawings, name)
        assert [d.name for d in LIB.find()] == ["A.pdf", "b.pdf", "c.pdf"]


class TestEachFileOnce:
    def test_a_symlink_to_a_listed_file_is_not_listed_again(self, dirs):
        uploads, drawings = dirs
        real = _pdf(drawings, "D1.pdf")
        (uploads / "link.pdf").symlink_to(real)
        assert len(LIB.find()) == 1

    def test_the_same_folder_named_twice_lists_once(self, dirs, monkeypatch):
        _, drawings = dirs
        _pdf(drawings, "D1.pdf")
        monkeypatch.setenv(LIB.DIRS_ENV, f"{drawings}:{drawings}")
        assert len(LIB.find()) == 1

    def test_two_different_files_with_one_name_are_both_listed(self, dirs):
        uploads, drawings = dirs
        _pdf(uploads, "X.pdf", b"%PDF-1.4 new\n")
        _pdf(drawings, "X.pdf", b"%PDF-1.4 old\n")
        assert len(LIB.find()) == 2


class TestLabels:
    def test_a_unique_name_is_shown_as_it_is(self, dirs):
        _, drawings = dirs
        _pdf(drawings, "D1.pdf")
        found = LIB.find()
        assert LIB.labels(found)[found[0].path] == "D1.pdf"

    def test_a_shared_name_says_where_each_one_is(self, dirs):
        uploads, drawings = dirs
        _pdf(uploads, "X.pdf", b"%PDF new\n")
        _pdf(drawings, "X.pdf", b"%PDF old\n")
        labels = sorted(LIB.labels(LIB.find()).values())
        assert labels == ["X.pdf  ·  Drawings folder", "X.pdf  ·  uploaded"]
