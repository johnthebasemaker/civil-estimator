"""The drawing viewer (ui/workspace/viewer.py).

What it has to get right: which values can be pointed at on the sheet, how each
one was obtained, that a number means the same thing on the sheet, in the table
and in the workbook, and that the region redrawn around a value is big enough to
read without running off the page.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from extractors import verification as VER
from extractors.models import (
    ExtractionResult, GradeSlabExtraction, PedestalExtraction,
    TitleBlockExtraction,
)
from ui.workspace import viewer as VW


def _result(*, used_text_layer: bool = False) -> ExtractionResult:
    return ExtractionResult(
        source_pdf="x.pdf", model="qwen2.5vl:7b", profile="thorough",
        used_text_layer=used_text_layer,
        title_block=TitleBlockExtraction(drawing_no="MD-0107", revision="C01",
                                         confidence="high"),
        pedestals=[
            PedestalExtraction(tag="P1", length_mm=600, width_mm=500, quantity=2,
                               raw_text="P1(600x500) 2Nos", confidence="high",
                               source_rect=[0.40, 0.30, 0.46, 0.33]),
            PedestalExtraction(tag="P2", length_mm=500, width_mm=500, quantity=20,
                               height_mm=800, raw_text="P2(500x500) 20Nos",
                               confidence="medium", source_rect=[]),
        ],
        grade_slabs=[GradeSlabExtraction(tag="GS-01", length_mm=24430,
                                         width_mm=8600, thickness_mm=300,
                                         confidence="medium")],
    )


class TestWhatCameFromWhere:
    def test_a_text_layer_sheet_is_exact(self):
        items = VW.evidence(_result(used_text_layer=True))
        title = next(i for i in items if i.kind == "Title block")
        assert title.source == VW.EXACT

    def test_otherwise_the_model_read_it(self):
        title = next(i for i in VW.evidence(_result()) if i.kind == "Title block")
        assert title.source == VW.MODEL

    def test_a_value_the_drawing_left_short_is_flagged(self):
        """A pedestal callout gives the plan size and a count, never a height."""
        p1 = next(i for i in VW.evidence(_result()) if i.label == "P1")
        assert p1.source == VW.INCOMPLETE
        assert "height" in p1.note.lower()

    def test_a_complete_value_is_not_flagged(self):
        p2 = next(i for i in VW.evidence(_result()) if i.label == "P2")
        assert p2.source == VW.MODEL

    def test_the_value_itself_is_never_called_assumed(self):
        """The dimensions were read. Only the missing figure is missing — saying
        the whole row was assumed would libel a number that is correct."""
        p1 = next(i for i in VW.evidence(_result()) if i.label == "P1")
        assert "600" in p1.extracted and "500" in p1.extracted
        assert VW.SOURCE_LABEL[p1.source].startswith("Read")

    @pytest.mark.parametrize("source", list(VW.SOURCE_ORDER))
    def test_every_source_has_a_label_and_a_colour(self, source):
        assert VW.SOURCE_LABEL[source] and len(VW.SOURCE_COLOUR[source]) == 3


class TestNumbering:
    def test_the_numbers_match_the_workbook(self):
        """Box 7 on the sheet is row 7 in the table and row #7 on the
        Verification sheet. They are read side by side."""
        result = _result()
        rows = VER.verification_rows(result)
        for item in VW.evidence(result):
            sheet_row = rows[item.number - 1]
            assert sheet_row[0] == item.number
            assert sheet_row[1] == item.kind and sheet_row[2] == item.label

    def test_the_check_print_agrees_too(self, tmp_path):
        """It used to number only the boxed items, so its "1." was the
        Verification sheet's #6."""
        import inspect
        source = inspect.getsource(VER.build_check_print)
        assert "for n, item in enumerate(all_items, start=1)" in source


class TestWhichOnesCanBePointedAt:
    def test_only_values_with_a_rectangle(self):
        items = VW.evidence(_result())
        assert [i.label for i in VW.traceable(items)] == ["P1"]

    def test_the_rest_still_appear_in_the_table(self):
        rows = VW.table_rows(VW.evidence(_result()))
        assert len(rows) == len(VW.evidence(_result()))
        assert any(r["Item"].startswith("Title block") for r in rows)

    def test_boxed_values_come_first(self):
        rows = VW.table_rows(VW.evidence(_result()))
        assert rows[0]["On sheet"] and not rows[-1]["On sheet"]

    def test_the_boxes_are_plain_hashable_data(self):
        """They are a cache key for the rendered overlay."""
        items = VW.evidence(_result())
        boxes = VW.boxes_of(items)
        p1 = next(i for i in items if i.label == "P1")
        assert hash(boxes)                       # it is a cache key
        assert boxes == ((p1.number, VW.INCOMPLETE, p1.rect),)

    def test_counts_are_reported_in_a_fixed_order(self):
        tally = VW.counts(VW.evidence(_result()))
        assert list(tally) == [k for k in VW.SOURCE_ORDER if k in tally]


class TestTheRegionRedrawnAroundAValue:
    TINY = (0.500, 0.500, 0.504, 0.502)

    def test_a_sliver_is_given_room(self):
        region = VW.crop_region(self.TINY)
        assert region.width >= 0.07 and region.height >= 0.07

    def test_it_stays_on_the_page(self):
        for rect in ((0.0, 0.0, 0.02, 0.02), (0.98, 0.98, 1.0, 1.0)):
            region = VW.crop_region(rect)
            assert 0.0 <= region.x0 < region.x1 <= 1.0
            assert 0.0 <= region.y0 < region.y1 <= 1.0

    def test_more_padding_shows_more_of_the_sheet(self):
        tight = VW.crop_region((0.3, 0.3, 0.4, 0.4), pad=0.25)
        wide = VW.crop_region((0.3, 0.3, 0.4, 0.4), pad=1.4)
        assert wide.width > tight.width

    def test_it_still_contains_the_value(self):
        rect = (0.30, 0.30, 0.40, 0.40)
        region = VW.crop_region(rect)
        assert region.x0 <= rect[0] and region.x1 >= rect[2]
        assert region.y0 <= rect[1] and region.y1 >= rect[3]


class TestDrawing:
    def _png(self, size=(400, 300)) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", size, "white").save(buffer, format="PNG")
        return buffer.getvalue()

    def test_boxes_are_drawn_on_the_sheet(self):
        boxes = VW.boxes_of(VW.evidence(_result()))
        out = VW.draw_boxes(self._png(), boxes)
        img = Image.open(io.BytesIO(out))
        assert img.size == (400, 300)
        assert img.convert("RGB").getcolors(maxcolors=1 << 16) is not None
        assert len(set(img.convert("RGB").getdata())) > 1, "nothing was drawn"

    def test_a_sheet_with_nothing_located_still_renders(self):
        out = VW.draw_boxes(self._png(), ())
        assert Image.open(io.BytesIO(out)).size == (400, 300)

    def test_the_trace_marks_the_value_inside_the_region(self):
        rect = (0.30, 0.30, 0.40, 0.40)
        region = VW.crop_region(rect)
        out = VW.draw_trace(self._png(), rect, region, VW.MODEL)
        assert len(set(Image.open(io.BytesIO(out)).convert("RGB").getdata())) > 1
