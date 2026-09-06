"""Filename builder for output workbooks.

Format: {sanitised_drawing_no}_{YYYYMMDD}_{HHMM}.xlsx
Sanitises the drawing number for filesystem safety across macOS/Windows/Linux.
"""
from __future__ import annotations
import re
from datetime import datetime
from pathlib import Path


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitise(text: str) -> str:
    """Strip filesystem-unsafe chars; collapse whitespace to underscore."""
    if not text:
        return "untitled"
    cleaned = _UNSAFE.sub("", text).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "untitled"


def build_output_filename(drawing_no: str, when: datetime | None = None) -> str:
    """Return e.g. 'MD-522-8110-EG-CV-LAD-0107_20260906_1430.xlsx'."""
    when = when or datetime.now()
    stem = sanitise(drawing_no)
    return f"{stem}_{when.strftime('%Y%m%d_%H%M')}.xlsx"


def build_output_path(drawing_no: str, base_dir: str | Path = "output",
                      when: datetime | None = None) -> Path:
    return Path(base_dir) / build_output_filename(drawing_no, when)