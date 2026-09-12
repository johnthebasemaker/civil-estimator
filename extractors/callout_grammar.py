"""Deterministic re-parsing and plausibility gates for vision output.

The vision model's job is to *transcribe*; this module's job is to *parse*.
Everything the model returns as a number is re-derived here from the verbatim
`raw_text` it echoed, using a regex grammar built from the Maaden callout
convention. Where the regex and the model disagree, the regex wins.

Why this matters: a 7b model asked to do arithmetic-ish field splitting will
occasionally transpose "500x350" into length 350 / width 500, or read "11 Nos"
as quantity 1. It very rarely misreads the *string*. Anchoring on the string
converts a soft failure mode into a hard, testable one.
"""
from __future__ import annotations

import re
from datetime import date

# ---------- Pedestal callout grammar ----------
# "TYP DETAIL OF PEDESTAL P1(600x500) 2Nos"
# "PEDESTAL P2 (500x500) 20Nos"
# "P3 (350x350) 11 Nos"
PEDESTAL_CALLOUT_RE = re.compile(
    r"""
    \b(?P<tag>P\s?\d{1,2})\b        # pedestal mark: P1 .. P99 (never F1, C1 ...)
    \s*[\(\[]\s*
    (?P<length>\d{2,4})             # first dimension, mm
    \s*[x×X*]\s*
    (?P<width>\d{2,4})              # second dimension, mm
    \s*[\)\]]
    \s*[-–—:]?\s*
    (?P<qty>\d{1,3})                # quantity
    \s*(?:N\s*[o0O]\s*s?|NOS|N[o0O]\.)   # "Nos", "NOS", "No.", "2 N o s"
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Plausibility envelope for a concrete pedestal on an industrial foundation
# drawing. A 150 mm "pedestal" is a kerb; a 3 m one is a pile cap. Either is a
# misread, and a misread is far more expensive downstream than a miss.
# Same grammar, but with an explicit height as a third dimension:
# "PEDESTAL P1(600x500x800) 2Nos". Rare on layout drawings, but when it is
# printed it is the only trustworthy source of pedestal height on the sheet
# (handoff §3: take the height "only if it appears in the same callout string").
PEDESTAL_CALLOUT_3D_RE = re.compile(
    r"""
    \b(?P<tag>P\s?\d{1,2})\b
    \s*[\(\[]\s*
    (?P<length>\d{2,4})\s*[x×X*]\s*
    (?P<width>\d{2,4})\s*[x×X*]\s*
    (?P<height>\d{2,4})
    \s*[\)\]]
    \s*[-–—:]?\s*
    (?P<qty>\d{1,3})
    \s*(?:N\s*[o0O]\s*s?|NOS|N[o0O]\.)
    """,
    re.VERBOSE | re.IGNORECASE,
)

MIN_PEDESTAL_MM = 150.0
MAX_PEDESTAL_MM = 3000.0
MAX_PEDESTAL_QTY = 999
TAG_RE = re.compile(r"^P\d{1,2}$")


def parse_pedestal_callout(raw_text: str) -> dict | None:
    """Re-derive tag/length/width/quantity from the verbatim callout string.

    Returns None when the string does not match the grammar — which is the
    correct answer for "TYP DETAIL OF FOUNDATION F1" and for anything the model
    paraphrased instead of copying.
    """
    if not raw_text:
        return None
    # Try the 3-dimension form first: it is a strict superset, so the 2-D
    # pattern would otherwise match "600x500" out of "600x500x800" and silently
    # drop the height.
    m = PEDESTAL_CALLOUT_3D_RE.search(raw_text)
    if m:
        return {
            "tag": m.group("tag").replace(" ", "").upper(),
            "length_mm": float(m.group("length")),
            "width_mm": float(m.group("width")),
            "height_mm": float(m.group("height")),
            "quantity": int(m.group("qty")),
        }
    m = PEDESTAL_CALLOUT_RE.search(raw_text)
    if not m:
        return None
    return {
        "tag": m.group("tag").replace(" ", "").upper(),
        "length_mm": float(m.group("length")),
        "width_mm": float(m.group("width")),
        "height_mm": None,
        "quantity": int(m.group("qty")),
    }


def validate_pedestal(tag: str, length_mm: float | None, width_mm: float | None,
                      quantity: int | None) -> tuple[bool, str]:
    """Plausibility gate. Returns (accepted, reason_if_rejected)."""
    if not tag or not TAG_RE.match(str(tag).replace(" ", "").upper()):
        return False, f"tag {tag!r} is not a pedestal mark (expected P1..P99)"
    for name, val in (("length_mm", length_mm), ("width_mm", width_mm)):
        if val is None:
            return False, f"{name} missing"
        try:
            v = float(val)
        except (TypeError, ValueError):
            return False, f"{name} {val!r} is not numeric"
        if not MIN_PEDESTAL_MM <= v <= MAX_PEDESTAL_MM:
            return False, (f"{name} {v:g} mm outside plausible range "
                           f"{MIN_PEDESTAL_MM:g}-{MAX_PEDESTAL_MM:g} mm")
    if quantity is None:
        return False, "quantity missing"
    try:
        q = int(quantity)
    except (TypeError, ValueError):
        return False, f"quantity {quantity!r} is not an integer"
    if not 1 <= q <= MAX_PEDESTAL_QTY:
        return False, f"quantity {q} outside plausible range 1-{MAX_PEDESTAL_QTY}"
    return True, ""


def normalise_tag(tag: str) -> str:
    return str(tag or "").replace(" ", "").upper()


# ---------- Slab helpers ----------
THICKNESS_RE = re.compile(r"\(?\s*(\d{2,4})\s*THK\s*\)?", re.IGNORECASE)
TOC_LEVEL_RE = re.compile(r"TOC\s*\.?\s*EL\.?\s*([0-9]{1,3}\.[0-9]{2,3})", re.IGNORECASE)

MIN_SLAB_MM, MAX_SLAB_MM = 500.0, 500_000.0        # 0.5 m .. 500 m extent
MIN_SLAB_THK_MM, MAX_SLAB_THK_MM = 75.0, 2000.0    # 75 mm blinding .. 2 m raft


def parse_thickness_mm(text: str) -> float | None:
    m = THICKNESS_RE.search(text or "")
    return float(m.group(1)) if m else None


def parse_toc_level(text: str) -> str:
    m = TOC_LEVEL_RE.search(text or "")
    return m.group(1) if m else ""


def validate_grade_slab(length_mm, width_mm, thickness_mm) -> tuple[bool, str]:
    vals = (("length_mm", length_mm, MIN_SLAB_MM, MAX_SLAB_MM),
            ("width_mm", width_mm, MIN_SLAB_MM, MAX_SLAB_MM),
            ("thickness_mm", thickness_mm, MIN_SLAB_THK_MM, MAX_SLAB_THK_MM))
    for name, val, lo, hi in vals:
        if val is None:
            return False, f"{name} missing"
        try:
            v = float(val)
        except (TypeError, ValueError):
            return False, f"{name} {val!r} is not numeric"
        if not lo <= v <= hi:
            return False, f"{name} {v:g} mm outside plausible range {lo:g}-{hi:g} mm"
    if float(length_mm) < float(width_mm):
        # Not an error — just worth recording; plans are usually drawn long-side across.
        return True, "length < width (slab reported portrait; check plan orientation)"
    return True, ""


# ---------- Title block helpers ----------
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

_NUMERIC_DATE_RE = re.compile(r"^\s*(\d{1,4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,4})\s*$")
_TEXT_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[-/ ]\s*([A-Za-z]{3,9})\s*[-/ ]\s*(\d{2,4})\s*$")


def _expand_year(y: int) -> int:
    """Two-digit year -> four. Drawings in circulation are 1980-2079."""
    if y >= 100:
        return y
    return 2000 + y if y < 80 else 1900 + y


def normalise_date(raw: str) -> tuple[str, str]:
    """Title-block date -> ('YYYY-MM-DD', note).

    Maaden sheets print DD/MM/YY. Day-first is assumed whenever both leading
    fields could be a month, and the ambiguity is reported rather than hidden —
    an estimator reading '05/09/25' deserves to know the pipeline guessed.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""

    m = _TEXT_DATE_RE.match(raw)
    if m:
        d, mon, y = int(m.group(1)), m.group(2)[:3].upper(), _expand_year(int(m.group(3)))
        if mon in _MONTHS:
            try:
                return date(y, _MONTHS[mon], d).isoformat(), ""
            except ValueError:
                return "", f"date {raw!r} is not a real calendar date"

    m = _NUMERIC_DATE_RE.match(raw)
    if not m:
        return "", f"date {raw!r} not in a recognised format — left blank for manual entry"

    a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
    note = ""
    if a > 31:                       # YYYY-MM-DD
        y, mth, d = _expand_year(a), b, c
    else:                            # DD/MM/YY (day-first)
        d, mth, y = a, b, _expand_year(c)
        if d <= 12 and mth <= 12 and d != mth:
            note = (f"date {raw!r} is ambiguous (DD/MM vs MM/DD); "
                    f"read day-first as {d:02d}/{mth:02d} — verify against the sheet")
    try:
        return date(y, mth, d).isoformat(), note
    except ValueError:
        return "", f"date {raw!r} is not a real calendar date"


_DRAWING_NO_CLEAN_RE = re.compile(r"[^A-Za-z0-9\-_/]")


def clean_drawing_no(text: str) -> str:
    """Strip decoration but keep the exact character sequence of the number."""
    t = (text or "").strip().replace("–", "-").replace("—", "-")
    t = re.sub(r"^(DRAWING\s*(NUMBER|NO\.?)\s*[:\-]?\s*)", "", t, flags=re.IGNORECASE)
    return _DRAWING_NO_CLEAN_RE.sub("", t).strip("-_/ ")


def clean_revision(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^(REV(ISION)?\.?\s*[:\-]?\s*)", "", t, flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9]", "", t).upper()


def clean_project_name(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


# =====================================================================
# Whole-sheet grammars
# =====================================================================
# With text blocks located geometrically (see extractors/vector_text.py), one
# montage pass transcribes every title-sized line on the sheet. Interpretation
# then happens here, in deterministic regex — so supporting a new element type
# costs zero extra model calls.
#
# Everything below reads *lines the model transcribed*, never pixels.

CURB_WALL_RE = re.compile(
    r"CURB\s*WALL\s*(\d{2,4})\s*THK\s*[x×X]\s*(\d{2,4})\s*HIGH", re.IGNORECASE)

SUMP_RE = re.compile(
    r"SUMP\s*(?:PIT)?\s*(\d{3,4})\s*[x×X]\s*(\d{3,4})", re.IGNORECASE)

# "TOC EL 97.850", "FGL EL. 97.100", "IL 97.200", "HPP 97.600", "BOC 96.100"
LEVEL_RE = re.compile(
    r"\b(TOC|FGL|BOC|HPP|LPP|IL|FFL|NGL)\b\s*\.?\s*(?:EL\.?)?\s*"
    r"(\d{1,3}\.\d{2,3})", re.IGNORECASE)

INSERT_PLATE_RE = re.compile(
    r"INSERT\s*PLATE\s*TYPE\s*([A-Z]\s?\d{1,2}\s?[a-z]?)", re.IGNORECASE)

# "4MM THK ACID RESISTANT", "6MM THK ACID RESISTANT EPOXY COATING"
EPOXY_RE = re.compile(
    r"(\d{1,2})\s*MM\s*THK\s*ACID\s*RESISTANT", re.IGNORECASE)

# Rebar callouts inside section boxes: "(10)-D12", "(3)-D10 CLOSED LINK",
# "8-D20", "D10-100 LINKS", "D10 @ 150 c/c"
REBAR_COUNT_RE = re.compile(
    r"\(?(\d{1,3})\)?\s*[-–—]\s*[DØø]\s?(\d{1,2})\b", re.IGNORECASE)
REBAR_SPACING_RE = re.compile(
    r"[DØø]\s?(\d{1,2})\s*(?:[-–—@]|\s)\s*(\d{2,3})\s*(?:c/c|LINKS|CTS)?",
    re.IGNORECASE)

# "(300 THK)", "300 THK GRADE SLAB", "150 THK BLINDING"
BLINDING_RE = re.compile(r"(\d{2,4})\s*THK\s*(?:PCC|BLINDING)", re.IGNORECASE)

_LEVEL_MIN, _LEVEL_MAX = 0.0, 500.0        # plant levels in metres


def scan_lines(lines: list[str]) -> dict:
    """Apply every grammar to transcribed text lines.

    Returns a dict of findings keyed by element type. Each finding keeps the
    verbatim source line, because an estimator checking a number against the
    sheet needs to know exactly which string it came from.

    Only `pedestals` and `grade_slab_thickness_mm` carry enough information to
    build a `core.models` object outright. Everything else is reported for the
    human to complete — a curb wall callout gives thickness and height but not
    its run length, and inventing that length is how a BOQ goes wrong quietly.
    """
    found: dict = {
        "pedestals": [], "curb_walls": [], "sumps": [], "levels": [],
        "insert_plates": [], "epoxy": [], "rebar": [], "thicknesses": [],
    }
    seen_ped: set[str] = set()

    for raw in lines:
        line = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not line:
            continue

        ped = parse_pedestal_callout(line)
        if ped:
            ok, _ = validate_pedestal(ped["tag"], ped["length_mm"],
                                      ped["width_mm"], ped["quantity"])
            if ok and ped["tag"] not in seen_ped:
                seen_ped.add(ped["tag"])
                found["pedestals"].append({**ped, "raw_text": line})

        for m in CURB_WALL_RE.finditer(line):
            found["curb_walls"].append({
                "thickness_mm": float(m.group(1)), "height_mm": float(m.group(2)),
                "raw_text": line})

        for m in SUMP_RE.finditer(line):
            found["sumps"].append({
                "length_mm": float(m.group(1)), "width_mm": float(m.group(2)),
                "raw_text": line})

        for m in LEVEL_RE.finditer(line):
            val = float(m.group(2))
            if _LEVEL_MIN <= val <= _LEVEL_MAX:
                found["levels"].append({
                    "kind": m.group(1).upper(), "level_m": val, "raw_text": line})

        for m in INSERT_PLATE_RE.finditer(line):
            found["insert_plates"].append({
                "type": re.sub(r"\s+", "", m.group(1)), "raw_text": line})

        for m in EPOXY_RE.finditer(line):
            found["epoxy"].append({
                "thickness_mm": float(m.group(1)), "raw_text": line})

        for m in REBAR_COUNT_RE.finditer(line):
            n, dia = int(m.group(1)), int(m.group(2))
            if 1 <= n <= 200 and dia in (6, 8, 10, 12, 16, 20, 25, 28, 32, 40):
                found["rebar"].append({"count": n, "diameter_mm": dia,
                                       "spacing_mm": None, "raw_text": line})
        for m in REBAR_SPACING_RE.finditer(line):
            dia, sp = int(m.group(1)), int(m.group(2))
            if dia in (6, 8, 10, 12, 16, 20, 25, 28, 32, 40) and 50 <= sp <= 500:
                found["rebar"].append({"count": None, "diameter_mm": dia,
                                       "spacing_mm": sp, "raw_text": line})

        thk = parse_thickness_mm(line)
        if thk and MIN_SLAB_THK_MM <= thk <= MAX_SLAB_THK_MM:
            found["thicknesses"].append({"thickness_mm": thk, "raw_text": line})
        for m in BLINDING_RE.finditer(line):
            found["thicknesses"].append({"thickness_mm": float(m.group(1)),
                                         "raw_text": line, "kind": "blinding"})

    # De-duplicate the repeat-heavy categories (a level appears on many details).
    for key, fields in (("levels", ("kind", "level_m")),
                        ("insert_plates", ("type",)),
                        ("epoxy", ("thickness_mm",)),
                        ("curb_walls", ("thickness_mm", "height_mm")),
                        ("sumps", ("length_mm", "width_mm")),
                        ("rebar", ("count", "diameter_mm", "spacing_mm")),
                        ("thicknesses", ("thickness_mm",))):
        seen: set[tuple] = set()
        unique = []
        for item in found[key]:
            sig = tuple(item.get(f) for f in fields)
            if sig not in seen:
                seen.add(sig)
                unique.append(item)
        found[key] = unique
    return found
