"""Tests for rate_library. Uses monkeypatch to point DB at a temp file."""
from __future__ import annotations
import pytest
from pathlib import Path
from core import rate_library


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_library, "DB_PATH", tmp_path / "rates_test.db")
    yield


def test_set_and_get_rate():
    rate_library.set_rate("Structural Concrete", "m3", 450.0)
    assert rate_library.get_rate("Structural Concrete", "m3") == 450.0

def test_get_missing_rate_returns_zero():
    assert rate_library.get_rate("Nothing", "kg") == 0.0

def test_upsert_updates_value():
    rate_library.set_rate("Rebar (manual BBS)", "kg", 3.5)
    rate_library.set_rate("Rebar (manual BBS)", "kg", 4.0)
    assert rate_library.get_rate("Rebar (manual BBS)", "kg") == 4.0

def test_bulk_set():
    n = rate_library.bulk_set([
        {"category": "Formwork", "unit": "m2", "rate_sar": 45.0},
        {"category": "Earthwork", "unit": "m3", "rate_sar": 25.0},
    ])
    assert n == 2
    assert rate_library.get_rate("Formwork", "m2") == 45.0

def test_get_all_rates():
    rate_library.set_rate("A", "m3", 100)
    rate_library.set_rate("B", "kg", 5)
    rows = rate_library.get_all_rates()
    assert len(rows) == 2
    assert all("updated_at" in r for r in rows)