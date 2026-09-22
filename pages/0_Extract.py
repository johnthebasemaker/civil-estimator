"""Civil Estimator — the workspace.

One page, four tabs:

    Drawings  the set, each drawing's status, and one drawing up close
    Queue     what the worker is reading, with pause / stop / cancel / retry
    BOQ       the combined bill from every read drawing, and the project estimate
    Pricing   rates against the project estimate

This file used to hold all of it in 1,400 lines, and change shape depending on
how many drawings were ticked; Input, Review, BOM and Costing were separate
pages beside it. The tabs live in `ui/workspace/` now, one module each, so they
can be tested and guarded on their own. What stays here is the order of things:
the gate, the header, the session bar, the tabs, the sidebar.

The file keeps its name because every launcher, test and saved link opens
`pages/0_Extract.py`, and the entry script forwards here.
"""
from __future__ import annotations

# Repair sys.path before anything heavy is imported: `streamlit run Home.py`
# uses the framework Python, which has no PyMuPDF. See sitepath.py.
import sitepath  # noqa: F401  (import first — it fixes the import path)

import streamlit as st

from ui import kit

# PyMuPDF is the one dependency the framework Python does not carry. sitepath has
# already tried to supply it from the venv; if it still is not importable the
# honest thing is a repair instruction, not a traceback.
try:
    from ui.workspace import (boq_view, common, drawings_view, pricing_view,
                              queue_view)
except ModuleNotFoundError as exc:  # pragma: no cover - environment repair path
    st.set_page_config(page_title="Civil Estimator", layout="wide")
    st.error(f"This page cannot start: {exc}")
    st.code(sitepath.explain() or f"Install {exc.name} and restart the app.",
            language="text")
    st.stop()

st.set_page_config(page_title="Civil Estimator", layout="wide",
                   page_icon=kit.favicon())

# The gate comes before anything that reads or writes the project.
kit.require_login()

project = common.project()
kit.page_header("Drawing → BOQ", project,
                "Read drawings, pick what goes in the bill, price it.")
common.session_bar()

# Labels are fixed on purpose: Streamlit identifies a tab by its label, so a
# label with a live count in it would snap the view back to the first tab every
# time the count changed.
TABS = ("📄 Drawings", "⏳ Queue", "🧾 BOQ", "💰 Pricing")
VIEWS = (("Drawings", drawings_view.render), ("Queue", queue_view.render),
         ("BOQ", boq_view.render), ("Pricing", pricing_view.render))

for tab, (name, render) in zip(st.tabs(list(TABS)), VIEWS):
    with tab:
        common.guarded(name, render)

kit.sidebar_summary(project)
drawings_view.sidebar()
kit.sidebar_account()
