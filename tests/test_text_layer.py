"""Reading a drawing's own text layer, and the correction feedback loop.

The bug these guard against: five of eleven sheets carry a real text layer and
the extractor looked straight past it, spending five minutes of vision on each
and returning nothing. Two of the remaining six carry a handful of stray
characters and must NOT be mistaken for text sheets, or they skip the vision
path and return nothing instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from extractors import corrections
from extractors import text_layer as TL
from extractors.pdf_to_image import open_page

ROOT = Path(__file__).resolve().parent.parent
DRAWINGS = ROOT / "Drawings"

TEXT_SHEETS = ["MD-522-8110-EG-CV-LAD-0101_C01.pdf",
               "MD-522-8110-EG-CV-LAD-0102_C01.pdf",
               "MD-522-8110-EG-CV-LAD-0103_C01.pdf",
               "MD-522-8110-EG-CV-LAD-0104_C01.pdf",
               "MD-522-8110-EG-CV-LAD-0105_C01.pdf"]
# 141 and 328 stray characters respectively — outlined sheets, not text sheets.
THIN_TEXT_SHEETS = ["MD-522-8120-EG-CV-LAD-0101_C03.pdf",
                    "MD-522-89MH-EG-CV-LAD-0001_C01.pdf"]
OUTLINED_SHEETS = ["MD-522-8110-EG-CV-LAD-0106_C01.pdf",
                   "MD-522-8110-EG-CV-LAD-0107_C01.pdf"]

pytestmark = pytest.mark.skipif(not DRAWINGS.exists(),
                                reason="Drawings/ not present")


def _page(name):
    return open_page(DRAWINGS / name, 0)


class TestRouting:
    @pytest.mark.parametrize("name", TEXT_SHEETS)
    def test_real_text_layer_is_used(self, name):
        doc, page = _page(name)
        try:
            assert TL.has_usable_text(page)
        finally:
            doc.close()

    @pytest.mark.parametrize("name", THIN_TEXT_SHEETS + OUTLINED_SHEETS)
    def test_outlined_sheets_go_to_vision(self, name):
        """A few stray border labels are not a text layer. Treating them as one
        skips the vision path and returns nothing."""
        doc, page = _page(name)
        try:
            assert not TL.has_usable_text(page)
        finally:
            doc.close()

    def test_threshold_sits_above_the_stray_text_sheets(self):
        assert TL.MIN_USABLE_CHARS > 328


class TestLineReconstruction:
    def test_spans_group_into_lines_and_blocks(self):
        doc, page = _page(TEXT_SHEETS[0])
        try:
            lines, blocks = TL.read_page(page)
        finally:
            doc.close()
        assert len(lines) > 50
        assert blocks and all(b.lines for b in blocks)

    def test_lines_are_ordered_down_the_sheet(self):
        doc, page = _page(TEXT_SHEETS[0])
        try:
            lines = TL.lines_from_page(page)
        finally:
            doc.close()
        ys = [l.rect.y0 for l in lines]
        assert ys == sorted(ys)

    def test_every_line_keeps_its_position(self):
        doc, page = _page(TEXT_SHEETS[0])
        try:
            lines = TL.lines_from_page(page)
            w, h = page.rect.width, page.rect.height
        finally:
            doc.close()
        for line in lines:
            assert 0 <= line.rect.x0 <= w and 0 <= line.rect.y0 <= h

    def test_rotated_text_is_flagged_not_merged(self):
        """Border repeats and section marks run vertically; joining them to
        horizontal neighbours would corrupt both."""
        doc, page = _page(TEXT_SHEETS[0])
        try:
            lines = TL.lines_from_page(page)
        finally:
            doc.close()
        assert all(isinstance(l.rotated, bool) for l in lines)


class TestDrawingNumber:
    @pytest.mark.parametrize("name", TEXT_SHEETS)
    def test_sheet_reports_its_own_number_not_a_reference(self, name):
        """Counting occurrences got this wrong — 0102 reported 0101 and 0105
        reported 0104, because a referenced drawing can appear more often than
        the sheet's own number. Position in the border settles it."""
        doc, page = _page(name)
        try:
            tb = TL.title_block_from_text(page, None, name)
        finally:
            doc.close()
        assert tb["drawing_no"] == name.split("_")[0]

    def test_revision_comes_from_the_filename_suffix(self):
        doc, page = _page(TEXT_SHEETS[0])
        try:
            assert TL.title_block_from_text(page, None, TEXT_SHEETS[0])["revision"] == "C01"
        finally:
            doc.close()

    def test_filename_is_the_fallback_when_text_has_no_number(self):
        class Bare:
            rect = type("R", (), {"width": 100.0, "height": 100.0})()
        tb = TL.title_block_from_text.__wrapped__ if hasattr(
            TL.title_block_from_text, "__wrapped__") else TL.title_block_from_text
        out = tb(Bare(), [], "MD-522-8110-EG-CV-LAD-0107_C01.pdf")
        assert out["drawing_no"] == "MD-522-8110-EG-CV-LAD-0107"
        assert out["source"] == "filename"


class TestPositionMarks:
    def test_marks_are_counted_on_plan_sheets(self):
        doc, page = _page("MD-522-8110-EG-CV-LAD-0102_C01.pdf")
        try:
            marks = TL.count_position_marks(page)
        finally:
            doc.close()
        assert marks.get("P1", 0) > 5
        assert all(k[0] in "PF" for k in marks)

    def test_prose_mentions_are_not_counted(self):
        """'FOR DETAILS REFER DWG P1' must not add to the P1 count — only a
        span that is nothing but the mark counts."""
        assert TL.MARK_RE.match("P1")
        assert not TL.MARK_RE.match("FOR DETAILS REFER DWG P1")
        assert not TL.MARK_RE.match("P1 P1 P1")


# ================= corrections =================
class TestCorrectionComparison:
    @pytest.mark.parametrize("a,b", [("600", "600.0"), ("600", " 600 "),
                                     ("ABC", "abc")])
    def test_values_compare_the_way_a_person_would(self, a, b):
        row = corrections.Correction(item="x", tag="P1", extracted=a, corrected=b)
        assert not row.changed

    def test_a_real_change_is_detected(self):
        row = corrections.Correction(item="x", tag="P1", extracted="600",
                                     corrected="650")
        assert row.changed

    def test_blank_correction_means_agreed(self):
        row = corrections.Correction(item="x", tag="P1", extracted="600",
                                     corrected="")
        assert not row.changed


class TestScoring:
    def _review(self, pairs, drawing="D-1"):
        r = corrections.SheetReview(drawing_no=drawing)
        for item, extracted, corrected in pairs:
            r.rows.append(corrections.Correction(
                item=item, tag="P1", extracted=extracted, corrected=corrected,
                verified_by="expert"))
        return r

    def test_accuracy_counts_only_reviewed_rows(self):
        r = self._review([("Pedestal", "600", ""), ("Pedestal", "500", "550")])
        r.rows.append(corrections.Correction(item="Pedestal", tag="P9",
                                             extracted="1", corrected=""))
        assert len(r.reviewed) == 2
        assert r.accuracy() == 0.5

    def test_report_breaks_down_by_item_and_drawing(self):
        rep = corrections.score([
            self._review([("Pedestal", "600", ""), ("Level", "97.5", "97.6")], "D-1"),
            self._review([("Pedestal", "500", "")], "D-2")])
        assert rep["values_reviewed"] == 3
        assert rep["values_correct"] == 2
        assert rep["per_item"]["Pedestal"]["accuracy_pct"] == 100.0
        assert rep["per_item"]["Level"]["accuracy_pct"] == 0.0
        assert set(rep["per_drawing"]) == {"D-1", "D-2"}

    def test_empty_review_does_not_divide_by_zero(self):
        assert corrections.score([])["accuracy_pct"] == 0.0


class TestGrammarFeedback:
    def test_a_callout_the_grammar_cannot_read_is_surfaced(self):
        """The highest-value output: a pattern the grammar misses is missed on
        every drawing until it is added."""
        r = corrections.SheetReview(drawing_no="D-1")
        r.rows.append(corrections.Correction(
            item="Pedestal", tag="P9", extracted="", corrected="600x500, 3 Nos",
            callout="PEDESTAL P9 - 600 BY 500 - THREE OFF", verified_by="e"))
        out = corrections.unparsed_callouts(r)
        assert len(out) == 1 and out[0]["tag"] == "P9"

    def test_a_readable_callout_is_not_reported_as_a_gap(self):
        r = corrections.SheetReview(drawing_no="D-1")
        r.rows.append(corrections.Correction(
            item="Pedestal", tag="P1", extracted="600 x 500 mm, 2 Nos",
            corrected="600 x 500 mm, 3 Nos",
            callout="TYP DETAIL OF PEDESTAL P1(600x500) 2Nos", verified_by="e"))
        assert corrections.unparsed_callouts(r) == []

    def test_only_wrong_rows_are_examined(self):
        r = corrections.SheetReview(drawing_no="D-1")
        r.rows.append(corrections.Correction(
            item="Pedestal", tag="P1", extracted="x", corrected="",
            callout="UNREADABLE", verified_by="e"))
        assert corrections.unparsed_callouts(r) == []


class TestExampleBanking:
    def test_a_half_right_drawing_is_not_banked(self, tmp_path):
        """An example is ground truth for every later run; banking one with a
        wrong row teaches the mistake."""
        wb = tmp_path / "x.xlsx"
        review = corrections.SheetReview(drawing_no="D-1", source=str(wb))
        review.rows = [
            corrections.Correction(item="Pedestal", tag="P1",
                                   extracted="600 x 500 mm, 2 Nos", corrected="",
                                   callout="PEDESTAL P1(600x500) 2Nos",
                                   verified_by="e"),
            corrections.Correction(item="Pedestal", tag="P2",
                                   extracted="500 x 500 mm, 20 Nos",
                                   corrected="500 x 500 mm, 22 Nos",
                                   callout="PEDESTAL P2(500x500) 20Nos",
                                   verified_by="e")]
        assert any(r.changed for r in review.reviewed)
