"""Suite-wide guards.

Tests drive the real page, and the page writes through to a few stores that
belong to whoever uses this machine. The queue, the cache and the upload folder
are sandboxed by the modules that need them; the classification store is
touched from so many places that it is sandboxed here, for every test. Before
this, one test refiled a real drawing as Brown Field on every run.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _own_classification_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CIVIL_ESTIMATOR_CLASSIFICATIONS",
                       str(tmp_path / "classifications.json"))


@pytest.fixture(autouse=True)
def _tabs_fail_loudly(monkeypatch):
    """In the app, a tab that raises is reported in words and the other tabs
    carry on. In a test that would turn every bug into a quiet pass, so the
    workspace re-raises instead (ui/workspace/common.py, `guarded`)."""
    monkeypatch.setenv("CIVIL_ESTIMATOR_STRICT", "1")
