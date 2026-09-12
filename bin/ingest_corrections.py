#!/usr/bin/env python
"""Read expert-corrected workbooks and report what the tool got wrong.

    venv/bin/python bin/ingest_corrections.py output/reviewed/*.xlsx

Reads the Verification sheet of each marked-up workbook, scores the extraction
against what the engineers wrote, banks confirmed drawings as few-shot examples,
and — most usefully — lists any callout the regex grammar could not parse, since
adding a pattern for those fixes that shape of callout permanently.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors import corrections  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workbooks", nargs="+", help="marked-up .xlsx files")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.workbooks if Path(p).suffix.lower() == ".xlsx"]
    if not paths:
        raise SystemExit("No .xlsx files given.")

    report = corrections.ingest(paths, write_report=not args.no_report)

    print(f"\n{'=' * 66}\nEXTRACTION ACCURACY\n{'=' * 66}")
    print(f"  drawings reviewed : {report['drawings_reviewed']}")
    print(f"  values reviewed   : {report['values_reviewed']}")
    print(f"  correct           : {report['values_correct']} "
          f"({report['accuracy_pct']}%)")

    if report["per_item"]:
        print("\n  by item type:")
        for item, s in report["per_item"].items():
            print(f"    {item:<16} {s['right']:>3}/{s['reviewed']:<3} "
                  f"= {s['accuracy_pct']:>5.1f}%")

    if report["per_drawing"]:
        print("\n  by drawing:")
        for dwg, s in report["per_drawing"].items():
            pct = 100 * s["right"] / s["reviewed"] if s["reviewed"] else 0
            print(f"    {dwg:<32} {s['right']:>3}/{s['reviewed']:<3} = {pct:>5.1f}%")

    if report["unparsed_callouts"]:
        print(f"\n{'-' * 66}\nCALLOUTS THE GRAMMAR CANNOT READ "
              f"({len(report['unparsed_callouts'])})\n{'-' * 66}")
        print("  Each of these is missed on every drawing until a pattern is")
        print("  added to extractors/callout_grammar.py. This is the cheapest")
        print("  and most permanent way to improve extraction.\n")
        for row in report["unparsed_callouts"]:
            print(f"    {row['drawing_no']} {row['grid_ref']:<12} "
                  f"{row['callout'][:52]!r}")
            print(f"        should give: {row['expected']}")

    if report["examples_saved"]:
        print(f"\n  banked as verified examples: "
              f"{', '.join(report['examples_saved'])}")
    for failure in report.get("failures", []):
        print(f"\n  could not read {failure}")
    if report.get("report_path"):
        print(f"\n  report → {report['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
