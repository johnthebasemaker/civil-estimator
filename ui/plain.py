"""Plain language for problems, with the technical detail one click away.

The people opening this app through a shared link are estimators and managers,
not whoever keeps the machine running. `<urlopen error [Errno 61] Connection
refused>` and a line of shell tells them nothing they can act on, and makes a
working app look broken. So every problem is said twice:

  * a headline in words — what happened, and what still works;
  * the raw message and the fix, inside a "Technical details" expander, for the
    person who can actually run the command.

The translation functions are pure, so they are tested without Streamlit. Only
`show` draws anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# How the app is started, for the technical detail. `bin/ce` runs the web app
# and the worker together; the worker line is for someone running them apart.
START_ALL = "./bin/ce start"
START_WORKER = "venv/bin/python bin/worker.py"
START_MODEL = "ollama serve"


@dataclass(frozen=True)
class Problem:
    headline: str                  # one sentence anybody can read
    detail: str = ""               # the raw message, verbatim
    fix: str = ""                  # command(s) for the maintainer, if any


def model_problem(message: str, model: str = "qwen2.5vl:7b") -> Problem:
    """Why the reading service cannot be used, from `OllamaClient.health()`."""
    text = (message or "").strip()
    low = text.lower()
    if "not pulled" in low:
        name = _quoted(text) or model
        return Problem("The drawing-reading model isn't installed on this "
                       "computer yet.", text, f"ollama pull {name}")
    if "timed out" in low or "timeout" in low:
        return Problem("The drawing-reading service isn't responding.", text,
                       START_MODEL)
    if any(k in low for k in ("cannot reach", "connection refused", "errno 61",
                              "urlopen error", "failed to establish",
                              "name or service not known")):
        return Problem("The drawing-reading service is offline.", text,
                       f"{START_MODEL}\nollama pull {model}")
    return Problem("The drawing-reading service reported a problem.", text,
                   START_MODEL)


def job_problem(error: str) -> str:
    """A queue row's failure in a few words. The raw text stays on the job."""
    text = (error or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low.startswith("the file is no longer at"):
        return "File not found — it was moved or deleted after it was queued."
    if low.startswith("the vision model is unavailable"):
        return ("The reading service was offline when this drawing's turn came. "
                "Start it, then press Retry failed.")
    if "worker stopped twice" in low:
        return ("Reading stopped part-way twice. The PDF may be damaged — "
                "try opening it, then Retry.")
    if "cancel" in low:
        return "Stopped before it finished."
    return "This drawing could not be read. Retry, or see the technical details."


def show(problem: Problem, *, level: str = "error", icon: str | None = None,
         extra: str = "") -> None:
    """Headline on the page, everything else in a closed expander."""
    import streamlit as st

    say = {"error": st.error, "warning": st.warning,
           "info": st.info}.get(level, st.error)
    headline = problem.headline + (f" {extra}" if extra else "")
    say(headline, icon=icon)
    if problem.detail or problem.fix:
        with st.expander("Technical details", expanded=False):
            if problem.detail:
                st.caption("What the system reported")
                st.code(problem.detail, language=None)
            if problem.fix:
                st.caption("To fix it, on the computer running the app")
                st.code(problem.fix, language="bash")


def _quoted(text: str) -> str:
    match = re.search(r"'([^']+)'", text)
    return match.group(1) if match else ""
