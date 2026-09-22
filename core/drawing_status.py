"""What state a drawing is in, in one word.

The drawings list used to show a file name and a size, so the only way to find
out whether a drawing had been read was to tick it and wait for the page to
change shape. The badge answers that before anyone clicks:

  Not read      no reading of this version of the file exists
  Queued        waiting for the worker
  Reading 40%   the worker is on it now
  Needs review  read, with gaps or assumptions to check (the same count the
                workbook's Gaps_and_Assumptions sheet carries)
  Ready         read, and nothing flagged
  Failed        the last attempt did not finish

Pure: it takes the facts and returns the badge, so every rule is tested
without Streamlit, a queue or a PDF.
"""
from __future__ import annotations

from dataclasses import dataclass

from core import jobstore as JS

NOT_READ, QUEUED, READING = "not_read", "queued", "reading"
NEEDS_REVIEW, READY, FAILED = "needs_review", "ready", "failed"

LABELS = {
    NOT_READ: "Not read", QUEUED: "Queued", READING: "Reading",
    NEEDS_REVIEW: "Needs review", READY: "Ready", FAILED: "Failed",
}

# Colour family per state. Kept to four so the list reads at a glance: grey for
# nothing yet, blue for in motion, amber for a person's attention, green / red
# for done and not done.
TONES = {
    NOT_READ: "idle", QUEUED: "busy", READING: "busy",
    NEEDS_REVIEW: "warn", READY: "good", FAILED: "bad",
}

# Worst first: the order a set is worked through.
ORDER = (FAILED, NEEDS_REVIEW, READING, QUEUED, NOT_READ, READY)

# How each state reads in a count — "11 to review", not "11 needs review".
COUNT_WORDS = {
    NOT_READ: "not read", QUEUED: "queued", READING: "being read",
    NEEDS_REVIEW: "to review", READY: "ready", FAILED: "failed",
}


@dataclass(frozen=True)
class Status:
    key: str
    detail: str = ""                 # a short explanation for the tooltip
    progress: float | None = None    # percent, while reading

    @property
    def label(self) -> str:
        if self.key == READING and self.progress is not None:
            return f"{LABELS[READING]} {self.progress:.0f}%"
        return LABELS[self.key]

    @property
    def tone(self) -> str:
        return TONES[self.key]

    @property
    def has_reading(self) -> bool:
        return self.key in (NEEDS_REVIEW, READY)


def status_for(*, latest: JS.Job | None, has_saved: bool,
               attention: int = 0) -> Status:
    """The badge for one drawing.

    `latest` is the newest queue job for the drawing, `has_saved` whether a
    saved reading of the file *as it is now* exists, and `attention` how many
    gaps and assumptions that reading leaves for a person.

    Work in motion wins, because it is what is about to change. A failed attempt
    wins over an older reading, because the attempt was asked for and did not
    happen — showing "Ready" would hide that. A cancelled attempt is no news, so
    the drawing shows whatever it had before.
    """
    if latest is not None and latest.state == JS.RUNNING:
        return Status(READING, "being read now", latest.progress_pct)
    if latest is not None and latest.state == JS.QUEUED:
        return Status(QUEUED, "waiting for the reader")
    if latest is not None and latest.state == JS.FAILED:
        return Status(FAILED, "the last attempt did not finish")
    if has_saved:
        if attention:
            return Status(NEEDS_REVIEW,
                          f"{attention} gap(s) or assumption(s) to check")
        return Status(READY, "read, nothing flagged")
    return Status(NOT_READ, "no reading of this version of the file")


def tally(statuses) -> dict[str, int]:
    """How many drawings are in each state, worst first, zeros left out."""
    counts = {key: 0 for key in ORDER}
    for s in statuses:
        counts[s.key] += 1
    return {key: n for key, n in counts.items() if n}
