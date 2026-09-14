"""Civil Estimator — entry point.

This file exists because Streamlit needs a script to start from, and because
every launcher, systemd unit and test in the project names it. It is no longer
a page.

What used to be here was a project-setup screen: a form of typed fields for the
project name, drawing number, revision and date, next to a readiness panel. It
was the first thing anyone saw and it was the wrong first thing. Nobody opens
this tool to type a drawing number — they open it with a drawing, and the model
reads the number off the title block in the time it would take to type it. The
form's real effect was to make the app look like something only an estimator
could start.

So the entry point now does two things and stops: check the password, then hand
straight over to Drawing → BOQ. The project identity it used to collect is
edited in that page's "Project details" box, and saving or loading a project
moved to the session bar at the top of it, beside Clear session.
"""
from __future__ import annotations

# Repair sys.path before anything heavy is imported: `streamlit run Home.py`
# uses the framework Python, which has no PyMuPDF. See sitepath.py.
import sitepath  # noqa: F401  (import first — it fixes the import path)

import streamlit as st

from ui import kit

st.set_page_config(page_title="Civil Estimator", layout="wide",
                   page_icon=kit.favicon())

# The gate is checked here as well as on every page. A login on one page alone
# would be bypassed by navigating straight to another page's URL.
kit.require_login()

# require_login already hides this script from the page list: it has nothing to
# show, and a nav entry that bounces you elsewhere the moment you click it reads
# as a bug.
st.switch_page("pages/0_Extract.py")
