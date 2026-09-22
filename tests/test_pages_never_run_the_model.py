"""The web app never reads a drawing itself.

Every reading goes through the queue and `bin/worker.py`. The single-drawing
view used to call the extractor inline, inside the Streamlit process — which is
what froze the tab, heated the laptop and ignored the cache, all while the queue
built to prevent exactly that sat unused beside it.

Checked on the syntax tree rather than by searching text, so a comment or a
docstring that mentions the extractor is fine; only a real call fails.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Anything that does model work or runs a whole extraction.
FORBIDDEN_CALLS = {
    "extract_from_pdf",     # the whole pipeline
    "extract_from_image",
    "generate",             # OllamaClient.generate — one model call
    "chat",
}

WEB_FILES = sorted(
    [ROOT / "Home.py"]
    + list((ROOT / "pages").glob("*.py"))
    + list((ROOT / "ui").rglob("*.py")))


def _calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        if name in FORBIDDEN_CALLS:
            found.append((node.lineno, name))
    return found


def test_there_are_web_files_to_check():
    assert any(p.name == "0_Extract.py" for p in WEB_FILES)


@pytest.mark.parametrize("path", WEB_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_web_file_runs_the_model(path):
    calls = _calls(path)
    assert not calls, (f"{path.relative_to(ROOT)} calls {calls} — put the "
                       f"drawing on the queue instead (JS.enqueue)")


def test_the_worker_is_where_it_happens():
    """The rule has somewhere to point: the worker does call the extractor."""
    assert any(name == "extract_from_pdf" for _, name in _calls(ROOT / "bin" / "worker.py"))
