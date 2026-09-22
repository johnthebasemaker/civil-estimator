"""Durable queue for drawing extractions.

Why this module exists: the batch loop used to run inside the Streamlit script
thread. Streamlit cannot redraw or accept a click while a script is running, so
a twenty-drawing batch froze the tab for however long the model took, and a
cancel button was not merely missing but impossible — there was no moment in
which the app could notice it had been pressed.

Moving the work out is only half the answer. The other half is that the queue
has to outlive both the browser tab and the app process. An engineer who starts
twenty drawings before lunch should be able to close the laptop, and a worker
restart in the middle of drawing nine should cost drawing nine, not the batch.
So the queue is a table, not a list in memory.

SQLite is the right store here and will stay right for a while. The model serves
one request at a time, so there is no write concurrency for a heavier database
to win; what matters is that a claim is atomic, and `BEGIN IMMEDIATE` gives us
that. WAL mode lets the web app read the queue while the worker writes it.

Three states need care rather than cleverness:

* **Claiming.** Two workers must never take the same job. The claim is a single
  immediate transaction that re-checks the state it is transitioning from.
* **Dying.** A worker killed mid-job leaves a row marked running forever. Each
  worker heartbeats; a running job whose worker has gone quiet is returned to
  the queue by `reap_stale`, once, and then it is allowed to fail honestly.
* **Cancelling.** Cancel is a *request*, not a state change. The worker sees the
  flag between model calls and stops. Nothing is killed from outside, because a
  half-written workbook is worse than a slow stop.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# Overridable so a server deployment can put the queue on a persistent volume
# and the tests can run against a throwaway file. Read at call time rather than
# import time, because the app and the worker are separate processes and the
# environment is the only thing they reliably share.
DB_ENV = "CIVIL_ESTIMATOR_JOBS_DB"
DB_PATH = Path("data/jobs.db")


def default_db_path() -> Path:
    return Path(os.environ.get(DB_ENV) or DB_PATH)

# A worker updates its heartbeat before every model call. Vision generation on a
# CPU-only host can genuinely take minutes, so this has to be generous: a
# too-eager reaper double-runs drawings, which costs GPU time and confuses the
# person watching the queue.
HEARTBEAT_STALE_S = 900.0

QUEUED, RUNNING, DONE, FAILED, CANCELLED = (
    "queued", "running", "done", "failed", "cancelled")
TERMINAL = {DONE, FAILED, CANCELLED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT '',
    drawing_path    TEXT NOT NULL,
    drawing_name    TEXT NOT NULL,
    fingerprint     TEXT NOT NULL DEFAULT '',
    profile         TEXT NOT NULL DEFAULT 'thorough',
    page            INTEGER NOT NULL DEFAULT 0,
    force_rerun     INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'queued',
    position        INTEGER NOT NULL DEFAULT 0,
    progress_pct    REAL NOT NULL DEFAULT 0.0,
    stage           TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker_id       TEXT NOT NULL DEFAULT '',
    heartbeat_at    REAL NOT NULL DEFAULT 0,
    requeues        INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    started_at      REAL NOT NULL DEFAULT 0,
    finished_at     REAL NOT NULL DEFAULT 0,
    elapsed_s       REAL NOT NULL DEFAULT 0,
    from_cache      INTEGER NOT NULL DEFAULT 0,
    json_path       TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, position, created_at);
CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id);
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, created_at);

CREATE TABLE IF NOT EXISTS queue_flags (
    owner   TEXT PRIMARY KEY,
    paused  INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Job:
    id: str
    batch_id: str
    owner: str
    drawing_path: str
    drawing_name: str
    fingerprint: str = ""
    profile: str = "thorough"
    page: int = 0                    # zero-based page of the PDF to read
    force_rerun: bool = False
    state: str = QUEUED
    position: int = 0
    progress_pct: float = 0.0
    stage: str = ""
    cancel_requested: bool = False
    worker_id: str = ""
    heartbeat_at: float = 0.0
    requeues: int = 0
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    elapsed_s: float = 0.0
    from_cache: bool = False
    json_path: str = ""
    error: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def path(self) -> Path:
        return Path(self.drawing_path)


def _row_to_job(row: sqlite3.Row) -> Job:
    data = dict(row)
    for flag in ("force_rerun", "cancel_requested", "from_cache"):
        data[flag] = bool(data[flag])
    return Job(**data)


# ------------------------------------------------------------------ plumbing
def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # web app reads while worker writes
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database made by an older build up to the current columns.

    `CREATE TABLE IF NOT EXISTS` never touches a table that is already there, so
    a column added later has to be added by hand. Two processes opening the same
    old file at once can both try; the loser's "duplicate column" is the proof
    the winner succeeded, not an error.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "page" not in columns:
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN page INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


@contextmanager
def _db(db_path: Path | str | None = None):
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write(conn: sqlite3.Connection):
    """An immediate transaction, so two workers cannot claim the same job."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --------------------------------------------------------------- fingerprints
def fingerprint(pdf_path: Path | str, profile: str = "thorough",
                page: int = 0) -> str:
    """Identity of "this drawing, extracted this way".

    Content-addressed rather than name-and-date: a reissued drawing keeps its
    filename, and an mtime changes when a file is merely copied between
    folders. Hashing the bytes means a revision is a different job and a copy is
    the same one.

    The first page hashes exactly as it did before pages were tracked, so every
    extraction saved by an older build is still found. Later pages mix their
    number in, because page 2 of a file is not the same reading as page 1.
    """
    path = Path(pdf_path)
    digest = hashlib.sha256()
    digest.update(profile.encode())
    if page:
        digest.update(f"#page={int(page)}".encode())
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:32]


# ------------------------------------------------------------------ enqueue
def enqueue(paths, *, owner: str = "", profile: str = "thorough",
            force: bool = False, batch_id: str | None = None,
            page: int = 0, db_path=None) -> list[Job]:
    """Queue drawings for extraction. Returns the jobs, in order.

    A drawing already queued or running for this owner is not queued twice:
    people tick the same sheet in two sessions, and a second copy would spend
    the model's time proving the first one right.
    """
    batch_id = batch_id or uuid.uuid4().hex[:12]
    now = time.time()
    jobs: list[Job] = []
    with _db(db_path) as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) AS p FROM jobs").fetchone()
            position = int(row["p"])
            # Compared by resolved path: `Drawings/x.pdf` and its absolute
            # spelling are one drawing, and queueing it twice would spend the
            # model's time proving the first reading right.
            live = {(drawing_key(r["drawing_path"]), r["page"]) for r in conn.execute(
                "SELECT drawing_path, page FROM jobs "
                "WHERE owner = ? AND state IN (?, ?)", (owner, QUEUED, RUNNING))}
            for path in paths:
                path = Path(path)
                if (drawing_key(path), int(page)) in live:
                    continue
                live.add((drawing_key(path), int(page)))
                position += 1
                job = Job(id=uuid.uuid4().hex[:16], batch_id=batch_id, owner=owner,
                          drawing_path=str(path), drawing_name=path.name,
                          fingerprint=fingerprint(path, profile, page),
                          profile=profile, page=int(page),
                          force_rerun=force, position=position, created_at=now)
                conn.execute(
                    "INSERT INTO jobs (id, batch_id, owner, drawing_path, "
                    "drawing_name, fingerprint, profile, page, force_rerun, "
                    "state, position, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job.id, job.batch_id, job.owner, job.drawing_path,
                     job.drawing_name, job.fingerprint, job.profile, job.page,
                     int(job.force_rerun), QUEUED, job.position, job.created_at))
                jobs.append(job)
    return jobs


# ------------------------------------------------------------------ worker
def claim_next(worker_id: str, *, owner: str | None = None, db_path=None) -> Job | None:
    """Take the next queued job, atomically. None when there is nothing to do."""
    now = time.time()
    with _db(db_path) as conn:
        with _write(conn):
            paused = {r["owner"] for r in conn.execute(
                "SELECT owner FROM queue_flags WHERE paused = 1")}
            sql = ("SELECT * FROM jobs WHERE state = ? "
                   + ("AND owner = ? " if owner is not None else "")
                   + "ORDER BY position, created_at")
            args = (QUEUED,) + ((owner,) if owner is not None else ())
            job = None
            for row in conn.execute(sql, args):
                if row["owner"] in paused:
                    continue                  # this owner's queue is on hold
                job = _row_to_job(row)
                break
            if job is None:
                return None
            conn.execute(
                "UPDATE jobs SET state = ?, worker_id = ?, started_at = ?, "
                "heartbeat_at = ?, progress_pct = 0, stage = ?, error = '' "
                "WHERE id = ? AND state = ?",
                (RUNNING, worker_id, now, now, "starting", job.id, QUEUED))
    job.state, job.worker_id, job.started_at, job.heartbeat_at = (
        RUNNING, worker_id, now, now)
    return job


def heartbeat(job_id: str, *, progress_pct: float | None = None,
              stage: str | None = None, db_path=None) -> bool:
    """Record that the worker is alive, and how far along. Returns keep-going.

    False means a cancel has been requested. The worker checks this between
    model calls rather than being killed from outside, because a job stopped
    mid-write leaves a half-built workbook that looks finished.
    """
    now = time.time()
    with _db(db_path) as conn:
        sets = ["heartbeat_at = ?"]
        args: list = [now]
        if progress_pct is not None:
            sets.append("progress_pct = ?")
            args.append(max(0.0, min(100.0, float(progress_pct))))
        if stage is not None:
            sets.append("stage = ?")
            args.append(stage)
        args.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", args)
        row = conn.execute(
            "SELECT cancel_requested, state FROM jobs WHERE id = ?",
            (job_id,)).fetchone()
    if row is None:
        return False
    return not (row["cancel_requested"] or row["state"] in TERMINAL)


def finish(job_id: str, *, json_path: str = "", from_cache: bool = False,
           db_path=None) -> None:
    now = time.time()
    with _db(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, finished_at = ?, progress_pct = 100, "
            "stage = ?, json_path = ?, from_cache = ?, "
            "elapsed_s = CASE WHEN started_at > 0 THEN ? - started_at ELSE 0 END "
            "WHERE id = ?",
            (DONE, now, "done", json_path, int(from_cache), now, job_id))


def fail(job_id: str, error: str, *, db_path=None) -> None:
    now = time.time()
    with _db(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, finished_at = ?, stage = ?, error = ?, "
            "elapsed_s = CASE WHEN started_at > 0 THEN ? - started_at ELSE 0 END "
            "WHERE id = ?",
            (FAILED, now, "failed", str(error)[:2000], now, job_id))


def mark_cancelled(job_id: str, *, db_path=None) -> None:
    now = time.time()
    with _db(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, finished_at = ?, stage = ? WHERE id = ?",
            (CANCELLED, now, "cancelled", job_id))


def reap_stale(*, stale_after: float = HEARTBEAT_STALE_S, db_path=None) -> int:
    """Return jobs whose worker has gone quiet to the queue. Once.

    A job that has already been requeued and died again is failed instead of
    cycling forever — usually it is the drawing, not the worker.
    """
    cutoff = time.time() - stale_after
    with _db(db_path) as conn:
        with _write(conn):
            stale = conn.execute(
                "SELECT id, requeues FROM jobs WHERE state = ? AND heartbeat_at < ?",
                (RUNNING, cutoff)).fetchall()
            requeued = 0
            for row in stale:
                if row["requeues"] >= 1:
                    conn.execute(
                        "UPDATE jobs SET state = ?, finished_at = ?, stage = ?, "
                        "error = ? WHERE id = ?",
                        (FAILED, time.time(), "failed",
                         "the worker stopped twice on this drawing — check the "
                         "worker log and the PDF itself", row["id"]))
                    continue
                conn.execute(
                    "UPDATE jobs SET state = ?, worker_id = '', progress_pct = 0, "
                    "stage = ?, requeues = requeues + 1 WHERE id = ?",
                    (QUEUED, "requeued after a worker stopped", row["id"]))
                requeued += 1
    return requeued


# ------------------------------------------------------------------- control
def request_cancel(job_ids, *, db_path=None) -> int:
    """Ask for a stop. A queued job goes straight to cancelled; a running one is
    flagged and stops itself at the next safe point."""
    ids = [job_ids] if isinstance(job_ids, str) else list(job_ids)
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    now = time.time()
    with _db(db_path) as conn:
        with _write(conn):
            conn.execute(
                f"UPDATE jobs SET cancel_requested = 1 WHERE id IN ({marks}) "
                f"AND state IN (?, ?)", (*ids, QUEUED, RUNNING))
            cur = conn.execute(
                f"UPDATE jobs SET state = ?, finished_at = ?, stage = ? "
                f"WHERE id IN ({marks}) AND state = ?",
                (CANCELLED, now, "cancelled", *ids, QUEUED))
            return cur.rowcount or 0


def set_paused(paused: bool, *, owner: str = "", db_path=None) -> None:
    """Hold this owner's queue. The job in flight finishes; nothing new starts.

    Pausing does not abandon work in progress. Stopping a drawing four model
    calls in throws those minutes away, and the next resume pays for them again.
    """
    with _db(db_path) as conn:
        conn.execute(
            "INSERT INTO queue_flags (owner, paused) VALUES (?, ?) "
            "ON CONFLICT(owner) DO UPDATE SET paused = excluded.paused",
            (owner, int(paused)))


def is_paused(owner: str = "", *, db_path=None) -> bool:
    with _db(db_path) as conn:
        row = conn.execute("SELECT paused FROM queue_flags WHERE owner = ?",
                           (owner,)).fetchone()
    return bool(row and row["paused"])


def retry(job_ids, *, db_path=None) -> int:
    """Put failed or cancelled jobs back at the end of the queue."""
    ids = [job_ids] if isinstance(job_ids, str) else list(job_ids)
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with _db(db_path) as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) AS p FROM jobs").fetchone()
            base = int(row["p"])
            moved = 0
            for i, job_id in enumerate(ids, start=1):
                cur = conn.execute(
                    "UPDATE jobs SET state = ?, position = ?, cancel_requested = 0, "
                    "progress_pct = 0, stage = '', error = '', worker_id = '', "
                    "started_at = 0, finished_at = 0, requeues = 0 "
                    "WHERE id = ? AND state IN (?, ?)",
                    (QUEUED, base + i, job_id, FAILED, CANCELLED))
                moved += cur.rowcount or 0
            _ = marks
    return moved


def clear(*, owner: str | None = None, states=TERMINAL, db_path=None) -> int:
    """Forget finished jobs. Deliberate, never automatic.

    Results disappearing on their own is the complaint this whole module grew
    out of: a download triggered a rerun, the run that produced the buttons was
    gone, and so were the buttons. Nothing here removes a row unless a person
    asked for it, and the extraction JSON on disk survives regardless — clearing
    the queue costs no model time to undo.
    """
    states = list(states)
    marks = ",".join("?" * len(states))
    sql = f"DELETE FROM jobs WHERE state IN ({marks})"
    args = list(states)
    if owner is not None:
        sql += " AND owner = ?"
        args.append(owner)
    with _db(db_path) as conn:
        with _write(conn):
            return conn.execute(sql, args).rowcount or 0


# -------------------------------------------------------------------- views
def get(job_id: str, *, db_path=None) -> Job | None:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(*, owner: str | None = None, batch_id: str | None = None,
              states=None, limit: int = 500, db_path=None) -> list[Job]:
    sql = "SELECT * FROM jobs WHERE 1 = 1"
    args: list = []
    if owner is not None:
        sql += " AND owner = ?"
        args.append(owner)
    if batch_id:
        sql += " AND batch_id = ?"
        args.append(batch_id)
    if states:
        states = list(states)
        sql += f" AND state IN ({','.join('?' * len(states))})"
        args.extend(states)
    sql += " ORDER BY position, created_at LIMIT ?"
    args.append(limit)
    with _db(db_path) as conn:
        return [_row_to_job(r) for r in conn.execute(sql, args)]


def drawing_key(path: Path | str) -> str:
    """One spelling per drawing file, for matching jobs to library entries."""
    return str(Path(path).resolve())


def latest_by_drawing(*, owner: str | None = None, db_path=None) -> dict:
    """The most recent job for each (drawing path, page), newest wins.

    What a drawing's status badge is built from. A drawing read on Monday,
    re-read on Tuesday and failing on Tuesday is failing — the older success is
    history, not its state.

    Keyed by the resolved path, so a job queued as `/abs/Drawings/x.pdf` and a
    page asking about `Drawings/x.pdf` agree that they mean the same drawing.
    """
    out: dict[tuple[str, int], Job] = {}
    for job in list_jobs(owner=owner, limit=100_000, db_path=db_path):
        key = (drawing_key(job.drawing_path), job.page)
        if key not in out or job.created_at >= out[key].created_at:
            out[key] = job
    return out


def summary(*, owner: str | None = None, db_path=None) -> dict:
    """Counts and overall progress, for the header of the queue panel.

    Overall percent counts a running job's own progress, so a twenty-drawing
    queue moves while drawing one is still being read rather than jumping in
    twentieths. That is the difference between a bar that looks stuck and a bar
    that tells you the machine is working.
    """
    jobs = list_jobs(owner=owner, db_path=db_path)
    counts = {s: 0 for s in (QUEUED, RUNNING, DONE, FAILED, CANCELLED)}
    for job in jobs:
        counts[job.state] = counts.get(job.state, 0) + 1
    active = [j for j in jobs if j.state not in (CANCELLED,)]
    done_units = sum(1.0 for j in active if j.state in (DONE, FAILED))
    done_units += sum(j.progress_pct / 100.0 for j in active if j.state == RUNNING)
    total = len(active)
    elapsed = [j.elapsed_s for j in jobs if j.state == DONE and j.elapsed_s > 0
               and not j.from_cache]
    mean = sum(elapsed) / len(elapsed) if elapsed else 0.0
    remaining = counts[QUEUED] + counts[RUNNING]
    return {
        "counts": counts,
        "total": total,
        "percent": round(100.0 * done_units / total, 1) if total else 0.0,
        "running": next((j for j in jobs if j.state == RUNNING), None),
        "mean_seconds": round(mean, 1),
        "eta_seconds": round(mean * remaining, 1) if mean else 0.0,
        "paused": is_paused(owner or "", db_path=db_path),
    }
