"""What the drawing did not say, and what was assumed instead.

A takeoff is never complete from one sheet. A pedestal callout gives a footprint
and no height; a plan marks positions and references sizes elsewhere; a sections
sheet specifies a coating without stating its area. The old behaviour was to
disable the button — no pedestal and no slab meant no workbook at all, which is
how five drawings in this set produced nothing an estimator could hold.

That is the wrong trade. A bill with a stated gap is useful: it tells the
engineer exactly which figure to supply and where. A bill that was never
produced tells them nothing, and the missing figure is still missing.

So nothing here blocks. Every gap is recorded with four things:

    what is missing · what was assumed instead · what that affects · where to fix it

and the workbook carries them on their own sheet. The severity ranking is about
how a number should be treated, not about how bad it is:

* ``blocking``  — a quantity that cannot be computed at all. It is absent from
  the bill rather than wrong in it.
* ``assumed``   — a number is present and did not come off the drawing. This is
  the dangerous class, because it looks like every other number.
* ``confirm``   — the drawing states a specification without its extent. The
  reviewer supplies one figure and the row prices itself.
* ``note``      — provenance worth knowing, nothing to do.
"""
from __future__ import annotations

from dataclasses import dataclass, field

BLOCKING, ASSUMED, CONFIRM, NOTE = "blocking", "assumed", "confirm", "note"

SEVERITY_ORDER = {BLOCKING: 0, ASSUMED: 1, CONFIRM: 2, NOTE: 3}
SEVERITY_LABEL = {
    BLOCKING: "Not priced",
    ASSUMED: "Assumed — check before pricing",
    CONFIRM: "Needs one figure from you",
    NOTE: "For information",
}


@dataclass
class Gap:
    """One thing the drawing did not give."""
    severity: str
    subject: str                 # what it is about, e.g. "Pedestal P1"
    missing: str                 # what the drawing did not state
    assumed: str = ""            # what was used instead, if anything
    affects: str = ""            # which quantities move if this is wrong
    fix: str = ""                # where to put the right answer
    source: str = ""             # verbatim text this came from, if any

    @property
    def label(self) -> str:
        return SEVERITY_LABEL.get(self.severity, self.severity)

    def as_row(self) -> dict:
        return {"Severity": self.label, "Subject": self.subject,
                "Not stated on the drawing": self.missing,
                "Used instead": self.assumed, "Affects": self.affects,
                "Where to fix it": self.fix, "From the sheet": self.source}


@dataclass
class GapReport:
    gaps: list[Gap] = field(default_factory=list)

    def add(self, *args, **kwargs) -> None:
        self.gaps.append(Gap(*args, **kwargs))

    def sorted(self) -> list[Gap]:
        return sorted(self.gaps, key=lambda g: (SEVERITY_ORDER.get(g.severity, 9),
                                                g.subject))

    def count(self, severity: str) -> int:
        return sum(1 for g in self.gaps if g.severity == severity)

    @property
    def blocking(self) -> int:
        return self.count(BLOCKING)

    @property
    def needs_attention(self) -> int:
        return self.count(BLOCKING) + self.count(ASSUMED) + self.count(CONFIRM)

    def as_rows(self) -> list[dict]:
        return [g.as_row() for g in self.sorted()]

    def headline(self) -> str:
        """One sentence for the top of the workbook and the page."""
        if not self.needs_attention:
            return ("Every quantity in this workbook was read from the drawing. "
                    "Check it anyway before pricing.")
        parts = []
        if self.count(ASSUMED):
            parts.append(f"{self.count(ASSUMED)} quantity(ies) rest on an assumption")
        if self.count(CONFIRM):
            parts.append(f"{self.count(CONFIRM)} item(s) need one figure from you")
        if self.count(BLOCKING):
            parts.append(f"{self.count(BLOCKING)} could not be priced at all")
        return ("This bill is usable and incomplete: " + "; ".join(parts)
                + ". Each one is listed below with what to do about it.")


# --------------------------------------------------------------------- build
PLACEHOLDER_TOLERANCE = 1e-9


def report_for(project, result=None, *, derived=None,
               placeholder_height_m: float | None = None) -> GapReport:
    """Everything missing from one drawing's takeoff.

    `project` is what will be priced, `result` the extraction it came from.
    Both are optional-ish: a manually entered project has no extraction, and a
    sections sheet has an extraction with almost nothing in the project.
    """
    rep = GapReport()
    result_discovery = (getattr(result, "discovery", None) or {}) if result else {}
    discovered = result_discovery.get("items") or []
    marks = dict(getattr(result, "position_marks", {}) or {}) if result else {}

    # --- pedestals with a height nobody printed -----------------------------
    if placeholder_height_m is not None:
        for ped in getattr(project, "pedestals", []):
            if abs(ped.height_m - placeholder_height_m) < PLACEHOLDER_TOLERANCE:
                rep.add(ASSUMED, f"Pedestal {ped.tag}",
                        "height — the callout gives a footprint only",
                        f"{placeholder_height_m:g} m placeholder",
                        "concrete volume, formwork area, rebar weight",
                        "the Pedestals grid on this page, or the Pedestals sheet")

    # --- nothing the template knows how to price ----------------------------
    if not getattr(project, "pedestals", []) and not getattr(project, "grade_slabs", []):
        if discovered:
            rep.add(BLOCKING, "This sheet",
                    "no pedestal or slab the bill can price",
                    f"{len(discovered)} item(s) read from the drawing's own wording",
                    "the priced sheets are empty; the Drawing_Items sheet is not",
                    "enter the element on the Input page if it belongs to this sheet")
        else:
            rep.add(BLOCKING, "This sheet", "no quantities of any kind", "",
                    "nothing is priced from this drawing",
                    "check the extraction, or re-run with profile='sweep'")

    # --- the slab extent the text layer cannot give -------------------------
    if result is not None and getattr(result, "used_text_layer", False) \
            and not getattr(project, "grade_slabs", []):
        rep.add(BLOCKING, "Grade slab",
                "overall plan extent — a dimension string without the plan "
                "around it says nothing",
                "", "excavation, blinding, liner, coating and joints, all of "
                    "which follow from slab geometry",
                "measure it off the sheet and add it on the Input page")

    # --- derived quantities are arithmetic, not readings --------------------
    for item in derived or []:
        rep.add(ASSUMED, getattr(item.element, "tag", "") or item.target_field,
                f"{item.target_field} is not printed on the drawing",
                item.explanation, item.target_field,
                "the derivation rules above, or the Derivation sheet")

    # --- items specified without an extent ----------------------------------
    for item in discovered:
        if item.get("qty") is None:
            rep.add(CONFIRM, item.get("description", ""),
                    item.get("basis", "extent not stated"),
                    "", f"its own line, in {item.get('uom', '')}",
                    "the Drawing_Items sheet — type the figure and it prices "
                    "itself", item.get("source", ""))

    # --- marks are a count of labels, never a takeoff -----------------------
    if marks:
        shown = ", ".join(f"{k}x{v}" for k, v in marks.items())
        rep.add(CONFIRM, "Element marks",
                "how many of each element there actually are",
                f"labels counted on the sheet: {shown}",
                "nothing yet — these are not priced",
                "count them against the plan; an element drawn in both plan and "
                "section is labelled twice")

    # --- provenance ---------------------------------------------------------
    if result is not None:
        if getattr(result, "used_text_layer", False):
            rep.add(NOTE, "This sheet",
                    "", "read from the drawing's own text layer, so the "
                        "characters are exact and no model was involved",
                    "", "")
        elif getattr(result, "montages_sent", 0):
            rep.add(NOTE, "This sheet", "",
                    f"read by the vision model in {result.montages_sent} pass(es) "
                    f"— every value is an OCR reading",
                    "", "mark up the Verification sheet against the check print")
        if not getattr(getattr(result, "title_block", None), "drawing_no", ""):
            rep.add(ASSUMED, "Drawing number", "not read from the title block",
                    "the filename", "which sheet every quantity is attributed to",
                    "the Project details box on this page")
    return rep
