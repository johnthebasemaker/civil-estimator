#!/usr/bin/env python
"""Rebuild the consolidated set workbook from saved extractions.

    venv/bin/python bin/rebuild_set.py --derive all

Every run writes `output/<pdf stem>_extraction.json`, so the consolidated
workbook can be rebuilt — with different derivation rules, or after a change to
the workbook layout — without spending model time on the drawings again. A full
set costs half an hour of vision; re-reading its JSON costs a second.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import classification as CLASS
from core import set_workbook as SW                       # noqa: E402
from core.derivation import (                             # noqa: E402
    apply_derived, default_rules, derive, rules_from_findings,
)
from core.models import Project                           # noqa: E402
from extractors import qwen_vision as QV                  # noqa: E402
from extractors.models import ExtractionResult            # noqa: E402

OUTPUT_DIR = Path("output")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--derive", metavar="RULES", default="",
                    help="'all', or a comma list of rule keys")
    ap.add_argument("--out", default=str(OUTPUT_DIR / "SET_BOQ.xlsx"))
    ap.add_argument("--json-dir", default=str(OUTPUT_DIR))
    ap.add_argument("--project-name", default="")
    args = ap.parse_args(argv)

    files = sorted(Path(args.json_dir).glob("*_extraction.json"))
    if not files:
        raise SystemExit(f"No *_extraction.json in {args.json_dir}")

    wanted = {r.strip() for r in args.derive.split(",") if r.strip()}
    entries = []
    for path in files:
        try:
            result = ExtractionResult(**json.loads(path.read_text()))
        except Exception as exc:                          # noqa: BLE001
            print(f"  skip {path.name}: {exc}")
            continue

        project = Project(project_name=args.project_name, drawing_no="")
        QV.merge_into_project(project, result)
        if not project.drawing_no:
            project.drawing_no = Path(result.source_pdf).stem or path.stem

        derived_tags: set[str] = set()
        if wanted:
            rules = rules_from_findings(default_rules(), result.findings)
            for rule in rules:
                rule.enabled = "all" in wanted or rule.key in wanted
            items = derive(project, rules)
            derived_tags = {getattr(i.element, "tag", "") for i in items}
            apply_derived(project, items)

        placeholders = {p.tag for p in project.pedestals
                        if abs(p.height_m - QV.PLACEHOLDER_HEIGHT_M) < 1e-9}
        notes = []
        if result.used_text_layer:
            notes.append("read from the sheet's own text layer (no model calls)")
        if placeholders:
            notes.append(f"placeholder height on {', '.join(sorted(placeholders))}")
        discovery = result.discovery or {}
        n_items = len(discovery.get("items") or ())
        if not (project.pedestals or project.grade_slabs):
            if n_items:
                notes.append(f"no pedestal or slab on this sheet — {n_items} item(s) "
                             f"read from the drawing's own wording instead")
            else:
                notes.append("no quantities on this sheet — marks and findings only")

        entries.append(SW.build_entry(
            project.drawing_no, project, source_pdf=result.source_pdf,
            derived_tags=derived_tags, placeholder_tags=placeholders,
            notes=notes, position_marks=result.position_marks,
            discovery=discovery,
            # Set on the Extract page and kept on disk, so a workbook rebuilt
            # from saved extractions is still filed by site condition.
            classification=CLASS.get(result.source_pdf)))
        print(f"  {project.drawing_no:<32} "
              f"peds={len(project.pedestals)} slabs={len(project.grade_slabs)} "
              f"marks={len(result.position_marks)} read={n_items}")

    out = SW.write_set_workbook(entries, args.out,
                                project_name=args.project_name)
    print(f"\nConsolidated BOQ → {out}  ({len(entries)} drawing(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
