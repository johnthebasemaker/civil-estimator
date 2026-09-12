"""Two-stage Qwen2.5-VL extraction over an engineering drawing sheet.

Pipeline
--------
    Stage 1  title-block crop      -> drawing no, rev, project, date, drawn-by
    Stage 2  overlapping tile sweep-> pedestal callouts (reconciled by tag)
    Stage 3  foundation-plan crop  -> grade slab overall extent

Why a tile sweep and not one full-sheet call
--------------------------------------------
Measured on MD-522-8110-EG-CV-LAD-0107 Rev C01 (A0, 1189x841 mm) with
qwen2.5vl:7b on an M-series MacBook Air:

    full sheet @ 2000 px  ->  346 s, found 0 of 5 pedestals
    single tile @ 2000 px ->   51 s, found 3 of 3 pedestals in that tile

Downsampling an A0 to 2000 px puts a 3 mm callout under two pixels of stroke
height; the text is simply not in the image any more. Tiles hold effective
resolution at ~5300 sheet-px, and are *cheaper per call* because the image
token count scales with pixels sent, not with paper covered.

The handoff suggested cropping the "top-half where the plan lives" for Stage 2.
On this sheet that is wrong: the pedestal callouts are not in the plan at all,
they sit under the section details spread from y=0.20 to y=0.60. A sweep makes
no assumption about where the draughtsman put them.

Trust model
-----------
The model transcribes; `callout_grammar` parses. Numbers are re-derived by
regex from the verbatim `raw_text` the model echoes, then passed through a
plausibility envelope. Tiles overlap, so a callout read twice is a vote.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from core.models import GradeSlab, Pedestal, Project
from extractors import callout_grammar as G
from extractors import pdf_to_image as R
from extractors import prompts as P
from extractors import rag_examples
from extractors import sheet_grid as SG
from extractors import discovery as DISC
from extractors import text_layer as TL
from extractors import vector_text as VT
from extractors.models import (
    ExtractionResult, GradeSlabExtraction, MergeChange, MergePlan,
    PedestalExtraction, Region, RegionResponse, TitleBlockExtraction,
    TranscriptBlock,
)
from extractors.ollama_client import OllamaClient, OllamaError

# Pedestal height is not on a foundation-layout callout (handoff §3: "leave
# blank for human entry"), but core.models.Pedestal.height_m is a required
# PositiveFloat, so a placeholder is unavoidable. 0.800 m matches the pedestal
# heights in output/sample_boq.xlsx. Every use is flagged three ways: a
# confidence note, a merge warning, and a WARNINGS row on the Excel Summary
# sheet. It must never reach a priced BOQ unreviewed.
PLACEHOLDER_HEIGHT_M = 0.800

DEFAULT_SLAB_TAG = "GS-01"


@dataclass
class ExtractionConfig:
    """Everything tunable about a run. See PROFILES for the two presets.

    The two numbers that matter, both measured on this sheet and machine:

    * `tile_max_pixels` — hard ceiling per image. There is a sharp latency cliff
      on an M-series Air between ~1600 and ~1900 prompt-eval tokens
      (0.95 MP -> 47 s, 1.20 MP -> 318 s, 2.83 MP -> 346 s). Staying under it is
      worth more than any other tuning.
    * the grid — chosen so each tile still reaches ~4400 effective sheet-px,
      the resolution at which Maaden callouts read reliably. Below ~4100 the
      model starts returning empty tiles for callouts that are plainly there.

    Those two pull against each other, and the resolution of the conflict is
    "more, smaller tiles" rather than "fewer, bigger" ones. Three measured
    reasons the grid is as fine as it is:

    * **Competing text starves a callout.** A 4x5 sweep put the P3 callout in
      the same tile as the sheet's 13-line GENERAL NOTES block and the model
      returned nothing, though the callout was crisply legible. Smaller tiles
      keep the notes block, the key plan and the title block in tiles of their
      own.
    * **Cost per call is flat below the cliff.** Every sub-cliff call measured
      45-55 s regardless of whether it carried 0.95 MP or 1.01 MP, so the
      per-call price of a finer grid is only the extra calls — smaller tiles are
      otherwise free, and they buy resolution.
    * **Overlap is insurance against grid alignment.** At 6% a callout landing
      near a seam appears meaningfully in one tile only. 15% gives it a second
      look without pushing tile area over the pixel budget.
    """
    profile: str = "thorough"
    # "montage" locates text geometrically and reads only the text (default).
    # "tiles" is the original blind sweep, kept for sheets whose text is raster
    # (a true scan) rather than vector, where there are no glyph paths to find.
    strategy: str = "montage"
    montage_width: int = 1200
    montage_budget_px: int = 950_000
    montage_text_px: float = 22.0
    min_text_pt: float = 9.0
    tile_cols: int = 6
    tile_rows: int = 4
    tile_overlap: float = 0.15
    tile_px: int = 2600
    tile_max_pixels: int = 950_000
    tile_bounds: Region | None = None      # None = sweep the whole sheet
    title_block_px: int = 1400
    title_max_pixels: int = 1_000_000
    plan_px: int = 2000
    plan_max_pixels: int = 950_000
    extract_title_block: bool = True
    extract_pedestals: bool = True
    extract_grade_slab: bool = True
    # Tiles emptier than this are blank paper — skip rather than spend ~50 s
    # asking the model to read a margin.
    min_ink_ratio: float = 0.004
    use_rag: bool = True
    plan_region: Region = field(default_factory=lambda: R.PLAN)
    title_block_region: Region = field(default_factory=lambda: R.TITLE_BLOCK)
    num_predict_tile: int = 1024
    num_predict_title: int = 512
    num_predict_montage: int = 2000
    # The model occasionally transcribes only some crops of a montage and stops.
    # It is detectable (fewer entries than crops sent), so rather than shrinking
    # every montage for the worst case, re-send just the short one split in two.
    # Costs extra calls only when truncation actually happens.
    retry_short_montages: bool = True
    max_montage_splits: int = 2


# Maaden sheets put the section details — and therefore the pedestal callouts —
# in a horizontal band between the plan at the top and the typical-detail strip
# at the bottom. The `fast` profile sweeps only that band.
DETAIL_BAND = Region(name="detail_band", x0=0.0, y0=0.08, x1=1.0, y1=0.68)

PROFILES: dict[str, ExtractionConfig] = {
    # Default. Locates text blocks from vector geometry (~1 s, no model), packs
    # them into montages and transcribes those. ~6 calls, ~3 min on an M-series
    # Air, and each image is pure text with none of the linework or notes blocks
    # that starved callouts in the blind sweep.
    "thorough": ExtractionConfig(profile="thorough", strategy="montage"),
    # Same, but only the largest text. Fewer crops, fewer montages.
    "quick": ExtractionConfig(profile="quick", strategy="montage",
                              min_text_pt=11.0, montage_text_px=20.0,
                              extract_grade_slab=False),
    # The original blind tile sweep. 26 calls, ~21 min. Use only when the sheet
    # has no vector text to locate — e.g. a scanned drawing, where
    # `find_text_blocks` returns nothing.
    "sweep": ExtractionConfig(profile="sweep", strategy="tiles",
                              tile_cols=6, tile_rows=4),
    # Detail band only: the sweep skips the sheet margins and the top of the
    # plan, 12 tiles at ~4900 effective sheet-px, 14 calls, ~11 min. The
    # dedicated title-block and grade-slab crops still run. Use when the sheet
    # follows the Maaden layout; a callout outside the band will be missed.
    "fast": ExtractionConfig(profile="fast", strategy="tiles",
                             tile_cols=6, tile_rows=3,
                             tile_bounds=DETAIL_BAND),
}

ProgressFn = Callable[[str, int, int], None]


# ---------- Public API ----------
def extract_from_pdf(pdf_path: str | Path, page_number: int = 0, *,
                     profile: str = "thorough",
                     config: ExtractionConfig | None = None,
                     client: OllamaClient | None = None,
                     progress: ProgressFn | None = None,
                     debug_dir: str | Path | None = None) -> ExtractionResult:
    """Run the full extraction over one page of one PDF.

    `progress(label, done, total)` is called before each model call so the
    Streamlit page can show a bar over a six-minute run.
    """
    cfg = config or PROFILES.get(profile, PROFILES["thorough"])
    client = client or OllamaClient()
    started = time.time()

    doc, page = R.open_page(pdf_path, page_number)
    try:
        info = R.page_info(page)
        grid = SG.detect_grid(page)
        result = ExtractionResult(
            source_pdf=str(pdf_path), page_number=page_number,
            profile=cfg.profile, model=client.model,
            image_hash=R.sheet_hash(page),
        )
        result.note(f"Sheet {info['sheet_size']} {info['orientation']} "
                    f"({info['width_mm']}x{info['height_mm']} mm), "
                    f"/Rotate {info['rotation']}.")
        result.note(f"Border grid {grid.columns}x{grid.rows} "
                    f"{'measured from the sheet' if grid.detected else 'assumed'} "
                    f"— extracted values are quoted by grid square.")
        if cfg.extract_pedestals and cfg.strategy == "tiles":
            tiles = _tile_plan(cfg)
            eff = R.effective_sheet_px(tiles[0], cfg.tile_px,
                                       max_pixels=cfg.tile_max_pixels)
            result.note(f"Sweep: {len(tiles)} tiles at ~{eff} effective sheet-px, "
                        f"<= {cfg.tile_max_pixels / 1e6:.2f} MP each.")
        if not info["has_text_layer"]:
            result.note("PDF has no text layer (text outlined to curves) — "
                        "vision extraction is the only route; no text-parser "
                        "fallback is possible.")

        # --- Build the work list up front so progress is honest -------------
        # Montage strategy: find the text before deciding what to send.
        montages: list = []
        if cfg.extract_pedestals and cfg.strategy == "montage":
            if TL.has_usable_text(page):
                # The sheet kept its text. Read it and spend no model calls at
                # all: exact characters, exact coordinates, instant. Detection
                # is by volume, not by presence — two sheets here carry 141 and
                # 328 stray characters while being entirely outlined, and
                # treating those as "has text" would skip the vision path and
                # return nothing.
                result.used_text_layer = True
                chars, spans = TL.text_stats(page)
                lines, tblocks = TL.read_page(page)
                result.text_blocks_found = len(tblocks)
                w, h = page.rect.width, page.rect.height
                for i, blk in enumerate(tblocks, start=1):
                    rect_norm = [blk.rect.x0 / w, blk.rect.y0 / h,
                                 blk.rect.x1 / w, blk.rect.y1 / h]
                    result.transcript_blocks.append(TranscriptBlock(
                        region=f"text#{i}", rect_norm=rect_norm,
                        grid_ref=grid.ref_for_rect(rect_norm), lines=blk.texts))
                # Individual lines too: a callout split across two blocks still
                # matches the grammar on its own line.
                for line in lines:
                    if line.rotated:
                        continue
                    rect_norm = [line.rect.x0 / w, line.rect.y0 / h,
                                 line.rect.x1 / w, line.rect.y1 / h]
                    result.transcript_blocks.append(TranscriptBlock(
                        region="text-line", rect_norm=rect_norm,
                        grid_ref=grid.ref_for_rect(rect_norm), lines=[line.text]))
                result.transcribed_lines = [b.joined for b in tblocks] + \
                                           [l.text for l in lines if not l.rotated]
                result.note(
                    f"Sheet has its own text layer ({chars:,} characters, "
                    f"{spans} spans) — read directly into {len(tblocks)} block(s) "
                    f"and {len(lines)} line(s). No model calls used, and every "
                    f"value keeps its exact position on the sheet.")

                tb_text = TL.title_block_from_text(page, lines,
                                                   Path(pdf_path).name)
                for note in tb_text.get("notes", []):
                    result.note(note)
                iso, date_note = G.normalise_date(tb_text["date_raw"])
                if date_note:
                    result.note(date_note)
                result.title_block = TitleBlockExtraction(
                    drawing_no=tb_text["drawing_no"],
                    revision=tb_text["revision"],
                    date=iso, date_raw=tb_text["date_raw"],
                    confidence="high" if tb_text["drawing_no"] else "low")
                if tb_text["drawing_no"]:
                    result.note(f"Drawing number {tb_text['drawing_no']} read "
                                f"from the {tb_text['source']} — the sheet's own "
                                f"number, not one of the drawings it references.")
                # The title block itself is outlined on these sheets, and the
                # slab extent needs plan interpretation, so neither is attempted
                # from text. Both stay honest rather than guessed.
                cfg = replace(cfg, extract_title_block=False,
                              extract_grade_slab=False)
                result.note("Grade slab extent is not read from the text layer "
                            "— an overall plan dimension is just a number "
                            "without the plan around it. Enter it on Input, or "
                            "re-run this sheet with profile='sweep'.")

                # Discovery reads the span-level labels, not the stitched
                # lines above. CAD writes each annotation as its own text
                # entity, and joining entities that merely share a baseline puts
                # a neighbour's words into a callout — "4MM THK ACID RESISTANT"
                # ends up beside an unrelated bar mark from the far side of the
                # sheet. The grammars need the labels as drawn.
                result.discovery = DISC.summarise([
                    {"lines": blk.texts,
                     "grid_ref": grid.ref_for_rect(
                         [blk.rect.x0 / w, blk.rect.y0 / h,
                          blk.rect.x1 / w, blk.rect.y1 / h])}
                    for blk in TL.label_blocks(page)])

                marks = TL.count_position_marks(page)
                if marks:
                    result.position_marks = marks
                    shown = ", ".join(f"{k}x{v}" for k, v in marks.items())
                    result.note(
                        f"Position marks printed on this sheet: {shown}. These "
                        f"are counted for review, NOT used as BOQ quantities — "
                        f"an element drawn in both plan and section is labelled "
                        f"twice, and an unlabelled one is not counted at all.")
            else:
                blocks = VT.find_text_blocks(page, cfg.min_text_pt)
                result.text_blocks_found = len(blocks)
                candidates = VT.callout_candidates(blocks)
                montages = VT.build_montages(
                    page, candidates, width=cfg.montage_width,
                    budget_px=cfg.montage_budget_px,
                    target_text_px=cfg.montage_text_px)
                result.montages_sent = len(montages)
                result.note(
                    f"Located {len(blocks)} text block(s) from vector geometry, "
                    f"{len(candidates)} callout-shaped; packed into "
                    f"{len(montages)} montage(s). No model calls used to find them.")
                if not candidates:
                    result.note("No text blocks found — if this sheet is a scan "
                                "rather than vector CAD output, re-run with "
                                "profile='sweep'.")

        jobs: list[tuple[str, Region, int, int]] = []
        if cfg.extract_title_block:
            jobs.append(("title_block", cfg.title_block_region,
                         cfg.title_block_px, cfg.title_max_pixels))
        if cfg.extract_pedestals and cfg.strategy == "tiles":
            for t in _tile_plan(cfg):
                jobs.append(("pedestals", t, cfg.tile_px, cfg.tile_max_pixels))
        if cfg.extract_grade_slab:
            jobs.append(("grade_slab", cfg.plan_region,
                         cfg.plan_px, cfg.plan_max_pixels))

        examples = []
        if cfg.use_rag:
            examples = rag_examples.find_similar(result.image_hash, k=2)
            if examples:
                result.note(f"Few-shot: {len(examples)} verified example(s) "
                            f"retrieved from the library.")

        candidates: list[PedestalExtraction] = []
        total = len(jobs) + len(montages)
        done = 0

        # ---- Stage 2 (montage): one call per packed image, split on truncation ----
        queue: list[tuple[str, object, int]] = [
            (f"montage_{i}", m, 0) for i, m in enumerate(montages, start=1)]
        while queue:
            label, mont, split_depth = queue.pop(0)
            done += 1
            total = max(total, done)
            if progress:
                progress(f"transcribe:{label}", done, total)
            rr = RegionResponse(region=label, stage="transcribe",
                                px_width=mont.width, px_height=mont.height)
            try:
                gen = client.generate(
                    P.transcribe_prompt(len(mont.slots)),
                    [base64.b64encode(mont.png).decode()],
                    num_predict=cfg.num_predict_montage)
            except OllamaError as e:
                rr.error = str(e)
                result.responses.append(rr)
                result.note(f"Transcription of {label} failed: {e}")
                continue
            rr.raw_response = gen.response
            rr.elapsed_s = gen.elapsed_s
            result.responses.append(rr)

            per_crop, returned = _parse_transcript_crops(gen.json())
            unplaced = 0
            for position, (cid, crop_lines) in enumerate(per_crop):
                # Place by the number printed on the crop. Falling back to the
                # entry's position would be worse than not placing it at all: a
                # check print that boxes the wrong callout sends an engineer to
                # the wrong part of the sheet.
                slot = None
                if cid is not None and 1 <= cid <= len(mont.slots):
                    slot = mont.slots[cid - 1]
                elif len(per_crop) == len(mont.slots) and cid is None:
                    slot = mont.slots[position]     # ids absent, counts agree

                rect_norm, gref = [], ""
                if slot is not None:
                    r = slot.block.rect
                    rect_norm = [r.x0 / page.rect.width, r.y0 / page.rect.height,
                                 r.x1 / page.rect.width, r.y1 / page.rect.height]
                    gref = grid.ref_for_rect(rect_norm)
                else:
                    unplaced += 1
                result.transcript_blocks.append(TranscriptBlock(
                    region=f"{label}#{cid if cid is not None else position + 1}",
                    rect_norm=rect_norm, grid_ref=gref, lines=crop_lines))
                result.transcribed_lines.extend(crop_lines)
            if unplaced:
                result.note(f"{label}: {unplaced} crop(s) came back without a "
                            f"readable number — their text is kept but not "
                            f"located on the sheet.")
            lines = [l for _, c in per_crop for l in c]

            short = returned and returned < len(mont.slots)
            if not short:
                continue
            can_split = (cfg.retry_short_montages
                         and split_depth < cfg.max_montage_splits
                         and len(mont.slots) > 1)
            if not can_split:
                result.note(f"{label}: model returned {returned} of "
                            f"{len(mont.slots)} crops — some text may be unread.")
                continue
            halves = _split_montage(page, mont, cfg)
            if len(halves) < 2:
                result.note(f"{label}: model returned {returned} of "
                            f"{len(mont.slots)} crops — some text may be unread.")
                continue
            result.note(f"{label}: model returned {returned} of "
                        f"{len(mont.slots)} crops; re-sending it as "
                        f"{len(halves)} smaller montage(s).")
            for hi, half in enumerate(halves, start=1):
                queue.append((f"{label}.{hi}", half, split_depth + 1))
                total += 1

        for i, (stage, region, px, budget) in enumerate(jobs, start=1):
            done += 1
            if progress:
                progress(f"{stage}:{region.name}", done, total)
            img = R.render_region(page, region, px, max_pixels=budget)
            if debug_dir:
                R.save_debug(img, debug_dir)

            rr = RegionResponse(region=region.name, stage=stage,
                                px_width=img.width, px_height=img.height,
                                ink_ratio=round(img.ink_ratio, 4))

            if stage == "pedestals" and img.ink_ratio < cfg.min_ink_ratio:
                rr.skipped = True
                result.responses.append(rr)
                result.note(f"Tile {region.name} skipped: blank "
                            f"({img.ink_ratio * 100:.2f}% ink).")
                continue

            prompt, num_predict = _prompt_for(stage, cfg, examples)
            try:
                gen = client.generate(prompt, [img.b64],
                                      num_predict=num_predict)
            except OllamaError as e:
                rr.error = str(e)
                result.responses.append(rr)
                result.note(f"{stage} on {region.name} failed: {e}")
                continue

            rr.raw_response = gen.response
            rr.elapsed_s = gen.elapsed_s
            result.responses.append(rr)
            payload = gen.json()

            if stage == "title_block":
                result.title_block = _parse_title_block(payload, result)
            elif stage == "pedestals":
                candidates.extend(_parse_pedestals(payload, region.name, result))
            elif stage == "grade_slab":
                slab = _parse_grade_slab(payload, result)
                if slab:
                    result.grade_slabs = [slab]

        if cfg.strategy == "montage" and result.transcribed_lines:
            candidates.extend(_scan_with_provenance(result, grid))
            _note_findings(result, result.findings)

        result.pedestals = reconcile_pedestals(candidates, result)
        if not result.discovery:
            # A sheet that reached neither branch (no montages, no text layer)
            # still gets the key, so every consumer can read it without a guard.
            result.discovery = DISC.summarise([
                {"lines": b.lines, "grid_ref": b.grid_ref}
                for b in result.transcript_blocks])
        result.total_elapsed_s = round(time.time() - started, 1)
        _summarise(result)
        return result
    finally:
        doc.close()


def _tile_plan(cfg: ExtractionConfig) -> list[Region]:
    return R.tile_regions(cfg.tile_cols, cfg.tile_rows, cfg.tile_overlap,
                          bounds=cfg.tile_bounds)


def _prompt_for(stage: str, cfg: ExtractionConfig,
                examples: list[dict]) -> tuple[str, int]:
    if stage == "title_block":
        return P.TITLE_BLOCK_PROMPT, cfg.num_predict_title
    if stage == "grade_slab":
        return P.GRADE_SLAB_PROMPT, cfg.num_predict_tile
    return P.with_examples(P.PEDESTAL_PROMPT, examples), cfg.num_predict_tile


# ---------- Stage parsers ----------
def _parse_title_block(payload: dict, result: ExtractionResult) -> TitleBlockExtraction:
    raw_date = str(payload.get("date") or "").strip()
    iso, note = G.normalise_date(raw_date)
    if note:
        result.note(note)

    tb = TitleBlockExtraction(
        drawing_no=G.clean_drawing_no(str(payload.get("drawing_no") or "")),
        revision=G.clean_revision(str(payload.get("revision") or "")),
        project_name=G.clean_project_name(str(payload.get("project_name") or "")),
        date=iso, date_raw=raw_date,
        prepared_by=G.clean_project_name(str(payload.get("prepared_by") or "")),
    )
    filled = sum(bool(v) for v in (tb.drawing_no, tb.revision, tb.project_name, tb.date))
    tb.confidence = "high" if filled >= 3 else "medium" if filled >= 2 else "low"
    if not tb.drawing_no:
        result.note("Title block: drawing number not read — set it manually "
                    "before generating a BOQ (3_BOM refuses to run without it).")
    return tb


def _parse_pedestals(payload: dict, region_name: str,
                     result: ExtractionResult) -> list[PedestalExtraction]:
    """Turn one tile's JSON into validated candidates.

    Model fields are treated as a hint; `raw_text` re-parsed by regex is the
    authority. Anything failing the plausibility envelope is dropped with a
    note rather than silently kept.
    """
    rows = payload.get("pedestals")
    if not isinstance(rows, list):
        return []

    out: list[PedestalExtraction] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_text = str(row.get("raw_text") or "").strip()
        parsed = G.parse_pedestal_callout(raw_text)

        if parsed:
            tag, length, width = parsed["tag"], parsed["length_mm"], parsed["width_mm"]
            qty, height = parsed["quantity"], parsed.get("height_mm")
            validated = True
            # Where the model's own split disagrees with the string it copied,
            # the string wins — but say so, because it usually means the tile
            # was marginal.
            if _disagrees(row, parsed):
                result.note(f"{tag} on {region_name}: model field split disagreed "
                            f"with its own transcription {raw_text!r}; "
                            f"used the transcription.")
        else:
            tag = G.normalise_tag(row.get("tag"))
            length, width = row.get("length_mm"), row.get("width_mm")
            qty, height = row.get("quantity"), row.get("height_mm")
            validated = False

        ok, reason = G.validate_pedestal(tag, length, width, qty)
        if not ok:
            result.note(f"Rejected a callout on {region_name} ({reason})"
                        + (f" from {raw_text!r}" if raw_text else ""))
            continue

        out.append(PedestalExtraction(
            tag=G.normalise_tag(tag), length_mm=float(length), width_mm=float(width),
            quantity=int(qty), height_mm=float(height) if height else None,
            raw_text=raw_text, regex_validated=validated,
            seen_in_regions=[region_name],
            confidence="medium" if validated else "low",
            notes="" if validated else "callout text not in the expected grammar",
        ))
    return out


def _scan_with_provenance(result: ExtractionResult, grid) -> list[PedestalExtraction]:
    """Run the grammars per transcript block, so every hit keeps its location.

    Scanning the flat line list would be simpler but would throw away the one
    thing a checker needs: which patch of drawing the number came from. Blocks
    carry their rectangle, so each finding can be quoted by grid square.
    """
    merged: dict = {k: [] for k in ("pedestals", "curb_walls", "sumps", "levels",
                                    "insert_plates", "epoxy", "rebar", "thicknesses")}
    seen: dict[str, set] = {k: set() for k in merged}
    candidates: list[PedestalExtraction] = []

    blocks = result.transcript_blocks or [
        TranscriptBlock(region="transcript", lines=result.transcribed_lines)]
    for block in blocks:
        found = G.scan_lines(block.lines)
        for key, rows in found.items():
            for row in rows:
                item = dict(row)
                item["grid_ref"] = block.grid_ref
                item["source_rect"] = block.rect_norm
                if key == "pedestals":
                    # Every sighting becomes a candidate: reconcile_pedestals
                    # already collapses duplicates by tag, and it prefers the
                    # sighting that knows where it is. Deduplicating here would
                    # throw away the located re-send of a callout first seen in
                    # a truncated montage.
                    merged[key].append(item)
                    candidates.append(PedestalExtraction(
                        tag=row["tag"], length_mm=row["length_mm"],
                        width_mm=row["width_mm"], quantity=row["quantity"],
                        height_mm=row.get("height_mm"), raw_text=row["raw_text"],
                        regex_validated=True, seen_in_regions=[block.region],
                        source_rect=block.rect_norm, grid_ref=block.grid_ref,
                        confidence="high" if result.used_text_layer else "medium"))
                    continue

                sig = tuple(sorted((k, v) for k, v in row.items()
                                   if k != "raw_text" and not isinstance(v, (list, dict))))
                if sig in seen[key]:
                    # Same finding seen again — keep the sighting that knows
                    # where it is, so a truncated montage does not deprive a
                    # value of its grid reference.
                    if block.grid_ref:
                        for existing in merged[key]:
                            same = all(existing.get(k) == v for k, v in row.items()
                                       if k != "raw_text")
                            if same and not existing.get("grid_ref"):
                                existing["grid_ref"] = block.grid_ref
                                existing["source_rect"] = block.rect_norm
                    continue
                seen[key].add(sig)
                merged[key].append(item)

    # Collapse the pedestal sightings for the findings view; the candidate list
    # keeps them all so reconciliation can vote and pick up a location.
    merged["pedestals"] = _dedupe_pedestal_rows(merged["pedestals"])
    result.findings = merged

    # Open-vocabulary pass over the same transcript. The grammars above look for
    # element types we named in advance; this one looks for anything with a
    # specification printed beside it and takes the name from the drawing. On
    # the five sheets that produced no pedestal and no slab it is the only thing
    # that produces a takeoff line at all.
    if not result.discovery:
        result.discovery = DISC.summarise([
            {"lines": b.lines, "grid_ref": b.grid_ref} for b in blocks])
    n_items = len(result.discovery.get("items", []))
    if n_items:
        measured = result.discovery.get("measured", 0)
        result.note(
            f"Discovered {n_items} specified item(s) from the sheet's own "
            f"wording; {measured} carry a quantity the drawing states outright "
            f"and {n_items - measured} need an area, a length or a count from "
            f"the reviewer before they can be priced.")
    return candidates


def _dedupe_pedestal_rows(rows: list[dict]) -> list[dict]:
    """One row per tag, preferring the sighting that carries a grid reference."""
    best: dict[str, dict] = {}
    for row in rows:
        tag = row.get("tag", "")
        current = best.get(tag)
        if current is None or (not current.get("grid_ref") and row.get("grid_ref")):
            best[tag] = row
    return list(best.values())


def _split_montage(page, mont, cfg: ExtractionConfig) -> list:
    """Re-pack one montage's blocks into two smaller montages."""
    blocks = [slot.block for slot in mont.slots]
    if len(blocks) < 2:
        return []
    mid = (len(blocks) + 1) // 2
    out = []
    for chunk in (blocks[:mid], blocks[mid:]):
        if not chunk:
            continue
        out.extend(VT.build_montages(
            page, chunk, width=cfg.montage_width,
            budget_px=cfg.montage_budget_px,
            target_text_px=cfg.montage_text_px))
    return out


def _parse_transcript_crops(payload: dict) -> tuple[list[tuple[int | None, list[str]]], int]:
    """Transcript as (printed crop id, lines) pairs.

    The id is what the model read off the montage, not the position of the entry
    in its reply — the two disagree often enough that trusting position mis-
    attributes text to the wrong part of the drawing. `id` is 1-based as printed;
    None when the model omitted it, in which case the caller declines to place
    that text rather than guessing.
    """
    crops = payload.get("crops")
    if isinstance(crops, list):
        out: list[tuple[int | None, list[str]]] = []
        for crop in crops:
            if isinstance(crop, dict):
                lines = [l.strip() for l in (crop.get("lines") or [])
                         if isinstance(l, str) and l.strip()]
                raw_id = crop.get("id")
                try:
                    cid = int(raw_id) if raw_id is not None else None
                except (TypeError, ValueError):
                    cid = None
                out.append((cid, lines))
            elif isinstance(crop, str) and crop.strip():
                out.append((None, [crop.strip()]))
            else:
                out.append((None, []))
        return out, len(crops)
    flat, n = _parse_transcript(payload)
    return ([(None, flat)] if flat else []), n


def _parse_transcript(payload: dict) -> tuple[list[str], int]:
    """Flatten {"crops":[{"lines":[...]}]} (or a bare {"lines":[...]}) to lines."""
    lines: list[str] = []
    crops = payload.get("crops")
    if isinstance(crops, list):
        for crop in crops:
            if isinstance(crop, dict):
                for ln in crop.get("lines") or []:
                    if isinstance(ln, str) and ln.strip():
                        lines.append(ln.strip())
            elif isinstance(crop, str) and crop.strip():
                lines.append(crop.strip())
        return lines, len(crops)
    flat = payload.get("lines")
    if isinstance(flat, list):
        lines = [l.strip() for l in flat if isinstance(l, str) and l.strip()]
    return lines, 0


_FINDING_LABELS = {
    "curb_walls": "curb wall", "sumps": "sump", "levels": "level",
    "insert_plates": "insert plate type", "epoxy": "epoxy coating",
    "rebar": "rebar callout", "thicknesses": "thickness note",
}


def _note_findings(result: ExtractionResult, found: dict) -> None:
    """Summarise the element types the grammars recognised but cannot merge.

    These are surfaced, never merged: a curb wall callout prints thickness and
    height but not its run length, and a sump plan dimension says nothing about
    depth. Guessing the missing half is how a BOQ goes wrong quietly.
    """
    bits = [f"{len(found[k])} {label}(s)"
            for k, label in _FINDING_LABELS.items() if found.get(k)]
    if bits:
        result.note("Also read from the sheet (review only, not merged): "
                    + ", ".join(bits) + ".")


def _disagrees(row: dict, parsed: dict) -> bool:
    for key in ("length_mm", "width_mm", "quantity"):
        val = row.get(key)
        if val in (None, ""):
            continue
        try:
            if float(val) != float(parsed[key]):
                return True
        except (TypeError, ValueError):
            return True
    return False


def _parse_grade_slab(payload: dict, result: ExtractionResult) -> GradeSlabExtraction | None:
    row = payload.get("grade_slab")
    if not isinstance(row, dict):
        return None

    raw_text = str(row.get("raw_text") or "").strip()
    thickness = row.get("thickness_mm") or G.parse_thickness_mm(raw_text)
    toc = str(row.get("toc_level") or "") or G.parse_toc_level(raw_text)

    ok, reason = G.validate_grade_slab(row.get("length_mm"), row.get("width_mm"), thickness)
    if not ok:
        result.note(f"Grade slab rejected ({reason}) — enter it manually in 1_Input.")
        return None
    if reason:
        result.note(f"Grade slab: {reason}")

    slab = GradeSlabExtraction(
        tag=DEFAULT_SLAB_TAG,
        length_mm=float(row["length_mm"]), width_mm=float(row["width_mm"]),
        thickness_mm=float(thickness), toc_level=toc, raw_text=raw_text,
        confidence="medium" if raw_text else "low",
        notes="Overall extent read off the foundation layout plan — "
              "verify against the dimension string on the sheet.",
    )
    result.note(f"Grade slab read as {slab.length_mm:g} x {slab.width_mm:g} x "
                f"{slab.thickness_mm:g} mm"
                + (f" (TOC EL {slab.toc_level})" if slab.toc_level else "")
                + ". Plan dimensions are less reliable than callouts — check it.")
    return slab


# ---------- Cross-tile reconciliation ----------
def reconcile_pedestals(candidates: list[PedestalExtraction],
                        result: ExtractionResult | None = None) -> list[PedestalExtraction]:
    """Collapse duplicate sightings of the same pedestal tag into one row.

    Overlapping tiles mean a callout near a seam is read twice. Two independent
    reads agreeing is the strongest confidence signal available here, so
    agreement is promoted to "high" and disagreement is surfaced rather than
    averaged away — averaging two dimensions would invent a third wrong one.
    """
    by_tag: dict[str, list[PedestalExtraction]] = {}
    for c in candidates:
        by_tag.setdefault(c.tag, []).append(c)

    merged: list[PedestalExtraction] = []
    for tag in sorted(by_tag, key=_tag_sort_key):
        group = by_tag[tag]
        # A regex-validated read beats an unvalidated one outright.
        preferred = [c for c in group if c.regex_validated] or group
        winner = _modal_candidate(preferred)

        regions = sorted({r for c in group for r in c.seen_in_regions})
        winner.seen_in_regions = regions
        _place(winner, group, result)

        conflicts = {(c.length_mm, c.width_mm, c.quantity) for c in preferred}
        if len(conflicts) > 1:
            detail = "; ".join(f"{l:g}x{w:g} x{q}" for l, w, q in sorted(conflicts))
            winner.notes = (winner.notes + " | " if winner.notes else "") + \
                f"tiles disagreed ({detail}) — kept the most frequent reading"
            winner.confidence = "low"
            if result:
                result.note(f"{tag}: conflicting readings across tiles ({detail}). "
                            f"Verify on the sheet.")
        elif winner.regex_validated and len(regions) > 1:
            winner.confidence = "high"

        merged.append(winner)
    return merged


def _place(winner: PedestalExtraction, group: list[PedestalExtraction],
           result: ExtractionResult | None) -> None:
    """Give a callout a location only when its sightings agree on one.

    The model repeats itself: on one montage it reported the same P1 callout
    under crop 1 *and* crop 5, and reported it again on a montage that does not
    contain P1 at all. Any single sighting is therefore a weak claim about where
    the text lives.

    So locations are voted on. A clear majority wins; a tie means we do not know,
    and the callout goes into the check print unboxed rather than boxed over the
    wrong detail. An engineer can find an unboxed value; one sent to the wrong
    grid square may simply tick it.
    """
    votes: dict[str, list[PedestalExtraction]] = {}
    for c in group:
        if c.grid_ref:
            votes.setdefault(c.grid_ref, []).append(c)
    if not votes:
        return

    ranked = sorted(votes.items(), key=lambda kv: -len(kv[1]))
    top_ref, top = ranked[0]
    if len(ranked) > 1 and len(ranked[1][1]) == len(top):
        winner.grid_ref, winner.source_rect = "", []
        if result:
            result.note(
                f"{winner.tag}: sightings disagreed on where it is on the sheet "
                f"({', '.join(r for r, _ in ranked)}) — reported without a "
                f"location rather than pointing at the wrong detail.")
        return
    winner.grid_ref = top_ref
    winner.source_rect = top[0].source_rect


def _tag_sort_key(tag: str) -> tuple[int, str]:
    digits = "".join(ch for ch in tag if ch.isdigit())
    return (int(digits) if digits else 9999, tag)


def _modal_candidate(group: list[PedestalExtraction]) -> PedestalExtraction:
    """Most frequently reported (L, W, qty) triple; ties break on first seen."""
    counts: dict[tuple, int] = {}
    for c in group:
        key = (c.length_mm, c.width_mm, c.quantity)
        counts[key] = counts.get(key, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1])[0]
    winner = next(c for c in group if (c.length_mm, c.width_mm, c.quantity) == best)
    return winner.model_copy(deep=True)


def _summarise(result: ExtractionResult) -> None:
    calls = [r for r in result.responses if not r.skipped and not r.error]
    slowest = max((r.elapsed_s for r in calls), default=0.0)
    result.note(
        f"{len(calls)} model call(s) in {result.total_elapsed_s:.0f} s "
        f"(slowest {slowest:.0f} s); {len(result.pedestals)} pedestal type(s), "
        f"{len(result.grade_slabs)} grade slab(s)."
    )
    if not result.pedestals:
        result.note("No pedestal callouts found. If the sheet has them, the tile "
                    "grid may be too coarse — raise tile_cols/tile_rows or "
                    "tile_px and re-run.")


# ---------- Merge into the Phase 1 Project ----------
def plan_merge(project: Project, result: ExtractionResult) -> MergePlan:
    """Dry-run the merge. Drives the 'Preview merge' step of pages/0_Extract.py.

    Non-destructive by contract (handoff §6): anything the human already typed
    wins over anything the model read.
    """
    plan = MergePlan()

    tb = result.title_block
    for field_name, value in (("drawing_no", tb.drawing_no),
                              ("revision", tb.revision),
                              ("project_name", tb.project_name),
                              ("date", tb.date),
                              ("prepared_by", tb.prepared_by)):
        if not value:
            continue
        current = getattr(project, field_name, "")
        if current:
            plan.changes.append(MergeChange(
                kind="field", target=field_name, action="skip_existing",
                detail=f"keeping {current!r}, extraction said {value!r}"))
        else:
            plan.changes.append(MergeChange(
                kind="field", target=field_name, action="add", detail=str(value)))

    existing_tags = {p.tag.upper() for p in project.pedestals}
    for ped in result.pedestals:
        if ped.tag.upper() in existing_tags:
            plan.changes.append(MergeChange(
                kind="pedestal", target=ped.tag, action="skip_existing",
                detail="already entered — extraction ignored"))
            continue
        h_mm = ped.height_mm
        h_m = (h_mm / 1000.0) if h_mm else PLACEHOLDER_HEIGHT_M
        detail = (f"{ped.length_mm / 1000:.3f} x {ped.width_mm / 1000:.3f} x "
                  f"{h_m:.3f} m, {ped.quantity} Nos")
        if not h_mm:
            detail += f"  [height is a {PLACEHOLDER_HEIGHT_M:.3f} m PLACEHOLDER]"
        plan.changes.append(MergeChange(
            kind="pedestal", target=ped.tag, action="add", detail=detail))

    placeholders = [p.tag for p in result.pedestals
                    if not p.height_mm and p.tag.upper() not in existing_tags]
    if placeholders:
        plan.warnings.append(
            f"UNVERIFIED: pedestal height is not on the callouts for "
            f"{', '.join(placeholders)}; merged at a placeholder "
            f"{PLACEHOLDER_HEIGHT_M:.3f} m. Concrete, formwork and rebar for "
            f"these pedestals are WRONG until the real heights are entered in "
            f"1_Input."
        )

    slab_tags = {s.tag.upper() for s in project.grade_slabs}
    for slab in result.grade_slabs:
        if not slab.is_usable():
            plan.changes.append(MergeChange(
                kind="grade_slab", target=slab.tag, action="reject",
                detail="incomplete dimensions"))
            continue
        if slab.tag.upper() in slab_tags:
            plan.changes.append(MergeChange(
                kind="grade_slab", target=slab.tag, action="skip_existing",
                detail="already entered — extraction ignored"))
            continue
        plan.changes.append(MergeChange(
            kind="grade_slab", target=slab.tag, action="add",
            detail=f"{slab.length_mm / 1000:.3f} x {slab.width_mm / 1000:.3f} x "
                   f"{slab.thickness_mm / 1000:.3f} m"))
        plan.warnings.append(
            f"UNVERIFIED: grade slab {slab.tag} extent was read off the plan "
            f"view, not a callout. Confirm "
            f"{slab.length_mm:g} x {slab.width_mm:g} x {slab.thickness_mm:g} mm "
            f"against the sheet."
        )

    low = [p.tag for p in result.pedestals if p.confidence == "low"]
    if low:
        plan.warnings.append(
            f"Low-confidence pedestal reading(s): {', '.join(low)}. "
            f"Check these against the drawing before pricing.")
    return plan


def merge_into_project(project: Project, result: ExtractionResult) -> Project:
    """Apply the extraction to a Project, non-destructively.

    Never overwrites a value the human already set; never removes anything.
    Returns the same instance (Streamlit holds it in session_state).
    """
    tb = result.title_block
    for field_name, value in (("drawing_no", tb.drawing_no),
                              ("revision", tb.revision),
                              ("project_name", tb.project_name),
                              ("date", tb.date),
                              ("prepared_by", tb.prepared_by)):
        if value and not getattr(project, field_name, ""):
            setattr(project, field_name, value)

    existing_tags = {p.tag.upper() for p in project.pedestals}
    for ped in result.pedestals:
        if ped.tag.upper() in existing_tags:
            continue
        height_m = (ped.height_mm / 1000.0) if ped.height_mm else PLACEHOLDER_HEIGHT_M
        project.pedestals.append(Pedestal(
            tag=ped.tag,
            length_m=ped.length_mm / 1000.0,      # handoff §5: mm -> m at merge
            width_m=ped.width_mm / 1000.0,
            height_m=height_m,
            quantity=ped.quantity,
        ))
        existing_tags.add(ped.tag.upper())

    slab_tags = {s.tag.upper() for s in project.grade_slabs}
    for slab in result.grade_slabs:
        if not slab.is_usable() or slab.tag.upper() in slab_tags:
            continue
        project.grade_slabs.append(GradeSlab(
            tag=slab.tag,
            length_m=slab.length_mm / 1000.0,
            width_m=slab.width_mm / 1000.0,
            thickness_m=slab.thickness_mm / 1000.0,
        ))
        slab_tags.add(slab.tag.upper())

    if not project.pdf_source_path and result.source_pdf:
        project.pdf_source_path = result.source_pdf
        project.pdf_source_filename = Path(result.source_pdf).name

    return project


# ---------- Provenance for the workbook ----------
def extraction_warnings(result: ExtractionResult, plan: MergePlan | None = None) -> list[str]:
    """Warning lines to push into BOM.warnings.

    core/excel_writer.py already renders BOM.warnings as a red banner at the top
    of the Summary sheet, so this is how an auto-generated workbook declares
    itself a draft without any change to Phase 1 code.
    """
    lines = [
        f"UNVERIFIED DRAFT — quantities below were derived from "
        f"{Path(result.source_pdf).name or 'a drawing'} by {result.model} "
        f"vision extraction and have NOT been checked by an engineer.",
    ]
    if plan:
        lines.extend(plan.warnings)
    for ped in result.pedestals:
        bits = [f"Pedestal {ped.tag}: read as "
                f"{ped.length_mm:g}x{ped.width_mm:g} mm, {ped.quantity} Nos"]
        if ped.raw_text:
            bits.append(f'from "{ped.raw_text}"')
        bits.append(f"[{ped.confidence} confidence, "
                    f"{len(ped.seen_in_regions)} tile sighting(s)]")
        lines.append(" ".join(bits))
    lines.append("Tier 3 items (rebar bar-by-bar, joints, sumps, embedments, "
                 "excavation extents, concrete grades) are NOT extracted — "
                 "enter them manually in 1_Input.")
    return lines
