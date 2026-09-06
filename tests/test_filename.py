"""Tests for filename builder."""
from __future__ import annotations
from datetime import datetime
from core.filename import sanitise, build_output_filename, build_output_path


def test_sanitise_removes_slash():
    assert sanitise("MD/522/8110") == "MD5228110"

def test_sanitise_removes_colon_and_quotes():
    assert sanitise('a:b"c') == "abc"

def test_sanitise_collapses_whitespace():
    assert sanitise("MD 522 8110") == "MD_522_8110"

def test_sanitise_empty_returns_untitled():
    assert sanitise("") == "untitled"
    assert sanitise(None or "") == "untitled"

def test_build_output_filename_format():
    dt = datetime(2026, 9, 6, 14, 30)
    result = build_output_filename("MD-522-8110-EG-CV-LAD-0107", dt)
    assert result == "MD-522-8110-EG-CV-LAD-0107_20260906_1430.xlsx"

def test_build_output_filename_current_time():
    result = build_output_filename("TEST-001")
    assert result.startswith("TEST-001_")
    assert result.endswith(".xlsx")
    # YYYYMMDD_HHMM = 13 chars, +5 for _.xlsx
    assert len(result) == len("TEST-001_") + 13 + len(".xlsx")

def test_build_output_path():
    dt = datetime(2026, 9, 6, 14, 30)
    p = build_output_path("TEST-001", "output", dt)
    assert p.name == "TEST-001_20260906_1430.xlsx"
    assert p.parent.name == "output"