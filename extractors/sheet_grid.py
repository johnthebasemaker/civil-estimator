"""Drawing-border grid references.

Every ISO drawing sheet carries a frame divided into numbered columns and
lettered rows, printed in the border. It is how engineers point at things:
"the P3 detail in C-3", not "83% across and 22% down". Anything this tool
extracts should be quotable that way, or a checker cannot find it on the print.

The grid is **measured from the sheet**, not assumed. Border labels are glyphs
sitting in the margin strips, so clustering their positions gives the real cell
pitch. On the reference A0 that comes out at exactly 1/16 on both axes, with
cell centres at (i + 0.5)/16 — and, unusually, the row letters include I and O,
which the ISO convention normally omits. Assuming the convention would have put
every reference below row H off by one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import fitz

# This border uses the plain alphabet. Many borders skip I and O (they read as
# 1 and 0); `detect_grid` picks whichever fits the number of rows it measures.
ALPHABET_WITH_IO = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ALPHABET_NO_IO = "ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass(frozen=True)
class SheetGrid:
    """Border grid of a drawing sheet, in normalised sheet fractions."""
    columns: int = 16
    rows: int = 16
    columns_right_to_left: bool = True     # Maaden A0: "16" at the left edge
    letters: str = ALPHABET_WITH_IO
    x0: float = 0.0
    x1: float = 1.0
    y0: float = 0.0
    y1: float = 1.0
    detected: bool = False

    def ref(self, x: float, y: float) -> str:
        return f"{self.row_letter(y)}-{self.column_number(x)}"

    def column_number(self, x: float) -> int:
        frac = _clamp01((x - self.x0) / max(self.x1 - self.x0, 1e-9))
        idx = min(int(frac * self.columns), self.columns - 1)
        return self.columns - idx if self.columns_right_to_left else idx + 1

    def row_letter(self, y: float) -> str:
        frac = _clamp01((y - self.y0) / max(self.y1 - self.y0, 1e-9))
        idx = min(int(frac * self.rows), self.rows - 1)
        return self.letters[min(idx, len(self.letters) - 1)]

    def ref_for_rect(self, rect_norm) -> str:
        """Grid square of a rectangle; a span reads as "C-7 → C-8"."""
        if not rect_norm or len(rect_norm) != 4:
            return ""
        x0, y0, x1, y1 = rect_norm
        start, end = self.ref(x0, y0), self.ref(x1, y1)
        if start == end:
            return start
        return f"{start} → {end}"

    def cell_bounds(self, x: float, y: float) -> tuple[float, float, float, float]:
        """Normalised bounds of the cell containing a point (for drawing it)."""
        cw = (self.x1 - self.x0) / self.columns
        ch = (self.y1 - self.y0) / self.rows
        ci = min(int(_clamp01((x - self.x0) / max(self.x1 - self.x0, 1e-9))
                     * self.columns), self.columns - 1)
        ri = min(int(_clamp01((y - self.y0) / max(self.y1 - self.y0, 1e-9))
                     * self.rows), self.rows - 1)
        return (self.x0 + ci * cw, self.y0 + ri * ch,
                self.x0 + (ci + 1) * cw, self.y0 + (ri + 1) * ch)


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


DEFAULT_GRID = SheetGrid()

# Border labels are small glyphs; the text-block detector's 9 pt floor is too
# high for them.
_MIN_LABEL_PT, _MAX_LABEL_PT = 3.0, 30.0
_BAND = 0.020          # how far into the sheet the border strip reaches
_CLUSTER_TOL = 0.012   # merge label glyphs closer than this (same character)


def _cluster(values: list[float], tol: float) -> list[float]:
    values = sorted(values)
    out, run = [], [values[0]]
    for v in values[1:]:
        if v - run[-1] <= tol:
            run.append(v)
        else:
            out.append(sum(run) / len(run))
            run = [v]
    out.append(sum(run) / len(run))
    return out


def _median_gap(centres: list[float]) -> float | None:
    if len(centres) < 3:
        return None
    gaps = sorted(centres[i + 1] - centres[i] for i in range(len(centres) - 1))
    return gaps[len(gaps) // 2] or None


def detect_grid(page: fitz.Page, fallback: SheetGrid = DEFAULT_GRID) -> SheetGrid:
    """Measure the border grid from the sheet, falling back to the default.

    Reads the pitch of the border labels rather than their values — we cannot
    OCR "12" from a curve, but we can see that labels repeat every 1/16 of the
    sheet, which is all that is needed to place a point in a cell.
    """
    rect = page.rect
    w, h = rect.width, rect.height
    m = page.rotation_matrix

    rows_y: list[float] = []
    cols_x: list[float] = []
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"] * m)
        if not (_MIN_LABEL_PT <= r.height <= _MAX_LABEL_PT
                and 1.0 <= r.width <= _MAX_LABEL_PT):
            continue
        cx, cy = (r.x0 + r.x1) / 2 / w, (r.y0 + r.y1) / 2 / h
        if r.x1 < _BAND * w or r.x0 > (1 - _BAND) * w:
            rows_y.append(cy)
        if r.y1 < _BAND * h or r.y0 > (1 - _BAND) * h:
            cols_x.append(cx)

    n_rows = _count_from_pitch(rows_y)
    n_cols = _count_from_pitch(cols_x)
    if not n_rows or not n_cols:
        return fallback

    letters = ALPHABET_WITH_IO if n_rows > len(ALPHABET_NO_IO) else ALPHABET_WITH_IO
    if n_rows <= len(ALPHABET_NO_IO):
        # Both alphabets can supply this many letters; the reference border uses
        # the plain one, so prefer it unless the row count only fits the ISO
        # variant of a standard sheet.
        letters = ALPHABET_WITH_IO
    return SheetGrid(columns=n_cols, rows=n_rows,
                     columns_right_to_left=fallback.columns_right_to_left,
                     letters=letters, detected=True)


def _count_from_pitch(centres: list[float]) -> int | None:
    """Cell count implied by the spacing of detected border labels."""
    if len(centres) < 4:
        return None
    gap = _median_gap(_cluster(centres, _CLUSTER_TOL))
    if not gap or gap <= 0.005:
        return None
    n = round(1.0 / gap)
    return n if 4 <= n <= 40 else None
