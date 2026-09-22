"""Streamlit settings that shape what a viewer sees.

The app is opened by estimators and managers through a shared link. They
should not see Streamlit's developer items — the Deploy button above all — or a
raw error message when something unexpected happens.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / ".streamlit" / "config.toml"


def _client() -> dict:
    return tomllib.loads(CONFIG.read_text()).get("client", {})


def test_the_developer_toolbar_is_hidden():
    """'viewer' hides Deploy and the developer menu for everyone, including on
    localhost, where 'auto' would show them."""
    assert _client().get("toolbarMode") == "viewer"


def test_crash_messages_are_redacted_in_the_browser():
    """The raw message can carry paths and figures; it goes to the log."""
    assert _client().get("showErrorDetails") is False


def test_the_brand_theme_is_still_there():
    theme = tomllib.loads(CONFIG.read_text())["theme"]
    assert theme["primaryColor"] == "#1F4E78"
