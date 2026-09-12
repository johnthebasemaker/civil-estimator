"""Open-vocabulary item discovery.

Everything else in this package looks for things we named in advance —
pedestals, grade slabs, sumps. That is the wrong way round. A drawing is not
obliged to contain the elements we happened to think of, and the five sheets
that produced no quantities (…-0101 through …-0105) are full of perfectly good
takeoff content that no preset name matched: grout beds, acid-resistant
coating, insert plates, anchor-reinforcement bars, lugs, compacted fill.

So this module never asks "is there a pedestal on this sheet". It asks "what
has a specification printed next to it", and takes the item's name from the
drawing's own words.

The load-bearing assumption, and the reason this does not degenerate into
scraping every string on an A0:

    An item worth a BOQ line carries a number on the drawing.

A thickness, a diameter, a bar count, a bolt size, a pair of plan dimensions, a
compaction percentage. Text with no number attached is a title, a reference, a
note or a centreline label — it tells an estimator where to look, not what to
buy. So the grammars below recognise the *shape of a specification* and then
read outward to collect the words the draughtsman put beside it.

What this module will not do is invent quantities. It reports what the sheet
states and marks everything else "confirm" — a per-unit volume where the
geometry is fully printed, a bare specification where it is not. A count read
off a plan is not a takeoff quantity: the same element is labelled again in
section, so marks are counted for review in text_layer.count_position_marks and
never multiplied through to a number here.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field

# ---------------------------------------------------------------- vocabulary
# The only fixed vocabulary in this module is drafting furniture: words that
# describe the *drawing* rather than the works. Excluding these is safe in a way
# that enumerating element names is not — no BOQ has ever priced a scale bar.
FURNITURE_RE = re.compile(
    r"""(?xi)
    ^\s*(?:
        SECTION\b | DETAIL\b | SCALE\b | \(?SCALE | ELEVATION\s+[A-Z]?-?[A-Z]?\s*$ |
        VIEW\b | LOOKING\s+(?:NORTH|SOUTH|EAST|WEST) | KEY\s*PLAN |
        (?:PLANT|TRUE)\s+NORTH | PREVAILING\s+WIND | MAKKAH |
        REV(?:ISION)?\b | DRAWING\s*(?:NUMBER|NO) | DRG\.?\s*NO |
        SHEET\b | TITLE\s*$ | REMARK | NOTES?\s*:? | LEGEND | ISSUED\s+FOR |
        REACTION\s+TABLE | MAX\.?\s*(?:COMPRESSION|TENSION) | MOMENT\b |
        HORIZONTAL\b | DIMENSIONS?\s+SHALL\s+BE | REFER\s+(?:TO\s+)?(?:DWG|DRAWING) |
        FOR\s+(?:DETAILS?|ORIENTATION) | CLIENT | CONTRACTOR\s*$ | APPROVED |
        CHECKED | DESIGNED | DRAWN\b | PROPRIETARY | CONFIDENTIAL |
        THIS\s+DOCUMENT | INSIGNIA
    )""",
)

# A Maaden drawing number, a plant coordinate, a grid tick. All numeric, none
# of them a specification.
REFERENCE_RE = re.compile(
    r"(?i)\b[A-Z]{2}-\d{3}-[0-9A-Z]{4}-[A-Z]{2}-[A-Z]{2}-[A-Z]{3}-\d{3,4}"
    r"|\b[NE]\s?\d{6}\.\d{3}\b"
    r"|\b\d{5}-[A-Z]{3}-\d{3}\b")

# Tokens that end a description: the draughtsman's connectives and qualifiers.
BOUNDARY = {
    "OF", "TO", "AT", "FOR", "REFER", "SEE", "AND", "WITH", "PER", "AS",
    "EL", "EL.", "ELEV", "LEVEL", "C/C", "CTS", "@", "=", "-", "&",
    "TYP", "(TYP)", "TYP.", "NTS", "(NTS)", "UNO", "(UNO)", "VARING",
    "VARYING", "SHALL", "BE", "IS", "ARE", "ON", "IN", "BY", "FROM",
    "SECTION", "DETAIL", "PLAN", "SCALE",
}

# An element mark — P1, F12, E1, A3b, GS-01. Part of a description, never a
# reason to stop reading one, even though it contains a digit.
MARK_RE = re.compile(r"^[A-Z]{1,3}-?\d{1,2}[a-z]?$")

WORD_RE = re.compile(r"^[A-Z][A-Z&/'.\-]*$", re.IGNORECASE)

MAX_PHRASE_WORDS = 5

# Plausibility envelopes. A "thickness" of 4 mm is an epoxy coat; one of 9 m is
# a misread of a chainage.
THK_MIN, THK_MAX = 1.0, 3000.0
DIA_MIN, DIA_MAX = 5.0, 2000.0
BOX_MIN, BOX_MAX = 50.0, 100_000.0
LEVEL_MIN, LEVEL_MAX = 0.0, 500.0
BAR_DIAMETERS = {6, 8, 10, 12, 16, 20, 25, 28, 32, 40}


# ------------------------------------------------------------------ patterns
# "30 THK GROUT", "150 THK BLINDING", "4MM THK ACID RESISTANT EPOXY COATING"
THK_LEADING_RE = re.compile(
    r"(?i)\b(\d{1,4}(?:\.\d{1,2})?)\s*(?:MM)?\s*TH(?:K|ICK|IK)\.?\b")
# "IP 20mm THK", "HDPE LINER 1.5MM THK" — same pattern, phrase read backwards.

# "20mm DIA LUGS", "Ø25 DOWELS"
DIA_RE = re.compile(r"(?i)\b(\d{1,4}(?:\.\d{1,2})?)\s*(?:MM)?\s*(?:DIA\.?|DIAMETER|[Øø])\b")

# "P1(600x500)", "SUMP 900 x 900 x 1200", "450/500"
BOX_RE = re.compile(
    r"(?i)\(?\b(\d{2,5})\s*[x×*]\s*(\d{2,5})(?:\s*[x×*]\s*(\d{2,5}))?\b\)?")

# "16-D25", "(3)-D10", "8-D20"
BAR_COUNT_RE = re.compile(r"(?i)\(?(\d{1,3})\)?\s*[-–—]\s*[DTØø]\s?(\d{1,2})\b")
# "D10 @ 150", "D16-150 C/C", "D10-100 LINKS"
BAR_SPACING_RE = re.compile(r"(?i)\b[DTØø]\s?(\d{1,2})\s*(?:@|[-–—]|\s)\s*(\d{2,3})\b")

# "(2 Nos./SET)", "5-SETS", "2-SETS"
PER_SET_RE = re.compile(r"(?i)\((\d{1,3})\s*N[o0]s?\.?\s*/\s*SET\)")
SETS_RE = re.compile(r"(?i)\b(\d{1,3})\s*[-–—]?\s*SETS?\b")

# "12 Nos", "2Nos", "11 No."
COUNT_RE = re.compile(r"(?i)\b(\d{1,4})\s*N\s*[o0O]\s*s?\.?\b")

# "95% COMPACTED SOIL"
PERCENT_RE = re.compile(r"(?i)\b(\d{1,3})\s*%")

# "AR-(4)-B-M36-1345-N2" — anchor rod assembly: count, grade, size, length
ASSEMBLY_RE = re.compile(
    r"(?i)\b([A-Z]{2})-\((\d{1,2})\)-([A-Z])-M(\d{1,3})-(\d{2,5})-N(\d{1,2})\b")

# "BOBP EL. 98.380", "TOC EL 97.550", "BOC EL. 95.500", "U/S OF BASE PLATE EL. 98.350"
LEVEL_RE = re.compile(
    r"(?i)\b(TOC|BOC|BOBP|BOP|FGL|FFL|NGL|IL|HPP|LPP|TOS|U/S)\b[^0-9]{0,24}?"
    r"(\d{1,3}\.\d{2,3})\b")

# Concrete grade: "C35", "M30", "GRADE 40"
GRADE_RE = re.compile(r"(?i)\b(?:GRADE\s+)?([CM])\s?(\d{2})\b(?!\s*[x×*])")


# ------------------------------------------------------------------ dataclass
@dataclass
class DiscoveredItem:
    """One thing the drawing specifies, named in the drawing's own words."""
    kind: str                       # thickness | diameter | box | rebar | ...
    description: str                # e.g. "Acid resistant epoxy coating"
    spec: dict = field(default_factory=dict)
    uom: str = "Nos"
    qty: float | None = None        # None whenever the sheet does not state it
    qty_basis: str = ""             # how it was derived, or what is missing
    occurrences: int = 1
    raw_texts: list[str] = field(default_factory=list)
    grid_refs: list[str] = field(default_factory=list)

    @property
    def confirm(self) -> bool:
        """True when a person must supply something before this can be priced."""
        return self.qty is None

    @property
    def signature(self) -> tuple:
        return (self.kind, self.description.lower(),
                tuple(sorted((k, _round(v)) for k, v in self.spec.items())))

    def as_row(self) -> dict:
        return {
            "kind": self.kind,
            "description": self.description,
            "spec": dict(self.spec),
            "uom": self.uom,
            "qty": self.qty,
            "basis": self.qty_basis,
            "occurrences": self.occurrences,
            "confirm": self.confirm,
            "source": self.raw_texts[0] if self.raw_texts else "",
            "grid_ref": self.grid_refs[0] if self.grid_refs else "",
        }


def _round(v):
    return round(v, 4) if isinstance(v, float) else v


# ------------------------------------------------------------------- phrases
def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.strip()) if t]


def _clean_token(tok: str) -> str:
    return tok.strip("(),:;·.").strip()


def _is_boundary(tok: str) -> bool:
    bare = _clean_token(tok).upper()
    if not bare:
        return True
    if bare in BOUNDARY:
        return True
    if MARK_RE.match(bare):
        return False                      # marks belong to the description
    if any(ch.isdigit() for ch in bare):
        return True                       # another specification: stop
    return not WORD_RE.match(bare)


def _phrase_after(tokens: list[str], start: int) -> str:
    out = []
    for tok in tokens[start:start + MAX_PHRASE_WORDS + 2]:
        if _is_boundary(tok):
            break
        out.append(_clean_token(tok))
        if len(out) >= MAX_PHRASE_WORDS:
            break
    return " ".join(out).strip()


def _phrase_before(tokens: list[str], end: int) -> str:
    out = []
    for tok in reversed(tokens[max(0, end - MAX_PHRASE_WORDS - 2):end]):
        if _is_boundary(tok):
            break
        out.insert(0, _clean_token(tok))
        if len(out) >= MAX_PHRASE_WORDS:
            break
    return " ".join(out).strip()


def _token_index(text: str, char_pos: int) -> int:
    """Which whitespace token contains this character offset."""
    return len(_tokens(text[:char_pos])) if char_pos else 0


def _titlecase(phrase: str) -> str:
    """Drawings shout. A BOQ does not."""
    words = phrase.split()
    out = []
    for w in words:
        if MARK_RE.match(w.upper()) or len(w) <= 3 and w.isupper():
            out.append(w.upper())          # keep marks and abbreviations: IP, PCC, P1
        else:
            out.append(w.capitalize())
    return " ".join(out)


# ----------------------------------------------------------------- unit rules
# UoM follows from what was measured, not from what the item is called. A
# thickness describes a layer and layers are paid by area; three dimensions
# describe a body and bodies are paid by volume. That is the whole reason this
# module can put a unit against an item it has never heard of.
UOM_BY_KIND = {
    "thickness": "m2",
    "diameter": "Nos",
    "box2": "m2",
    "box3": "m3",
    "rebar": "Nos",
    "rebar_spacing": "m",
    "assembly": "Nos",
    "compaction": "m3",
    "count": "Nos",
    "set": "Nos",
}

# Words that specify rather than name. They must not end up in an item's
# description: "4MM THK ACID RESISTANT" describes acid-resistant coating that is
# 4 mm thick, and calling the item "THK Acid Resistant" helps nobody.
SPEC_WORDS = {
    "THK", "THICK", "THIK", "MM", "M", "DIA", "DIAMETER", "NOS", "NO", "NOS.",
    "CM", "KG", "SET", "SETS", "C/C", "CTS", "EL", "MPA", "N/MM2", "QTY",
    "EQ", "NTS", "UNO", "MAX", "MIN", "APPROX",
}


def _is_spec_word(token: str) -> bool:
    """Whether a token measures rather than names, "NOS./SET" included."""
    bare = _clean_token(token).upper()
    if bare in SPEC_WORDS:
        return True
    parts = [q for q in re.split(r"[/.]", bare) if q]
    return bool(parts) and all(q in SPEC_WORDS for q in parts)


def _mk(kind: str, description: str, spec: dict, raw: str, grid: str,
        *, uom: str | None = None, qty: float | None = None,
        basis: str = "") -> DiscoveredItem:
    return DiscoveredItem(
        kind=kind,
        description=description,
        spec=spec,
        uom=uom or UOM_BY_KIND.get(kind, "Nos"),
        qty=qty,
        qty_basis=basis,
        raw_texts=[raw],
        grid_refs=[grid] if grid else [],
    )


def _skip(line: str) -> bool:
    if len(line) < 3:
        return True
    if FURNITURE_RE.match(line):
        return True
    if REFERENCE_RE.search(line):
        return True
    # A line of prose from the general notes: long, sentence-like, and read by
    # the estimator directly rather than priced as an item.
    if len(line) > 110 and line.count(" ") > 14:
        return True
    return False


def descriptive(text: str) -> str:
    """The words of a label that name something, or "" if it names nothing.

    A drawing annotation is often split across stacked entities — "TYP DETAIL OF
    PEDESTAL" above "P1(600x500) 2Nos" — so the number is on one line and the
    name on another. This recovers the name half.
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text or _skip(text) or len(text) > 60:
        return ""
    words = []
    for tok in _tokens(text):
        bare = _clean_token(tok)
        if not bare:
            continue
        upper = bare.upper()
        if upper in BOUNDARY or _is_spec_word(bare):
            continue
        if WORD_RE.match(bare) or MARK_RE.match(upper):
            words.append(bare)
    return " ".join(words[:MAX_PHRASE_WORDS]).strip()


def _continue_phrase(phrase: str, line: str, following: list[str]) -> str:
    """Extend a phrase that ran to the end of its label onto the next one.

    "4MM THK ACID RESISTANT" and "EPOXY COATING (TYP)" are two text entities of
    one callout. Joining them only when the first ran out of line — rather than
    always — is what keeps "30 THK GROUT" from swallowing its neighbour.
    """
    if not phrase or not following:
        return phrase
    if not line.rstrip().upper().endswith(phrase.split()[-1].upper()):
        return phrase                      # the label continued sideways instead
    tail = descriptive(following[0])
    if not tail or any(ch.isdigit() for ch in tail):
        return phrase
    have = {w.upper() for w in phrase.split()}
    extra = [w for w in tail.split() if w.upper() not in have]
    if not extra:
        return phrase                      # the next label repeats this one
    words = (phrase + " " + " ".join(extra)).split()
    return " ".join(words[:MAX_PHRASE_WORDS + 2])


def _spec_suffix(**kw) -> str:
    """The measured part of a description, so every row reads on its own."""
    bits = []
    if kw.get("thickness_mm") is not None:
        bits.append(f"{kw['thickness_mm']:g} mm thick")
    if kw.get("diameter_mm") is not None:
        bits.append(f"{kw['diameter_mm']:g} mm dia")
    if kw.get("percent") is not None:
        bits.append(f"{kw['percent']:g}% compaction")
    return ", " + ", ".join(bits) if bits else ""


def scan_line(line: str, grid_ref: str = "", context=None,
              following=None) -> list[DiscoveredItem]:
    """Every specification printed on one label.

    `context` is the other labels drawn in the same stack; it names a number
    whose own label carries no words. `following` is the part of that stack
    drawn below this label, used to finish a phrase that ran off the end.

    Context is deliberately not offered to the reinforcement grammars. A bar
    callout sits in a thicket of unrelated labels, and borrowing a neighbour's
    words produced descriptions like "Reference Documents — D16 @ 150". "D25
    bars" with the callout beside it is less informative and more true.
    """
    line = re.sub(r"\s+", " ", str(line or "")).strip()
    if _skip(line):
        return []

    toks = _tokens(line)
    following = list(following or [])
    items: list[DiscoveredItem] = []
    fallback = ""
    for sibling in context or []:
        name = descriptive(sibling)
        if name and not any(ch.isdigit() for ch in name):
            fallback = name
            break

    # --- boxes: two or three dimensions in a row -----------------------------
    boxes: list[tuple[float, float, float | None]] = []
    for m in BOX_RE.finditer(line):
        dims = [float(g) for g in m.groups() if g is not None]
        if not all(BOX_MIN <= d <= BOX_MAX for d in dims):
            continue
        name = (_phrase_before(toks, _token_index(line, m.start()))
                or _phrase_after(toks, _token_index(line, m.end()))
                or fallback)
        if not name:
            continue                      # a chained plan dimension, not an item
        if len(dims) == 3:
            boxes.append((dims[0], dims[1], dims[2]))
            items.append(_mk(
                "box3", f"{_titlecase(name)} {dims[0]:g}x{dims[1]:g}x{dims[2]:g}",
                {"length_mm": dims[0], "width_mm": dims[1], "height_mm": dims[2]},
                line, grid_ref, qty=round(dims[0] * dims[1] * dims[2] / 1e9, 4),
                basis=f"{dims[0]:g}x{dims[1]:g}x{dims[2]:g} mm, per unit"))
        else:
            boxes.append((dims[0], dims[1], None))
            items.append(_mk(
                "box2", f"{_titlecase(name)} {dims[0]:g}x{dims[1]:g}",
                {"length_mm": dims[0], "width_mm": dims[1]},
                line, grid_ref, qty=round(dims[0] * dims[1] / 1e6, 4),
                basis=f"{dims[0]:g}x{dims[1]:g} mm, plan area per unit"))

    # --- thickness, read in both directions ---------------------------------
    for m in THK_LEADING_RE.finditer(line):
        thk = float(m.group(1))
        if not THK_MIN <= thk <= THK_MAX:
            continue
        after = _phrase_after(toks, _token_index(line, m.end()))
        name = after or _phrase_before(toks, _token_index(line, m.start())) or fallback
        if not name:
            continue
        if after:
            name = _continue_phrase(after, line, following)
        label = _titlecase(name) + _spec_suffix(thickness_mm=thk)
        if boxes and boxes[0][2] is None:
            # A thickness quoted beside plan dimensions is a complete body.
            L, W, _ = boxes[0]
            items.append(_mk("box3", label,
                             {"length_mm": L, "width_mm": W, "thickness_mm": thk},
                             line, grid_ref, uom="m3",
                             qty=round(L * W * thk / 1e9, 4),
                             basis=f"{L:g}x{W:g}x{thk:g} mm, per unit"))
            continue
        items.append(_mk("thickness", label, {"thickness_mm": thk}, line, grid_ref,
                         basis=f"{thk:g} mm thick — area not stated on this sheet"))

    # --- diameter ------------------------------------------------------------
    for m in DIA_RE.finditer(line):
        dia = float(m.group(1))
        if not DIA_MIN <= dia <= DIA_MAX:
            continue
        after = _phrase_after(toks, _token_index(line, m.end()))
        name = _continue_phrase(after, line, following) if after else fallback
        if not name:
            continue
        items.append(_mk("diameter",
                         _titlecase(name) + _spec_suffix(diameter_mm=dia),
                         {"diameter_mm": dia}, line, grid_ref,
                         basis=f"{dia:g} mm dia — count not stated on this sheet"))

    # --- reinforcement (no borrowed context; see the docstring) --------------
    for m in BAR_COUNT_RE.finditer(line):
        n, dia = int(m.group(1)), int(m.group(2))
        if dia not in BAR_DIAMETERS or not 1 <= n <= 200:
            continue
        parent = _phrase_after(toks, _token_index(line, m.end()))
        label = f"D{dia} bars — {n} Nos per element"
        if parent:
            label += f" ({_titlecase(parent)})"
        items.append(_mk("rebar", label,
                         {"count": float(n), "diameter_mm": float(dia)},
                         line, grid_ref, qty=float(n),
                         basis=f"{m.group(0).strip()} — bars per element"))

    for m in BAR_SPACING_RE.finditer(line):
        dia, sp = int(m.group(1)), int(m.group(2))
        if dia not in BAR_DIAMETERS or not 50 <= sp <= 500:
            continue
        parent = _phrase_after(toks, _token_index(line, m.end()))
        label = f"D{dia} bars @ {sp} c/c"
        if parent:
            label += f" ({_titlecase(parent)})"
        items.append(_mk("rebar_spacing", label,
                         {"diameter_mm": float(dia), "spacing_mm": float(sp)},
                         line, grid_ref,
                         basis=f"D{dia} @ {sp} c/c — run length not stated"))

    # --- anchor / bolt assemblies -------------------------------------------
    for m in ASSEMBLY_RE.finditer(line):
        prefix, n, grade, size, length, nuts = (
            m.group(1).upper(), int(m.group(2)), m.group(3).upper(),
            int(m.group(4)), int(m.group(5)), int(m.group(6)))
        items.append(_mk(
            "assembly", f"Anchor assembly {prefix} M{size} x {length} (grade {grade})",
            {"count": float(n), "size_mm": float(size), "length_mm": float(length),
             "nuts": float(nuts)},
            line, grid_ref, qty=float(n),
            basis=f"{m.group(0)} — {n} bolts per set"))

    # --- compaction ----------------------------------------------------------
    for m in PERCENT_RE.finditer(line):
        pct = int(m.group(1))
        if not 50 <= pct <= 100:
            continue
        after = _phrase_after(toks, _token_index(line, m.end()))
        name = _continue_phrase(after, line, following) if after else fallback
        if not name:
            continue
        items.append(_mk("compaction",
                         _titlecase(name) + _spec_suffix(percent=pct),
                         {"percent": float(pct)}, line, grid_ref,
                         basis=f"{pct}% compaction — volume not stated on this sheet"))

    # --- explicit counts, "(2 Nos./SET)" -------------------------------------
    for m in PER_SET_RE.finditer(line):
        n = int(m.group(1))
        name = _phrase_before(toks, _token_index(line, m.start())) or fallback
        if not name:
            continue
        items.append(_mk("set", f"{_titlecase(name)} — {n} Nos per set",
                         {"per_set": float(n)}, line, grid_ref, qty=float(n),
                         basis=f"{n} Nos per set"))

    return items


# ------------------------------------------------------------------ levels
def scan_levels(lines) -> list[dict]:
    """Elevations printed on the sheet. Context for a takeoff, not an item.

    Kept out of the item list because a level is never a BOQ line — but a pair
    of them is often the only place a height is written down, which is what
    `height_candidates` uses.
    """
    out: dict[tuple, dict] = {}
    for entry in lines:
        text, grid = _unpack(entry)
        text = re.sub(r"\s+", " ", text).strip()
        for m in LEVEL_RE.finditer(text):
            kind, val = m.group(1).upper(), float(m.group(2))
            if not LEVEL_MIN <= val <= LEVEL_MAX:
                continue
            out.setdefault((kind, val), {"kind": kind, "level_m": val,
                                         "raw_text": text, "grid_ref": grid})
    return sorted(out.values(), key=lambda d: (d["kind"], d["level_m"]))


# Levels that mark the top and the bottom of a concrete element. A height is the
# difference between one of each, never between two of the same.
_TOP_LEVELS = {"BOBP", "TOC", "TOS", "U/S", "FFL"}
_BOTTOM_LEVELS = {"BOC", "BOP", "NGL", "FGL"}


def height_candidates(levels: list[dict]) -> list[dict]:
    """Heights implied by the elevations on the sheet.

    On a details sheet the pedestal height is rarely dimensioned; it is implied
    by "BOBP EL. 98.380" over "BOC EL. 95.500". Every top/bottom pairing is
    offered with both source strings, because which bottom belongs to which top
    is a question about the section, and the section is what the engineer has in
    front of them. Nothing here is chosen automatically, and nothing here
    reaches a quantity without someone saying so.
    """
    tops = [l for l in levels if l["kind"] in _TOP_LEVELS]
    bottoms = [l for l in levels if l["kind"] in _BOTTOM_LEVELS]
    out = []
    for t in tops:
        for b in bottoms:
            h = round(t["level_m"] - b["level_m"], 3)
            if 0.1 <= h <= 15.0:
                out.append({"height_m": h,
                            "top": f"{t['kind']} EL. {t['level_m']:.3f}",
                            "bottom": f"{b['kind']} EL. {b['level_m']:.3f}"})
    out.sort(key=lambda d: d["height_m"])
    return out


# ------------------------------------------------------------- specifications
# The general notes are where a sheet states its materials. These are the
# numbers an estimator needs to pick a rate, and they are stated in prose rather
# than as callouts, so they are read separately from the item grammars.
STRENGTH_RE = re.compile(r"(?i)\b(\d{2,3})\s*(N\s?/\s?mm2|MPa)\b")
CONCRETE_GRADE_RE = re.compile(r"(?i)\b(?:GRADE\s+)?(C\d{2})\b(?!\s*[x×*])")


def scan_specifications(lines) -> list[dict]:
    """Material specifications stated in the sheet's own notes."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for entry in lines:
        text, _ = _unpack(entry)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 12:
            continue
        for m in STRENGTH_RE.finditer(text):
            key = (m.group(1), m.group(2).replace(" ", "").lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"value": float(m.group(1)),
                        "unit": m.group(2).replace(" ", ""),
                        "text": text})
    return out


def scan_grades(lines) -> list[str]:
    """Concrete grades named on the sheet.

    Only the C-prefixed form is accepted. "M36" on these sheets is a bolt
    thread, not a mix — it comes from "AR-(4)-B-M36-1345-N2" — and reporting it
    as a concrete grade is worse than reporting no grade at all.
    """
    seen: "OrderedDict[str, None]" = OrderedDict()
    for entry in lines:
        text, _ = _unpack(entry)
        for m in CONCRETE_GRADE_RE.finditer(re.sub(r"\s+", " ", text)):
            grade = m.group(1).upper()
            if 10 <= int(grade[1:]) <= 80:
                seen.setdefault(grade, None)
    return list(seen)


# ------------------------------------------------------------------ public
def _unpack(entry) -> tuple[str, str]:
    """Accept a plain string, or a block dict carrying its grid reference."""
    if isinstance(entry, dict):
        return (str(entry.get("text") or entry.get("line") or ""),
                str(entry.get("grid_ref") or ""))
    return str(entry or ""), ""


def _iter_lines(source):
    """Flatten blocks or a flat line list into (text, grid_ref, siblings, below).

    A block is a stack of labels drawn together, so its lines can name one
    another; a flat list has no such structure and gets none.
    """
    for entry in source or []:
        texts, grid = None, ""
        if isinstance(entry, dict) and isinstance(entry.get("lines"), list):
            texts = [str(l or "") for l in entry["lines"]]
            grid = str(entry.get("grid_ref") or "")
        elif hasattr(entry, "texts"):                 # text_layer.TextBlockT
            texts = list(entry.texts)
        if texts is None:
            text, grid = _unpack(entry)
            yield text, grid, [], []
            continue
        for i, line in enumerate(texts):
            yield line, grid, texts[:i] + texts[i + 1:], texts[i + 1:]


def discover(source) -> list[DiscoveredItem]:
    """Every specified item on a sheet, merged across repeated callouts.

    `source` is whatever the extractor produced: span-level blocks from
    `text_layer.label_blocks`, the vision transcript blocks, or a flat list of
    lines. A detail repeated in five sections is one item seen five times, not
    five items — the occurrence count is kept because it tells a reviewer how
    prominent a detail is, and it is never used as a quantity.
    """
    merged: "OrderedDict[tuple, DiscoveredItem]" = OrderedDict()
    for text, grid, siblings, below in _iter_lines(source):
        for item in scan_line(text, grid, siblings, below):
            sig = item.signature
            cur = merged.get(sig)
            if cur is None:
                merged[sig] = item
                continue
            cur.occurrences += 1
            if item.raw_texts[0] not in cur.raw_texts:
                cur.raw_texts.append(item.raw_texts[0])
            for g in item.grid_refs:
                if g and g not in cur.grid_refs:
                    cur.grid_refs.append(g)
    return list(merged.values())


def summarise(source) -> dict:
    """Everything this module can say about a sheet, ready for the workbook."""
    rows = list(_iter_lines(source))
    flat = [{"text": t, "grid_ref": g} for t, g, _, _ in rows]
    items = discover(source)
    levels = scan_levels(flat)
    return {
        "items": [i.as_row() for i in items],
        "levels": levels,
        "heights": height_candidates(levels),
        "grades": scan_grades(flat),
        "specifications": scan_specifications(flat),
        "measured": sum(1 for i in items if i.qty is not None),
        "to_confirm": sum(1 for i in items if i.qty is None),
    }
