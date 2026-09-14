"""Landing on the Extract page.

The entry script used to be a project-setup form: typed fields for the project
name, drawing number, revision and date. It was the first thing anyone saw and
it was the wrong first thing — nobody opens this tool to type a drawing number,
and the model reads it off the title block faster than a person can.
"""
from __future__ import annotations

from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

ROOT = Path(__file__).resolve().parent.parent
from ui.auth import SESSION_KEY as AUTH_KEY          # noqa: E402

HOME = str(ROOT / "Home.py")


class TestTheEntryScript:
    def test_it_forwards_to_the_extract_page(self, monkeypatch):
        """AppTest does not follow a page switch, so the call is recorded."""
        import streamlit as st

        targets: list[str] = []
        monkeypatch.setattr(st, "switch_page", targets.append)

        at = AppTest.from_file(HOME, default_timeout=60)
        at.session_state[AUTH_KEY] = True
        at.run()
        assert not at.exception
        assert targets == ["pages/0_Extract.py"], targets

    def test_it_renders_nothing_of_its_own(self, monkeypatch):
        import streamlit as st

        monkeypatch.setattr(st, "switch_page", lambda *_: None)
        at = AppTest.from_file(HOME, default_timeout=60)
        at.session_state[AUTH_KEY] = True
        at.run()
        assert not at.header
        assert not at.subheader
        assert not at.text_input

    def test_the_gate_still_comes_first(self):
        """A redirect that runs before the password would be a hole."""
        at = AppTest.from_file(HOME, default_timeout=60)
        at.run()
        assert not at.exception
        assert any("Password" in (t.label or "") for t in at.text_input)

    def test_no_typed_project_fields_remain(self):
        """Checked against the code, not the prose.

        The docstring explains what used to be here and names those fields on
        purpose; matching raw text would fail on the explanation itself.
        """
        import ast

        tree = ast.parse(Path(HOME).read_text())
        called = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for gone in ("text_input", "number_input", "selectbox", "subheader",
                     "readiness_panel", "sidebar_summary"):
            assert gone not in called, f"{gone}() is still on the entry script"

    def test_the_identity_fields_live_on_the_extract_page_now(self):
        page = (ROOT / "pages" / "0_Extract.py").read_text()
        assert "Project details" in page
        assert "Save or load this project" in page
