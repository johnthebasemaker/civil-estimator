#!/usr/bin/env python
"""Extraction worker.

Runs beside the web app and does the slow work: claim a drawing from the queue,
read it, save the result, repeat. The app never calls the model itself, which is
the whole point — a Streamlit script that is busy for six minutes is a frozen
tab with no way to press cancel.

    venv/bin/python bin/worker.py                 # run until stopped
    venv/bin/python bin/worker.py --once          # drain the queue and exit
    venv/bin/python bin/worker.py --owner alice   # only that person's jobs

Three behaviours are worth knowing about:

* **Cancel is cooperative.** The queue records a request; this process notices it
  between model calls and stops cleanly. Nothing is killed from outside, because
  a job interrupted mid-write leaves a workbook that looks finished and is not.
* **The cache is checked first.** A drawing whose bytes have been read before,
  under the same profile, is served from `output/cache` in milliseconds. Reissue
  the drawing and the hash changes, so the model runs again.
* **A sheet with a text layer needs no model at all.** Those finish in a fifth of
  a second, so the worker keeps going when Ollama is down and only reports a
  model problem on a drawing that actually needs one.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import extract_cache as CACHE                  # noqa: E402
from core import jobstore as JS                          # noqa: E402
from extractors import qwen_vision as QV                 # noqa: E402
from extractors.ollama_client import OllamaClient        # noqa: E402

POLL_SECONDS = 2.0
OUTPUT_DIR = Path("output")


class Cancelled(Exception):
    """Raised out of the progress callback to unwind a running extraction."""


_stopping = False


def _stop(signum, _frame):
    global _stopping
    _stopping = True
    print(f"\n[worker] signal {signum} — finishing the current drawing, "
          f"then stopping.", flush=True)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _served_from_cache(job: JS.Job) -> bool:
    """Whether this job can be answered without reading the drawing at all."""
    if job.force_rerun:
        return False
    return CACHE.load(job.fingerprint) is not None


def _reads_from_text(pdf: Path) -> bool:
    """Whether this sheet costs no model calls."""
    try:
        import fitz

        from extractors import text_layer as TL
        with fitz.open(pdf) as doc:
            return TL.has_usable_text(doc[0])
    except Exception:                                    # noqa: BLE001
        return False


def process(job: JS.Job, client: OllamaClient | None, *, db_path=None) -> str:
    """Run one job. Returns a one-word outcome for the log."""
    named = OUTPUT_DIR / f"{job.path.stem}_extraction.json"

    if not job.force_rerun:
        cached = CACHE.load(job.fingerprint)
        if cached is not None:
            CACHE.store(job.fingerprint, cached, also_named=named)
            JS.finish(job.id, json_path=str(named), from_cache=True, db_path=db_path)
            return "cache"

    if not job.path.is_file():
        JS.fail(job.id, f"the file is no longer at {job.path}", db_path=db_path)
        return "missing"

    def progress(label: str, done: int, total: int) -> None:
        # Model calls are the only thing slow enough to be worth reporting, and
        # the extractor calls this before each one. Cap at 99: the last percent
        # belongs to writing the result, not to reading the drawing.
        pct = min(99.0, 100.0 * done / max(total, 1))
        if not JS.heartbeat(job.id, progress_pct=pct,
                            stage=f"{label} ({done}/{total})", db_path=db_path):
            raise Cancelled(label)

    JS.heartbeat(job.id, progress_pct=1.0, stage="opening the drawing",
                 db_path=db_path)
    try:
        result = QV.extract_from_pdf(job.path, profile=job.profile,
                                     client=client, progress=progress)
    except Cancelled:
        JS.mark_cancelled(job.id, db_path=db_path)
        return "cancelled"
    except Exception as exc:                             # noqa: BLE001
        JS.fail(job.id, f"{type(exc).__name__}: {exc}", db_path=db_path)
        traceback.print_exc()
        return "failed"

    JS.heartbeat(job.id, progress_pct=99.0, stage="saving", db_path=db_path)
    CACHE.store(job.fingerprint, result, also_named=named)
    JS.finish(job.id, json_path=str(named), db_path=db_path)
    return "done"


def run(*, once: bool = False, owner: str | None = None, poll: float = POLL_SECONDS,
        db_path=None, model: str = "") -> int:
    wid = worker_id()
    print(f"[worker] {wid} started. Polling every {poll:g}s. "
          f"Ctrl-C stops after the current drawing.", flush=True)

    client: OllamaClient | None = None
    warned_no_model = False
    processed = 0

    while not _stopping:
        JS.reap_stale(db_path=db_path)
        job = JS.claim_next(wid, owner=owner, db_path=db_path)
        if job is None:
            if once:
                break
            time.sleep(poll)
            continue

        # A drawing that is not there needs no model and no excuses: say the
        # true reason rather than blaming whatever is checked next.
        if not job.path.is_file() and not _served_from_cache(job):
            JS.fail(job.id, f"the file is no longer at {job.path}", db_path=db_path)
            print(f"[worker] missing   {job.drawing_name}", flush=True)
            processed += 1
            continue

        # Only build a model client when a drawing actually needs one. Two kinds
        # do not: a drawing already in the cache, and a sheet that kept its text
        # layer. So a set that has been read once runs again with Ollama off.
        needs_model = (
            not _served_from_cache(job)
            and not _reads_from_text(job.path)
        )
        if client is None and needs_model:
            client = OllamaClient(model=model) if model else OllamaClient()
            ok, msg = client.health()
            if not ok:
                if not warned_no_model:
                    print(f"[worker] {msg}", flush=True)
                    warned_no_model = True
                JS.fail(job.id, f"the vision model is unavailable: {msg}",
                        db_path=db_path)
                client = None
                processed += 1
                continue

        started = time.time()
        outcome = process(job, client, db_path=db_path)
        processed += 1
        print(f"[worker] {outcome:<9} {job.drawing_name}  "
              f"{time.time() - started:.1f}s", flush=True)

    print(f"[worker] stopped after {processed} job(s).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="drain the queue and exit (useful in tests and cron)")
    ap.add_argument("--owner", default=None,
                    help="only take jobs queued by this owner")
    ap.add_argument("--poll", type=float, default=POLL_SECONDS)
    ap.add_argument("--model", default="", help="override the Ollama model")
    ap.add_argument("--db", default=None, help="job database path")
    args = ap.parse_args(argv)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    return run(once=args.once, owner=args.owner, poll=args.poll,
               db_path=args.db, model=args.model)


if __name__ == "__main__":
    raise SystemExit(main())
