"""The Area 3 search rule, extracted so it can be tested without Streamlit.

`pages/0_Extract.py` is a script, not a module — importing it outside a running
app executes the whole page. The matcher is small and worth testing directly,
so it lives here and the page imports it.
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
