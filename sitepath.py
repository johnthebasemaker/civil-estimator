"""Make the project's own dependencies importable however the app was started.

The failure this exists to stop:

    ModuleNotFoundError: No module named 'fitz'
      File "pages/0_Extract.py", line 25, in <module>
        from extractors import pdf_to_image as R

`streamlit` on PATH is the macOS framework Python, which has Streamlit but no
PyMuPDF. PyMuPDF lives in ./venv. So `streamlit run Home.py` starts the app,
serves the sidebar, and then dies the moment someone opens the Extract page.
`bin/app.sh` avoids that by using venv/bin/streamlit, but a README note is not
a fix — anyone who types the obvious command still gets the traceback.

So repair the path instead of documenting around it. The venv was created from
the framework interpreter (`venv/pyvenv.cfg` -> home = .../Versions/3.12), so
the two share an ABI and the venv's compiled extensions load cleanly under
either one.

Two rules keep this safe rather than clever:

1. **Append, never prepend.** Whatever the running interpreter already has
   wins. Only genuinely missing modules are taken from the venv. That matters
   because the framework Python carries Streamlit 1.58 and pandas 3.0 while the
   venv pins 1.39 and 2.2 — shadowing a library that is already imported is how
   a working app turns into an unexplainable one.

2. **Only on an exact version match.** `site-packages` is per minor version and
   holds compiled wheels. If the interpreter is 3.13 and the venv is 3.12, the
   path is not added and `missing()` reports it, so the page can say what is
   wrong instead of importing something that will segfault.
"""
from __future__ import annotations

import sys
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / "venv"

# Modules the app needs that the framework Python is known not to have.
REQUIRED = ("fitz",)

_applied: str | None = None


def site_packages() -> Path:
    """Where this interpreter's wheels would live inside the project venv."""
    v = sys.version_info
    return VENV / "lib" / f"python{v.major}.{v.minor}" / "site-packages"


def in_project_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def ensure() -> str | None:
    """Add the venv's site-packages as a fallback. Returns a note, or None.

    Idempotent, and a no-op when already running under venv/bin/python.
    """
    global _applied
    if _applied is not None or in_project_venv():
        return _applied or None

    sp = site_packages()
    if not sp.is_dir():
        _applied = ""
        return None

    entry = str(sp)
    if entry not in sys.path:
        sys.path.append(entry)

    _applied = (
        f"Running under {sys.executable}, not the project venv. "
        f"Missing packages are being loaded from {sp}. "
        f"Start with ./bin/app.sh to use the pinned versions."
    )
    return _applied


def missing() -> list[str]:
    """Required modules still unimportable after ensure() — for a clear message."""
    out = []
    for name in REQUIRED:
        try:
            if find_spec(name) is None:
                out.append(name)
        except (ImportError, ValueError):
            out.append(name)
    return out


def explain() -> str:
    """A repair instruction naming the actual problem, for display in the UI."""
    gone = missing()
    if not gone:
        return ""
    sp = site_packages()
    if not sp.is_dir():
        return (
            f"The project virtual environment has no {sp.parent.name} directory, so "
            f"{', '.join(gone)} cannot be loaded. This interpreter is Python "
            f"{sys.version_info.major}.{sys.version_info.minor}; the venv was built for a "
            f"different version. Rebuild it:\n\n"
            f"    python3 -m venv venv && venv/bin/pip install -r requirements.txt\n"
            f"    ./bin/app.sh"
        )
    return (
        f"{', '.join(gone)} is not installed in this environment "
        f"({sys.executable}) or in the project venv. Install the "
        f"dependencies and start the app with its own launcher:\n\n"
        f"    venv/bin/pip install -r requirements.txt\n"
        f"    ./bin/app.sh"
    )


ensure()
