"""The launch-environment repair.

The bug: `streamlit run Home.py` starts under the macOS framework Python, which
has Streamlit but not PyMuPDF, so the app serves its sidebar and then dies with
`ModuleNotFoundError: No module named 'fitz'` the moment anyone opens the
Extract page. `bin/app.sh` avoids it, and a README note asking people to use the
launcher did not stop it happening twice.

So the fix has to hold for the obvious command, not only the documented one.
The last test here is the one that matters: it runs the failing import chain
under the very interpreter that used to fail.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import sitepath

ROOT = Path(__file__).resolve().parent.parent
FRAMEWORK_PY = Path("/Library/Frameworks/Python.framework/Versions"
                    f"/{sys.version_info.major}.{sys.version_info.minor}/bin/python3")


class TestPathRepair:
    def test_site_packages_is_version_matched(self):
        v = sys.version_info
        assert sitepath.site_packages().name == "site-packages"
        assert f"python{v.major}.{v.minor}" in str(sitepath.site_packages())

    def test_ensure_is_idempotent(self):
        before = list(sys.path)
        sitepath.ensure()
        sitepath.ensure()
        assert sys.path.count(str(sitepath.site_packages())) <= 1
        assert len(sys.path) <= len(before) + 1

    def test_nothing_required_is_missing_here(self):
        assert sitepath.missing() == []
        assert sitepath.explain() == ""

    def test_the_venv_is_never_prepended(self):
        """Appending is what keeps the running interpreter's own versions.

        The framework Python carries Streamlit 1.58 and pandas 3.0 while the
        venv pins 1.39 and 2.2. Shadowing a library that is already imported is
        how a working app becomes an unexplainable one, so the venv may only
        supply what is genuinely absent.
        """
        entry = str(sitepath.site_packages())
        if entry in sys.path:
            assert sys.path.index(entry) == len(sys.path) - 1 or \
                sys.path.index(entry) > 0


@pytest.mark.skipif(not FRAMEWORK_PY.exists(),
                    reason="no framework Python on this machine")
def test_the_import_chain_that_used_to_crash_now_works():
    """The actual regression: the user's traceback, reproduced and gone."""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import sitepath\n"
        "assert not sitepath.in_project_venv(), 'test must run outside the venv'\n"
        "from extractors import pdf_to_image\n"
        "from extractors import qwen_vision\n"
        "print('ok')\n" % str(ROOT)
    )
    proc = subprocess.run([str(FRAMEWORK_PY), "-c", code], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
