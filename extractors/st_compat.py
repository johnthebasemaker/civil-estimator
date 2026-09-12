"""Streamlit API shims.

The project venv has Streamlit 1.39; the system Python has 1.58. Two widget
arguments were renamed between them, and calling the wrong one raises
TypeError at render time rather than import time — so a page looks fine until
someone opens it. These helpers pick whichever the installed version accepts.
"""
from __future__ import annotations

import inspect

import streamlit as st


def _accepts(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind == p.VAR_KEYWORD for p in params.values())


# st.image: use_column_width (<=1.40) -> use_container_width (>=1.41)
_IMAGE_FULL_WIDTH = ("use_container_width" if _accepts(st.image, "use_container_width")
                     else "use_column_width")


def image(target, data, **kwargs):
    """st.image / col.image with a version-correct full-width flag."""
    full = kwargs.pop("full_width", True)
    if full:
        kwargs[_IMAGE_FULL_WIDTH] = True
    return target.image(data, **kwargs)


def rerun() -> None:
    getattr(st, "rerun", None) or getattr(st, "experimental_rerun")
    (getattr(st, "rerun", None) or st.experimental_rerun)()
