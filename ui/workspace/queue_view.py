"""Queue tab: what the worker is reading, and the controls over it.

Drawings are added from the Drawings tab, where they are selected. This tab is
the view of the work itself — progress, pause, stop, cancel, retry — and it
refreshes on its own every two seconds without redrawing the rest of the page.
"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from core import jobstore as JS
from ui import plain
from ui.workspace import common as C


def render() -> None:
    st.subheader("Reading queue")
    st.caption("The worker reads one drawing at a time, in its own process, so "
               "this app stays responsive. You can close the tab: the queue is "
               "on disk, and finished drawings stay finished.")
    model_ok, model_msg = C.model_health()
    if not model_ok:
        plain.show(plain.model_problem(model_msg), level="warning", icon="🔌",
                   extra=C.MODEL_OFFLINE_NOTE + " Drawings that need the model "
                         "will fail until it is back — then press Retry failed.")
    queue_status()


@st.fragment(run_every="2s")
def queue_status() -> None:
    """Live view of the queue. Reruns itself; the rest of the page does not.

    This is what replaced running the batch inside the script. The work happens
    in `bin/worker.py`, so pressing pause or cancel is answered in about two
    seconds rather than at the end of a six-minute drawing.
    """
    owner = C.owner()
    jobs = JS.list_jobs(owner=owner)
    if not jobs:
        st.info("The queue is empty. Tick drawings on the **Drawings** tab and "
                "press **Add to the queue**.")
        return

    s = JS.summary(owner=owner)
    counts = s["counts"]
    running = s["running"]

    if s["paused"]:
        st.warning("Queue paused. The drawing in progress will finish; nothing "
                   "new starts until you resume.", icon="⏸")
    elif counts[JS.QUEUED] and not C.worker_alive(jobs):
        waited = time.time() - min(j.created_at for j in jobs
                                   if j.state == JS.QUEUED)
        if waited > C.WORKER_GRACE_S:
            plain.show(C.no_worker_problem(), level="warning", icon="⏳")
        else:
            st.info("Starting…", icon="⏳")

    # The bar measures how much of the queue has been *processed*, and a failed
    # drawing has been processed. Saying "100%" on its own would read as
    # success, so failures are named beside the number and again above it.
    if counts[JS.FAILED]:
        failed_names = ", ".join(j.drawing_name for j in jobs
                                 if j.state == JS.FAILED)[:200]
        st.warning(f"{counts[JS.FAILED]} drawing(s) did not extract: "
                   f"{failed_names}. The reason is in the Problem column below. "
                   f"Fix it and press Retry failed.", icon="⚠️")
        with st.expander("Technical details", expanded=False):
            for j in jobs:
                if j.state == JS.FAILED and j.error:
                    st.caption(j.drawing_name)
                    st.code(j.error, language=None)
    st.progress(min(1.0, s["percent"] / 100.0),
                text=f"{s['percent']:.0f}% processed — {counts[JS.DONE]} read, "
                     f"{counts[JS.QUEUED]} waiting, {counts[JS.FAILED]} failed")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Done", counts[JS.DONE])
    m2.metric("Waiting", counts[JS.QUEUED])
    m3.metric("Average per drawing",
              C.fmt_duration(s["mean_seconds"]) if s["mean_seconds"] else "—")
    m4.metric("Estimated remaining",
              C.fmt_duration(s["eta_seconds"]) if s["eta_seconds"] else "—")

    if running is not None:
        st.progress(min(1.0, running.progress_pct / 100.0),
                    text=f"▶ {running.drawing_name} — {running.progress_pct:.0f}% "
                         f"· {running.stage or 'working'} · "
                         f"{C.fmt_duration(time.time() - running.started_at)} elapsed")

    c1, c2, c3, c4 = st.columns(4)
    if s["paused"]:
        if c1.button("▶ Resume", use_container_width=True, key="q_resume"):
            JS.set_paused(False, owner=owner)
            st.rerun()
    elif c1.button("⏸ Pause", use_container_width=True, key="q_pause",
                   disabled=not counts[JS.QUEUED]):
        JS.set_paused(True, owner=owner)
        st.rerun()

    if c2.button("⏹ Stop current", use_container_width=True, key="q_stop",
                 disabled=running is None):
        JS.request_cancel(running.id)
        st.rerun()

    if c3.button("✖ Cancel waiting", use_container_width=True, key="q_cancel",
                 disabled=not counts[JS.QUEUED]):
        JS.request_cancel([j.id for j in jobs if j.state == JS.QUEUED])
        st.rerun()

    if c4.button("↻ Retry failed", use_container_width=True, key="q_retry",
                 disabled=not (counts[JS.FAILED] or counts[JS.CANCELLED])):
        JS.retry([j.id for j in jobs
                  if j.state in (JS.FAILED, JS.CANCELLED)])
        st.rerun()

    st.dataframe(pd.DataFrame([{
        "Drawing": j.drawing_name,
        "State": j.state,
        "%": round(j.progress_pct),
        "Stage": j.stage,
        "Time": C.fmt_duration(j.elapsed_s) if j.elapsed_s else "",
        "From cache": "yes" if j.from_cache else "",
        "Problem": plain.job_problem(j.error),
    } for j in jobs]), hide_index=True, use_container_width=True)

    st.caption("Nothing here disappears on its own. Finished rows stay until "
               "you clear them, and the extraction JSON on disk survives even "
               "that — clearing the queue costs no model time to undo.")
    finished = counts[JS.DONE] + counts[JS.FAILED] + counts[JS.CANCELLED]
    if st.session_state.get("_confirm_clear_queue"):
        st.warning(f"Clear {finished} finished row(s) from the queue? The "
                   f"drawings and their saved extractions are untouched, so "
                   f"re-queueing them costs nothing. The combined BOQ is built "
                   f"from these rows, so it empties too.", icon="🧹")
        y, n = st.columns(2)
        if y.button("Yes, clear them", key="q_clear_yes",
                    use_container_width=True):
            JS.clear(owner=owner)
            st.session_state.pop("_confirm_clear_queue", None)
            st.session_state.pop("drawing_picks", None)
            st.session_state.pop("line_picks", None)
            # A whole-app rerun, not a fragment one. The BOQ tab is built from
            # the finished jobs, so redrawing only this fragment would leave it
            # listing drawings the queue no longer has.
            st.rerun()
        if n.button("Keep them", key="q_clear_no", use_container_width=True):
            st.session_state.pop("_confirm_clear_queue", None)
            st.rerun()
    elif st.button("🧹 Clear finished rows", key="q_clear", disabled=not finished):
        st.session_state["_confirm_clear_queue"] = True
        st.rerun()
