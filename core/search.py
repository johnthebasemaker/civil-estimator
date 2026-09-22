"""The line-item search rule for the combined BOQ.

It used to live in `tests/helpers_search.py`, imported by the page — production
code reaching into the test folder, because the page was a script and the
matcher had nowhere else to go. The workspace is modules now, so it lives with
the rest of the logic that is tested without Streamlit.
"""
from __future__ import annotations

SEARCH_FIELDS = ("Drawing", "Source", "Group", "Description", "UoM")


def search_rows(rows: list[dict], query: str) -> list[dict]:
    """Rows matching every word typed, across drawing, source, group, description.

    Every word rather than the whole phrase: people type the way they think of
    an item — "rebar 0107", "epoxy sump" — and a substring match on the joined
    phrase finds neither. Case and word order do not matter.
    """
    words = [w for w in (query or "").lower().split() if w]
    if not words:
        return list(rows)
    out = []
    for row in rows:
        haystack = " ".join(str(row.get(f, "")) for f in SEARCH_FIELDS).lower()
        if all(word in haystack for word in words):
            out.append(row)
    return out
