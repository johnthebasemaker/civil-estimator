"""Phase 2 extractor tests — Ollama is mocked throughout.

No test in this file touches the network or the GPU: a real qwen2.5vl call is
~50 s, which is not a unit-test budget. The fake client replays canned JSON
keyed off the stage prompt, which is exactly what the real client returns.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.models import GradeSlab, Pedestal, Project
from extractors import callout_grammar as G
from extractors import prompts as P
from extractors import rag_examples
from extractors import qwen_vision as QV
from extractors.qwen_vision import PROFILES
from extractors.models import ExtractionResult, PedestalExtraction, Region
from extractors.ollama_client import GenerateResult, OllamaClient, _salvage_json
from extractors.pdf_to_image import (
    hamming, tile_regions, effective_sheet_px, open_page, page_info,
    render_region, sheet_hash,
)

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PDF = ROOT / "MD-522-8110-EG-CV-LAD-0107_C01.pdf"

# Ground truth, read off the sheet by hand.
TRUTH_PEDESTALS = {
    "P1": (600, 500, 2), "P2": (500, 500, 20), "P3": (350, 350, 11),
    "P6": (450, 450, 6), "P7": (500, 500, 4),
}


# ---------- Fake model host ----------
class FakeClient(OllamaClient):
    """Replays scripted responses instead of calling Ollama."""

    def __init__(self, script: dict[str, list[str]] | None = None,
                 model: str = "qwen2.5vl:7b-fake", truncate: bool = False):
        self.host, self.model, self.timeout_s = "fake://", model, 1
        self.script = script or {}
        self.calls: list[str] = []
        self.truncate = truncate      # emulate the model stopping early

    def _stage_of(self, prompt: str) -> str:
        if "title block" in prompt:
            return "title_block"
        if "foundation layout plan" in prompt:
            return "grade_slab"
        if "montage of" in prompt:
            return "transcribe"
        return "pedestals"

    def generate(self, prompt, images_b64=None, **kw) -> GenerateResult:
        stage = self._stage_of(prompt)
        self.calls.append(stage)
        queue = self.script.get(stage, [])
        body = queue.pop(0) if queue else '{}'
        if stage == "transcribe" and not self.truncate:
            body = self._pad_to_requested(prompt, body)
        return GenerateResult(response=body, elapsed_s=0.01)

    @staticmethod
    def _pad_to_requested(prompt: str, body: str) -> str:
        """Return exactly as many crops as the prompt asked for.

        The real model returns one entry per crop; a scripted body that returns
        fewer would trip the split-on-truncation retry and make call counts
        depend on fixture length rather than on behaviour under test.
        """
        m = re.search(r"montage of (\d+) separate crops", prompt)
        if not m:
            return body
        want = int(m.group(1))
        try:
            crops = json.loads(body).get("crops") or []
        except json.JSONDecodeError:
            return body
        crops = list(crops)[:want]
        crops += [{"lines": []} for _ in range(want - len(crops))]
        for i, c in enumerate(crops, start=1):
            c.setdefault("id", i)
        return json.dumps({"crops": crops})


def _ped_json(*rows) -> str:
    return json.dumps({"pedestals": [
        {"raw_text": r, "tag": t, "length_mm": l, "width_mm": w, "quantity": q}
        for r, t, l, w, q in rows]})


TITLE_JSON = json.dumps({
    "drawing_no": "MD-522-8110-EG-CV-LAD-0107", "revision": "C01",
    "project_name": "MAADEN PHOSPHATE 3 PHASE 1 RAK-RAS AL KHAIR-AREA 'A'",
    "date": "30/09/25", "prepared_by": "P.PARDULE",
})
SLAB_JSON = json.dumps({"grade_slab": {
    "length_mm": 24430, "width_mm": 8600, "thickness_mm": 300,
    "toc_level": "97.550",
    "raw_text": "FOUNDATION LAYOUT FOR EXISTING MGA PUMP AREA GRADE SLAB "
                "& PIPE SUPPORT (TOC EL. 97.550 UNO) (300 THK)(UNO)"}})


# ================= callout grammar =================
class TestCalloutGrammar:
    @pytest.mark.parametrize("text,expected", [
        ("TYP DETAIL OF PEDESTAL P1(600x500) 2Nos", ("P1", 600, 500, 2)),
        ("PEDESTAL P2 (500x500) 20Nos", ("P2", 500, 500, 20)),
        ("P3 (350x350) 11 Nos", ("P3", 350, 350, 11)),
        ("TYP DETAIL OF PEDESTAL P6(450x450) 6Nos", ("P6", 450, 450, 6)),
        ("TYPICAL DETAIL OF PEDESTAL P7(500X500) 4NOS", ("P7", 500, 500, 4)),
        ("PEDESTAL P10 [600×400] 7 No.", ("P10", 600, 400, 7)),
    ])
    def test_accepts_maaden_callout_forms(self, text, expected):
        got = G.parse_pedestal_callout(text)
        assert got is not None, text
        assert (got["tag"], got["length_mm"], got["width_mm"], got["quantity"]) == expected

    @pytest.mark.parametrize("text", [
        "TYP DETAIL OF FOUNDATION F1",       # foundation, not pedestal
        "SECTION A",
        "TYPICAL SECTION OF CONCRETE GRADE SLAB",
        "CURB WALL 150THK X 150HIGH",
        "", "P1",
    ])
    def test_rejects_non_pedestal_text(self, text):
        assert G.parse_pedestal_callout(text) is None

    def test_three_dimension_callout_yields_height(self):
        got = G.parse_pedestal_callout("PEDESTAL P4(600x500x900) 3Nos")
        assert got["height_mm"] == 900
        assert got["length_mm"] == 600 and got["width_mm"] == 500

    def test_two_dimension_callout_leaves_height_none(self):
        assert G.parse_pedestal_callout("PEDESTAL P1(600x500) 2Nos")["height_mm"] is None


class TestPlausibilityGates:
    def test_accepts_real_pedestal(self):
        assert G.validate_pedestal("P1", 600, 500, 2) == (True, "")

    @pytest.mark.parametrize("args", [
        ("F1", 600, 500, 2),        # not a pedestal mark
        ("P1", 50, 500, 2),         # kerb-sized
        ("P1", 600, 9000, 2),       # pile-cap sized
        ("P1", None, 500, 2),
        ("P1", 600, 500, 0),
        ("P1", 600, 500, 5000),
        ("P1", "wide", 500, 2),
    ])
    def test_rejects_implausible(self, args):
        ok, reason = G.validate_pedestal(*args)
        assert not ok and reason

    def test_slab_gate(self):
        assert G.validate_grade_slab(24430, 8600, 300)[0]
        assert not G.validate_grade_slab(24430, 8600, 5)[0]        # 5 mm slab
        assert not G.validate_grade_slab(24430, 8600, None)[0]

    def test_slab_portrait_is_flagged_not_rejected(self):
        ok, reason = G.validate_grade_slab(8600, 24430, 300)
        assert ok and "length < width" in reason


class TestDateNormalisation:
    @pytest.mark.parametrize("raw,iso", [
        ("30/09/25", "2025-09-30"), ("30-09-2025", "2025-09-30"),
        ("2025-09-30", "2025-09-30"), ("30-SEP-2025", "2025-09-30"),
        ("30 September 2025", "2025-09-30"), ("30.09.25", "2025-09-30"),
    ])
    def test_parses(self, raw, iso):
        assert G.normalise_date(raw)[0] == iso

    def test_ambiguous_date_is_day_first_and_flagged(self):
        iso, note = G.normalise_date("05/09/25")
        assert iso == "2025-09-05" and "ambiguous" in note

    @pytest.mark.parametrize("raw", ["", "garbage", "32/13/25"])
    def test_unparseable_returns_blank(self, raw):
        assert G.normalise_date(raw)[0] == ""

    def test_two_digit_year_window(self):
        assert G.normalise_date("01/01/85")[0].startswith("1985")
        assert G.normalise_date("01/01/26")[0].startswith("2026")


class TestTitleBlockCleaners:
    def test_drawing_no_keeps_hyphens_drops_label(self):
        assert G.clean_drawing_no("DRAWING NUMBER: MD-522-8110-EG-CV-LAD-0107") \
            == "MD-522-8110-EG-CV-LAD-0107"

    def test_en_dash_normalised(self):
        assert G.clean_drawing_no("MD–522–8110") == "MD-522-8110"

    def test_revision_cleaned(self):
        assert G.clean_revision("Rev. C01") == "C01"


# ================= tiling / geometry =================
class TestTiling:
    def test_grid_covers_whole_sheet(self):
        tiles = tile_regions(3, 2, 0.06)
        assert len(tiles) == 6
        assert min(t.x0 for t in tiles) == 0.0 and max(t.x1 for t in tiles) == 1.0
        assert min(t.y0 for t in tiles) == 0.0 and max(t.y1 for t in tiles) == 1.0

    def test_tiles_overlap_so_seams_are_covered(self):
        a, b = tile_regions(3, 2, 0.06)[0], tile_regions(3, 2, 0.06)[1]
        assert b.x0 < a.x1, "adjacent tiles must overlap or a seam callout is lost"

    def test_zero_overlap_tiles_abut_exactly(self):
        tiles = tile_regions(2, 1, 0.0)
        assert tiles[0].x1 == pytest.approx(tiles[1].x0)

    def test_effective_resolution_beats_full_sheet(self):
        tile = tile_regions(3, 2, 0.06)[0]
        assert effective_sheet_px(tile, 2000) > 4000

    @pytest.mark.parametrize("bad", [(0, 1, 0.1), (1, 0, 0.1)])
    def test_rejects_empty_grid(self, bad):
        with pytest.raises(ValueError):
            tile_regions(*bad)

    def test_rejects_absurd_overlap(self):
        with pytest.raises(ValueError):
            tile_regions(3, 2, 0.9)


class TestHashing:
    def test_identical_hashes_are_distance_zero(self):
        assert hamming("5110010820424080", "5110010820424080") == 0

    def test_garbage_hash_is_max_distance(self):
        assert hamming("zzz", "5110010820424080") == 64


# ================= JSON salvage =================
class TestJsonSalvage:
    def test_recovers_object_from_chatty_reply(self):
        assert _salvage_json('Sure!\n{"pedestals": []}\nHope that helps') == {"pedestals": []}

    def test_handles_braces_inside_strings(self):
        got = _salvage_json('{"raw_text": "P1 {600x500}", "tag": "P1"}')
        assert got["tag"] == "P1"

    def test_truncated_object_returns_empty(self):
        assert _salvage_json('{"pedestals": [{"tag": "P1"') == {}

    def test_generate_result_json_survives_prose(self):
        assert GenerateResult(response='```json\n{"a":1}\n```', elapsed_s=0).json() == {"a": 1}


# ================= reconciliation =================
def _cand(tag, l, w, q, region, validated=True) -> PedestalExtraction:
    return PedestalExtraction(tag=tag, length_mm=l, width_mm=w, quantity=q,
                              raw_text=f"PEDESTAL {tag}({l:g}x{w:g}) {q}Nos",
                              regex_validated=validated, seen_in_regions=[region])


class TestReconciliation:
    def test_duplicate_sightings_collapse_to_one_row(self):
        out = QV.reconcile_pedestals([_cand("P1", 600, 500, 2, "tile_r1c1"),
                                      _cand("P1", 600, 500, 2, "tile_r1c2")])
        assert len(out) == 1
        assert out[0].seen_in_regions == ["tile_r1c1", "tile_r1c2"]

    def test_agreement_across_tiles_promotes_confidence(self):
        out = QV.reconcile_pedestals([_cand("P1", 600, 500, 2, "a"),
                                      _cand("P1", 600, 500, 2, "b")])
        assert out[0].confidence == "high"

    def test_conflict_keeps_majority_and_drops_confidence(self):
        res = ExtractionResult()
        out = QV.reconcile_pedestals([_cand("P1", 600, 500, 2, "a"),
                                      _cand("P1", 600, 500, 2, "b"),
                                      _cand("P1", 650, 500, 2, "c")], res)
        assert len(out) == 1
        assert out[0].length_mm == 600           # majority wins, no averaging
        assert out[0].confidence == "low"
        assert any("conflicting" in n for n in res.confidence_notes)

    def test_regex_validated_beats_unvalidated(self):
        out = QV.reconcile_pedestals([_cand("P1", 999, 999, 9, "a", validated=False),
                                      _cand("P1", 600, 500, 2, "b", validated=True)])
        assert out[0].length_mm == 600

    def test_location_needs_agreement_between_sightings(self):
        """The model repeats callouts under different crop ids, so a lone
        sighting is a weak claim about where the text is."""
        a = _cand("P1", 600, 500, 2, "m1#1"); a.grid_ref, a.source_rect = "C-5", [0.1, 0.1, 0.2, 0.2]
        b = _cand("P1", 600, 500, 2, "m1#5"); b.grid_ref, b.source_rect = "D-8", [0.5, 0.5, 0.6, 0.6]
        res = ExtractionResult()
        out = QV.reconcile_pedestals([a, b], res)
        assert out[0].grid_ref == "" and out[0].source_rect == []
        assert any("disagreed on where it is" in n for n in res.confidence_notes)

    def test_majority_location_wins(self):
        a = _cand("P1", 600, 500, 2, "m1#5"); a.grid_ref, a.source_rect = "D-8", [0.5, 0.5, 0.6, 0.6]
        b = _cand("P1", 600, 500, 2, "m2#5"); b.grid_ref, b.source_rect = "D-8", [0.5, 0.5, 0.6, 0.6]
        c = _cand("P1", 600, 500, 2, "m3#1"); c.grid_ref, c.source_rect = "I-6", [0.9, 0.9, 0.95, 0.95]
        out = QV.reconcile_pedestals([a, b, c])
        assert out[0].grid_ref == "D-8"

    def test_unanimous_location_is_kept(self):
        a = _cand("P1", 600, 500, 2, "m1#5"); a.grid_ref, a.source_rect = "D-8", [0.5, 0.5, 0.6, 0.6]
        b = _cand("P1", 600, 500, 2, "m2#5"); b.grid_ref, b.source_rect = "D-8", [0.5, 0.5, 0.6, 0.6]
        assert QV.reconcile_pedestals([a, b])[0].grid_ref == "D-8"

    def test_no_location_at_all_is_fine(self):
        assert QV.reconcile_pedestals([_cand("P1", 600, 500, 2, "m1")])[0].grid_ref == ""

    def test_output_sorted_by_tag_number(self):
        out = QV.reconcile_pedestals([_cand("P10", 600, 500, 1, "a"),
                                      _cand("P2", 500, 500, 20, "a"),
                                      _cand("P1", 600, 500, 2, "a")])
        assert [p.tag for p in out] == ["P1", "P2", "P10"]

    def test_empty_input_is_empty_output(self):
        assert QV.reconcile_pedestals([]) == []


# ================= stage parsing =================
class TestStageParsing:
    def test_transcription_overrides_bad_field_split(self):
        """Model copies the string right but splits the numbers wrong."""
        res = ExtractionResult()
        payload = {"pedestals": [{"raw_text": "TYP DETAIL OF PEDESTAL P1(600x500) 2Nos",
                                  "tag": "P1", "length_mm": 500, "width_mm": 600,
                                  "quantity": 1}]}
        out = QV._parse_pedestals(payload, "tile_r1c1", res)
        assert (out[0].length_mm, out[0].width_mm, out[0].quantity) == (600, 500, 2)
        assert any("disagreed" in n for n in res.confidence_notes)

    def test_hallucinated_pedestal_is_rejected(self):
        res = ExtractionResult()
        payload = {"pedestals": [{"raw_text": "", "tag": "P1", "length_mm": 5,
                                  "width_mm": 5, "quantity": 2}]}
        assert QV._parse_pedestals(payload, "t", res) == []
        assert any("Rejected" in n for n in res.confidence_notes)

    def test_foundation_callout_is_not_a_pedestal(self):
        res = ExtractionResult()
        payload = {"pedestals": [{"raw_text": "TYP DETAIL OF FOUNDATION F1",
                                  "tag": "F1", "length_mm": 600,
                                  "width_mm": 500, "quantity": 1}]}
        assert QV._parse_pedestals(payload, "t", res) == []

    def test_malformed_payload_is_survivable(self):
        res = ExtractionResult()
        assert QV._parse_pedestals({}, "t", res) == []
        assert QV._parse_pedestals({"pedestals": "nope"}, "t", res) == []
        assert QV._parse_pedestals({"pedestals": [None, 3]}, "t", res) == []

    def test_title_block_normalises_date_and_sets_confidence(self):
        res = ExtractionResult()
        tb = QV._parse_title_block(json.loads(TITLE_JSON), res)
        assert tb.drawing_no == "MD-522-8110-EG-CV-LAD-0107"
        assert tb.revision == "C01" and tb.date == "2025-09-30"
        assert tb.date_raw == "30/09/25" and tb.prepared_by == "P.PARDULE"
        assert tb.confidence == "high"

    def test_empty_title_block_is_low_confidence_and_flagged(self):
        res = ExtractionResult()
        tb = QV._parse_title_block({}, res)
        assert tb.is_empty() and tb.confidence == "low"
        assert any("drawing number not read" in n for n in res.confidence_notes)

    def test_grade_slab_thickness_falls_back_to_raw_text(self):
        res = ExtractionResult()
        slab = QV._parse_grade_slab({"grade_slab": {
            "length_mm": 24430, "width_mm": 8600, "thickness_mm": None,
            "raw_text": "FOUNDATION LAYOUT ... (TOC EL. 97.550 UNO) (300 THK)(UNO)"}}, res)
        assert slab.thickness_mm == 300 and slab.toc_level == "97.550"

    def test_grade_slab_null_payload(self):
        assert QV._parse_grade_slab({"grade_slab": None}, ExtractionResult()) is None


# ================= end-to-end, mocked =================
def _crops_json(*crops) -> str:
    """Model reply shape: one entry per crop, keyed by its printed number."""
    return json.dumps({"crops": [{"id": i, "lines": list(c)}
                                 for i, c in enumerate(crops, start=1)]})


# What the model transcribes off the montages, split as the real run split it.
TRANSCRIPT_PAGES = [
    _crops_json(
        ["TYP DETAIL OF PEDESTAL", "P1(600x500) 2Nos", "Scale:1/25"],
        ["SECTION", "Scale:1/25"],
        ["TYP DETAIL OF PEDESTAL", "P3(350x350) 11Nos", "Scale:1/25"],
        ["INSERT PLATE TYPE A5b", "LUGS (TYP)"],
        ["4MM THK ACID RESISTANT", "EPOXY COATING (TYP)"],
    ),
    _crops_json(
        ["TYP DETAIL OF PEDESTAL", "P2(500x500) 20Nos", "Scale:1/25"],
        ["CURB WALL 150THK X 150HIGH"],
        ["TOC EL 97.850"],
    ),
    _crops_json(
        ["TYP DETAIL OF PEDESTAL", "P6(450x450) 6Nos"],
        ["(10)-D12", "(3)-D10 CLOSED LINK"],
    ),
    _crops_json(
        ["TYP DETAIL OF PEDESTAL", "P7(500x500) 4Nos"],
        ["SUMP 500x500"],
    ),
]


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample drawing not in repo root")
class TestMontageStrategy:
    """The default path: locate text geometrically, transcribe montages."""

    def _client(self):
        return FakeClient({
            "title_block": [TITLE_JSON],
            "transcribe": list(TRANSCRIPT_PAGES),
            "grade_slab": [SLAB_JSON],
        })

    def test_finds_every_pedestal_from_the_transcript(self):
        r = QV.extract_from_pdf(SAMPLE_PDF, client=self._client())
        assert {p.tag for p in r.pedestals} == set(TRUTH_PEDESTALS)
        for p in r.pedestals:
            assert (p.length_mm, p.width_mm, p.quantity) == TRUTH_PEDESTALS[p.tag]

    def test_uses_far_fewer_calls_than_the_blind_sweep(self):
        c = self._client()
        QV.extract_from_pdf(SAMPLE_PDF, client=c)
        sweep_calls = 1 + len(QV._tile_plan(PROFILES["sweep"])) + 1
        assert len(c.calls) < sweep_calls / 3, (
            f"montage strategy used {len(c.calls)} calls vs {sweep_calls} for sweep")

    def test_text_blocks_are_found_without_the_model(self):
        c = self._client()
        r = QV.extract_from_pdf(SAMPLE_PDF, client=c)
        assert r.text_blocks_found > 0
        assert r.montages_sent > 0
        # Locating text costs zero calls; only montages and the two crops do.
        assert c.calls.count("transcribe") == r.montages_sent
        assert len(c.calls) == r.montages_sent + 2

    def test_grammars_pick_up_extra_element_types(self):
        r = QV.extract_from_pdf(SAMPLE_PDF, client=self._client())
        f = r.findings
        assert f["curb_walls"][0]["thickness_mm"] == 150
        assert f["sumps"][0]["length_mm"] == 500
        assert any(l["kind"] == "TOC" for l in f["levels"])
        assert f["insert_plates"][0]["type"] == "A5b"
        assert f["epoxy"][0]["thickness_mm"] == 4
        assert any(rb["diameter_mm"] == 12 for rb in f["rebar"])

    def test_extra_findings_are_not_merged_into_the_project(self):
        """Curb wall and sump lack required dimensions — surfaced, never merged."""
        r = QV.extract_from_pdf(SAMPLE_PDF, client=self._client())
        p = QV.merge_into_project(Project(project_name="", drawing_no=""), r)
        assert p.curb_walls == [] and p.sumps == []
        assert len(p.pedestals) == len(TRUTH_PEDESTALS)

    def test_truncated_montage_is_retried_by_splitting(self):
        """The model sometimes transcribes some crops and stops. That is
        detectable, so the montage is re-sent in halves rather than lost."""
        c = FakeClient({"title_block": [TITLE_JSON],
                        "transcribe": ['{"crops": [{"lines": ["SECTION"]}]}'] * 60,
                        "grade_slab": [SLAB_JSON]}, truncate=True)
        r = QV.extract_from_pdf(SAMPLE_PDF, client=c)
        assert any("re-sending" in n for n in r.confidence_notes)
        # bounded: splitting stops at max_montage_splits rather than recursing
        assert any("some text may be unread" in n for n in r.confidence_notes)

    def test_splitting_is_bounded(self):
        c = FakeClient({"title_block": [TITLE_JSON],
                        "transcribe": ['{"crops": [{"lines": ["X"]}]}'] * 200,
                        "grade_slab": [SLAB_JSON]}, truncate=True)
        QV.extract_from_pdf(SAMPLE_PDF, client=c)
        assert c.calls.count("transcribe") < 60, "split retry must terminate"

    def test_progress_covers_montages_and_crops(self):
        seen = []
        c = self._client()
        r = QV.extract_from_pdf(SAMPLE_PDF, client=c,
                                progress=lambda l, i, n: seen.append((i, n)))
        assert seen[0][0] == 1
        assert seen[-1][0] == seen[-1][1]        # finishes at 100%
        assert seen[-1][1] >= r.montages_sent + 2

    def test_transcript_is_kept_for_audit(self):
        r = QV.extract_from_pdf(SAMPLE_PDF, client=self._client())
        assert any("P1(600x500) 2Nos" in l for l in r.transcribed_lines)


class TestTranscriptParsing:
    def test_parses_crops_shape(self):
        lines, n = QV._parse_transcript(json.loads(_crops_json(["A", "B"], ["C"])))
        assert lines == ["A", "B", "C"] and n == 2

    def test_crop_ids_are_read_back(self):
        payload = {"crops": [{"id": 3, "lines": ["X"]}, {"id": 1, "lines": ["Y"]}]}
        pairs, n = QV._parse_transcript_crops(payload)
        assert pairs == [(3, ["X"]), (1, ["Y"])] and n == 2

    def test_missing_or_bad_id_is_none(self):
        pairs, _ = QV._parse_transcript_crops(
            {"crops": [{"lines": ["A"]}, {"id": "nope", "lines": ["B"]}]})
        assert [p[0] for p in pairs] == [None, None]

    def test_parses_flat_lines_shape(self):
        lines, n = QV._parse_transcript({"lines": ["A", " B ", "", 3]})
        assert lines == ["A", "B"] and n == 0

    def test_survives_junk(self):
        assert QV._parse_transcript({}) == ([], 0)
        assert QV._parse_transcript({"crops": "nope"}) == ([], 0)
        assert QV._parse_transcript({"crops": [None, "X"]}) == (["X"], 2)


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample drawing not in repo root")
class TestSweepStrategy:
    """The fallback blind tile sweep, for sheets with no vector text."""

    def _client(self, profile: str = "sweep"):
        n_tiles = len(QV._tile_plan(PROFILES[profile]))
        hits = [
            _ped_json(("TYP DETAIL OF PEDESTAL P1(600x500) 2Nos", "P1", 600, 500, 2)),
            _ped_json(("TYP DETAIL OF PEDESTAL P2(500x500) 20Nos", "P2", 500, 500, 20),
                      ("TYP DETAIL OF PEDESTAL P3(350x350) 11Nos", "P3", 350, 350, 11)),
            _ped_json(("TYP DETAIL OF PEDESTAL P7(500x500) 4Nos", "P7", 500, 500, 4)),
            _ped_json(("TYP DETAIL OF PEDESTAL P6(450x450) 6Nos", "P6", 450, 450, 6)),
        ]
        empties = ['{"pedestals": []}'] * (n_tiles - len(hits))
        return FakeClient({"title_block": [TITLE_JSON],
                           "pedestals": hits + empties,
                           "grade_slab": [SLAB_JSON]})

    def test_full_run_recovers_ground_truth(self):
        r = QV.extract_from_pdf(SAMPLE_PDF, client=self._client(), profile="sweep")
        assert {p.tag for p in r.pedestals} == set(TRUTH_PEDESTALS)
        assert r.title_block.drawing_no == "MD-522-8110-EG-CV-LAD-0107"
        assert r.grade_slabs[0].length_mm == 24430

    def test_stage_call_order_is_title_then_tiles_then_plan(self):
        c = self._client()
        QV.extract_from_pdf(SAMPLE_PDF, client=c, profile="sweep")
        assert c.calls[0] == "title_block"
        assert c.calls[-1] == "grade_slab"
        assert c.calls.count("pedestals") == len(QV._tile_plan(PROFILES["sweep"]))

    def test_sends_no_montages(self):
        c = self._client()
        r = QV.extract_from_pdf(SAMPLE_PDF, client=c, profile="sweep")
        assert r.montages_sent == 0 and c.calls.count("transcribe") == 0


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample drawing not in repo root")
class TestExtractionRobustness:
    def test_handoff_debug_properties_are_populated(self):
        c = FakeClient({"title_block": [TITLE_JSON],
                        "transcribe": list(TRANSCRIPT_PAGES),
                        "grade_slab": [SLAB_JSON]})
        r = QV.extract_from_pdf(SAMPLE_PDF, client=c)
        assert "MD-522" in r.raw_stage1_response
        assert "24430" in r.raw_stage3_response

    def test_model_host_failure_degrades_not_crashes(self):
        class Dead(FakeClient):
            def generate(self, *a, **k):
                from extractors.ollama_client import OllamaError
                raise OllamaError("connection refused")
        r = QV.extract_from_pdf(SAMPLE_PDF, client=Dead())
        assert r.pedestals == [] and r.title_block.is_empty()
        assert any("connection refused" in n for n in r.confidence_notes)

    def test_no_text_layer_is_recorded(self):
        c = FakeClient({"title_block": [TITLE_JSON],
                        "transcribe": list(TRANSCRIPT_PAGES),
                        "grade_slab": [SLAB_JSON]})
        r = QV.extract_from_pdf(SAMPLE_PDF, client=c)
        assert any("no text layer" in n for n in r.confidence_notes)
        assert r.used_text_layer is False


# ================= merge semantics =================
def _result_with_pedestals() -> ExtractionResult:
    r = ExtractionResult(source_pdf=str(SAMPLE_PDF))
    r.title_block = QV._parse_title_block(json.loads(TITLE_JSON), r)
    r.pedestals = [PedestalExtraction(tag=t, length_mm=l, width_mm=w, quantity=q,
                                      regex_validated=True, confidence="high")
                   for t, (l, w, q) in TRUTH_PEDESTALS.items()]
    r.grade_slabs = [QV._parse_grade_slab(json.loads(SLAB_JSON), r)]
    return r


class TestMerge:
    def test_mm_converted_to_metres(self):
        p = QV.merge_into_project(Project(project_name="", drawing_no=""),
                                  _result_with_pedestals())
        p1 = next(x for x in p.pedestals if x.tag == "P1")
        assert (p1.length_m, p1.width_m, p1.quantity) == (0.600, 0.500, 2)

    def test_grade_slab_converted(self):
        p = QV.merge_into_project(Project(project_name="", drawing_no=""),
                                  _result_with_pedestals())
        gs = p.grade_slabs[0]
        assert (gs.length_m, gs.width_m, gs.thickness_m) == (24.430, 8.600, 0.300)

    def test_never_overwrites_human_entered_fields(self):
        p = Project(project_name="MY PROJECT", drawing_no="MY-DWG-01", revision="B")
        QV.merge_into_project(p, _result_with_pedestals())
        assert p.project_name == "MY PROJECT"
        assert p.drawing_no == "MY-DWG-01"
        assert p.revision == "B"

    def test_fills_only_empty_fields(self):
        p = Project(project_name="", drawing_no="MY-DWG-01")
        QV.merge_into_project(p, _result_with_pedestals())
        assert p.drawing_no == "MY-DWG-01"
        assert p.project_name.startswith("MAADEN")
        assert p.date == "2025-09-30"

    def test_existing_pedestal_tag_is_left_alone(self):
        p = Project(project_name="", drawing_no="")
        p.pedestals.append(Pedestal(tag="P1", length_m=9.9, width_m=9.9,
                                    height_m=9.9, quantity=99))
        QV.merge_into_project(p, _result_with_pedestals())
        p1 = [x for x in p.pedestals if x.tag == "P1"]
        assert len(p1) == 1 and p1[0].length_m == 9.9

    def test_existing_slab_tag_is_left_alone(self):
        p = Project(project_name="", drawing_no="")
        p.grade_slabs.append(GradeSlab(tag="GS-01", length_m=1, width_m=1,
                                       thickness_m=1))
        QV.merge_into_project(p, _result_with_pedestals())
        assert len(p.grade_slabs) == 1 and p.grade_slabs[0].length_m == 1

    def test_merge_is_idempotent(self):
        p, r = Project(project_name="", drawing_no=""), _result_with_pedestals()
        QV.merge_into_project(p, r)
        QV.merge_into_project(p, r)
        assert len(p.pedestals) == len(TRUTH_PEDESTALS)
        assert len(p.grade_slabs) == 1

    def test_placeholder_height_applied_and_declared(self):
        r = _result_with_pedestals()
        p = QV.merge_into_project(Project(project_name="", drawing_no=""), r)
        assert all(x.height_m == QV.PLACEHOLDER_HEIGHT_M for x in p.pedestals)
        plan = QV.plan_merge(Project(project_name="", drawing_no=""), r)
        assert any("PLACEHOLDER" in c.detail for c in plan.additions
                   if c.kind == "pedestal")
        assert any("placeholder" in w for w in plan.warnings)

    def test_callout_height_beats_placeholder(self):
        r = ExtractionResult()
        r.pedestals = [PedestalExtraction(tag="P4", length_mm=600, width_mm=500,
                                          height_mm=900, quantity=3,
                                          regex_validated=True)]
        p = QV.merge_into_project(Project(project_name="", drawing_no=""), r)
        assert p.pedestals[0].height_m == 0.900

    def test_pdf_source_recorded(self):
        p = QV.merge_into_project(Project(project_name="", drawing_no=""),
                                  _result_with_pedestals())
        assert p.pdf_source_filename == SAMPLE_PDF.name


class TestMergePlan:
    def test_plan_matches_what_merge_does(self):
        p, r = Project(project_name="", drawing_no=""), _result_with_pedestals()
        plan = QV.plan_merge(p, r)
        planned = {c.target for c in plan.additions if c.kind == "pedestal"}
        QV.merge_into_project(p, r)
        assert planned == {x.tag for x in p.pedestals}

    def test_plan_marks_existing_as_skipped(self):
        p = Project(project_name="X", drawing_no="Y")
        p.pedestals.append(Pedestal(tag="P1", length_m=1, width_m=1,
                                    height_m=1, quantity=1))
        plan = QV.plan_merge(p, _result_with_pedestals())
        skipped = {c.target for c in plan.changes if c.action == "skip_existing"}
        assert {"P1", "project_name", "drawing_no"} <= skipped

    def test_plan_does_not_mutate_project(self):
        p = Project(project_name="", drawing_no="")
        QV.plan_merge(p, _result_with_pedestals())
        assert p.pedestals == [] and p.project_name == ""


class TestExtractionWarnings:
    def test_declares_draft_status_first(self):
        r = _result_with_pedestals()
        lines = QV.extraction_warnings(r, QV.plan_merge(Project(project_name="",
                                                               drawing_no=""), r))
        assert lines[0].startswith("UNVERIFIED DRAFT")
        assert any("Tier 3" in l for l in lines)
        assert any("P1" in l for l in lines)


# ================= RAG example library =================
class TestRagExamples:
    def test_save_and_load_round_trip(self, tmp_path):
        rag_examples.save_example(
            drawing_no="MD-522-8110-EG-CV-LAD-0107", revision="C01",
            image_hash="5110010820424080",
            verified_extraction={"pedestals": [{"tag": "P1"}]},
            verified_by="Johnson Andrew", examples_dir=tmp_path)
        loaded = rag_examples.load_examples(tmp_path)
        assert len(loaded) == 1
        assert loaded[0]["verified_extraction"]["pedestals"][0]["tag"] == "P1"

    def test_identical_hash_retrieves_example(self, tmp_path):
        rag_examples.save_example(drawing_no="A-1", revision="C01",
                                  image_hash="5110010820424080",
                                  verified_extraction={"pedestals": []},
                                  examples_dir=tmp_path)
        assert rag_examples.find_similar("5110010820424080", examples_dir=tmp_path)

    def test_distant_hash_retrieves_nothing(self, tmp_path):
        rag_examples.save_example(drawing_no="A-1", revision="C01",
                                  image_hash="ffffffffffffffff",
                                  verified_extraction={"pedestals": []},
                                  examples_dir=tmp_path)
        assert rag_examples.find_similar("0000000000000000",
                                         examples_dir=tmp_path) == []

    def test_drawing_number_prefix_retrieves_sibling_sheet(self, tmp_path):
        rag_examples.save_example(drawing_no="MD-522-8110-EG-CV-LAD-0099",
                                  revision="C01", image_hash="ffffffffffffffff",
                                  verified_extraction={"pedestals": []},
                                  examples_dir=tmp_path)
        got = rag_examples.find_similar("0000000000000000",
                                        "MD-522-8110-EG-CV-LAD-0107",
                                        examples_dir=tmp_path)
        assert len(got) == 1

    def test_self_can_be_excluded(self, tmp_path):
        rag_examples.save_example(drawing_no="A-1", revision="C01",
                                  image_hash="5110010820424080",
                                  verified_extraction={"pedestals": []},
                                  examples_dir=tmp_path)
        assert rag_examples.find_similar("5110010820424080", exclude_drawing_no="A-1",
                                         examples_dir=tmp_path) == []

    def test_corrupt_file_is_skipped(self, tmp_path):
        (tmp_path / "bad.json").write_text("{not json")
        assert rag_examples.load_examples(tmp_path) == []

    def test_stats_track_handoff_target(self, tmp_path):
        rag_examples.save_example(drawing_no="A-1", revision="C01", image_hash="0",
                                  verified_extraction={"pedestals": [{"tag": "P1"}]},
                                  examples_dir=tmp_path)
        s = rag_examples.library_stats(tmp_path)
        assert s["distinct_drawings"] == 1 and s["target"] == 10
        assert s["target_met"] is False

    def test_few_shot_injection_mentions_example(self):
        prompt = P.with_examples(P.PEDESTAL_PROMPT, [
            {"drawing_no": "MD-1", "verified_extraction": {"pedestals": [{"tag": "P1"}]}}])
        assert "MD-1" in prompt and len(prompt) > len(P.PEDESTAL_PROMPT)

    def test_no_examples_leaves_prompt_untouched(self):
        assert P.with_examples(P.PEDESTAL_PROMPT, []) == P.PEDESTAL_PROMPT


class TestPromptRegressions:
    """Guards on wording that measurably changed model behaviour."""

    @pytest.mark.parametrize("phrase", P.SUPPRESSIVE_PHRASES)
    def test_pedestal_prompt_carries_no_suppressive_rules(self, phrase):
        """Stacked prohibitions collapsed recall to zero.

        Measured: on one tile image, a prompt carrying these three extra rules
        returned {"pedestals": []}; without them the same model read both
        callouts on the same image correctly. Filtering belongs in
        callout_grammar, not the prompt.
        """
        assert phrase not in P.PEDESTAL_PROMPT

    def test_pedestal_prompt_still_asks_for_verbatim_text(self):
        """raw_text is the anchor the regex re-parsing depends on."""
        assert "raw_text" in P.PEDESTAL_PROMPT
        assert "VERBATIM" in P.PEDESTAL_PROMPT

    def test_prompt_filtering_is_done_by_the_grammar_instead(self):
        """What the removed prompt rules were trying to achieve, done in code."""
        assert G.parse_pedestal_callout("TYP DETAIL OF FOUNDATION F1") is None
        assert G.parse_pedestal_callout("P2") is None          # bare plan mark
        assert not G.validate_pedestal("F1", 600, 500, 2)[0]


# ================= real sheet geometry (no model needed) =================
@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample drawing not in repo root")
class TestSampleSheetGeometry:
    def test_recognised_as_a0_landscape(self):
        doc, page = open_page(SAMPLE_PDF)
        try:
            info = page_info(page)
            assert info["sheet_size"] == "A0"
            assert info["orientation"] == "landscape"
            assert info["rotation"] == 270      # stored portrait, displayed landscape
        finally:
            doc.close()

    def test_has_no_text_layer_so_pdfplumber_would_fail(self):
        doc, page = open_page(SAMPLE_PDF)
        try:
            assert page_info(page)["has_text_layer"] is False
        finally:
            doc.close()

    def test_sheet_hash_is_stable(self):
        doc, page = open_page(SAMPLE_PDF)
        try:
            assert sheet_hash(page) == sheet_hash(page)
            assert len(sheet_hash(page)) == 16
        finally:
            doc.close()
