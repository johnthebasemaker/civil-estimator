"""Reuse of saved extractions.

The pipeline wrote an extraction JSON after every run and never read one back,
so re-opening a drawing cost the full model time again. The risk in fixing that
is serving stale numbers, which is why these tests care more about when the
cache *misses* than when it hits.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import extract_cache as C
from core import jobstore as JS
from extractors.models import ExtractionResult, TitleBlockExtraction

SHEET = Path("Drawings/MD-522-8110-EG-CV-LAD-0101_C01.pdf")
pytestmark = pytest.mark.skipif(not SHEET.exists(), reason="drawings not present")


def _result(drawing_no="MD-522-8110-EG-CV-LAD-0101") -> ExtractionResult:
    return ExtractionResult(
        source_pdf=str(SHEET), model="qwen2.5vl:7b",
        title_block=TitleBlockExtraction(drawing_no=drawing_no, revision="C01"),
        transcribed_lines=["30 THK GROUT"], used_text_layer=True)


class TestRoundTrip:
    def test_what_goes_in_comes_back(self, tmp_path):
        key = JS.fingerprint(SHEET)
        C.store(key, _result(), cache_dir=tmp_path)
        back = C.load(key, cache_dir=tmp_path)
        assert back is not None
        assert back.title_block.drawing_no == "MD-522-8110-EG-CV-LAD-0101"
        assert back.transcribed_lines == ["30 THK GROUT"]

    def test_a_miss_is_none_not_an_error(self, tmp_path):
        assert C.load("nothing-here", cache_dir=tmp_path) is None

    def test_a_damaged_entry_falls_back_to_extracting_again(self, tmp_path):
        """Better a slow correct read than a fast crash."""
        (tmp_path / "broken.json").write_text("{ this is not json")
        assert C.load("broken", cache_dir=tmp_path) is None

    def test_the_readable_copy_is_kept_for_the_rest_of_the_tooling(self, tmp_path):
        """bin/rebuild_set.py globs that directory; hex filenames are no use
        to someone opening one by hand."""
        named = tmp_path / "named" / "X_extraction.json"
        C.store("abc", _result(), cache_dir=tmp_path, also_named=named)
        assert named.is_file()
        assert "MD-522-8110-EG-CV-LAD-0101" in named.read_text()


class TestStaleness:
    def test_a_reissued_drawing_does_not_hit_the_old_entry(self, tmp_path):
        cache = tmp_path / "cache"
        original = JS.fingerprint(SHEET)
        C.store(original, _result(), cache_dir=cache)

        reissued = tmp_path / SHEET.name
        reissued.write_bytes(SHEET.read_bytes() + b"%rev C02")
        assert C.load(JS.fingerprint(reissued), cache_dir=cache) is None

    def test_the_same_drawing_under_another_name_does_hit(self, tmp_path):
        cache = tmp_path / "cache"
        C.store(JS.fingerprint(SHEET), _result(), cache_dir=cache)
        copied = tmp_path / "copied-for-the-client.pdf"
        copied.write_bytes(SHEET.read_bytes())
        assert C.load(JS.fingerprint(copied), cache_dir=cache) is not None


class TestBackfill:
    def test_it_adopts_an_old_extraction_whose_drawing_is_unchanged(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / f"{SHEET.stem}_extraction.json").write_text(
            _result().model_dump_json())
        cache = tmp_path / "cache"
        report = C.backfill(["Drawings"], cache_dir=cache, output_dir=out)
        assert report["linked"] == 1
        assert C.load(JS.fingerprint(SHEET), cache_dir=cache) is not None

    def test_it_skips_an_extraction_whose_drawing_is_gone(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "SOMETHING-WE-NO-LONGER-HAVE_extraction.json").write_text(
            _result().model_dump_json())
        report = C.backfill(["Drawings"], cache_dir=tmp_path / "cache",
                            output_dir=out)
        assert report["linked"] == 0 and report["skipped"] == 1
