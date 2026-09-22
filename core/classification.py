"""Which site condition each drawing belongs to.

Three conditions, and the difference between them is money rather than
geometry: a pedestal on open ground, the same pedestal inside a running plant,
and making good one that already exists are the same cubic metre of concrete at
three different rates. So the classification is an input, not something to be
inferred from the drawing — nothing on the sheet reliably says which it is.

Kept in one small JSON file rather than in the session, because the workbook is
rebuilt from saved extractions by `bin/rebuild_set.py` long after the browser
tab has gone, and a classification that only existed in a Streamlit session
would be lost by then.

Keyed by the drawing's **file name**, not its path. The same PDF routinely sits
in both `Drawings/` and `output/uploads/` — the app copies uploads into the
latter — and a path key made those two copies two different drawings, so a
classification set in the browser never reached the workbook rebuilt from the
saved extraction. The file name is what identifies a drawing to the people
using this; two different drawings sharing one file name would be a filing
problem long before it was ours.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

STORE = Path("output/classifications.json")
# Like the queue and the cache, the store can live elsewhere: on a server's
# data volume, or — the reason it exists — in a test's own folder, so running
# the suite never refiles somebody's real drawings.
STORE_ENV = "CIVIL_ESTIMATOR_CLASSIFICATIONS"


def store_path() -> Path:
    return Path(os.environ.get(STORE_ENV) or STORE)

GREEN_FIELD = "Green Field"
BROWN_FIELD = "Brown Field"
REPAIR = "Repair"
CHOICES = (GREEN_FIELD, BROWN_FIELD, REPAIR)
DEFAULT = GREEN_FIELD


def _key(pdf_path: str | Path) -> str:
    """One key per drawing, wherever the file happens to be sitting."""
    return Path(str(pdf_path)).name or str(pdf_path)


def load(store: Path | None = None) -> dict[str, str]:
    path = Path(store or store_path())
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # A damaged file must not stop a workbook being written; an unfiled
        # drawing is reported as unfiled, which is visible and recoverable.
        return {}
    # Entries written before the key became a file name are folded in, so an
    # upgrade does not quietly lose what somebody already filed.
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, str):
            out[Path(key).name or key] = value
    return out


def get(pdf_path: str | Path, *, store: Path | None = None,
        default: str = DEFAULT) -> str:
    value = load(store).get(_key(pdf_path), default)
    return value if value in CHOICES else default


def set_for(pdf_path: str | Path, value: str, *, store: Path | None = None) -> None:
    if value not in CHOICES:
        raise ValueError(f"{value!r} is not one of {CHOICES}")
    path = Path(store or store_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load(path)
    data[_key(pdf_path)] = value
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def forget(pdf_path: str | Path, *, store: Path | None = None) -> None:
    path = Path(store or store_path())
    data = load(path)
    if data.pop(_key(pdf_path), None) is not None:
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
