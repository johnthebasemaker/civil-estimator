"""SQLite-backed rate library.

Rates keyed by (category, unit) — reused across projects.
User enters rates on the Costing page; they persist here and pre-fill
on future runs.

Schema:
  rates(category TEXT, unit TEXT, rate_sar REAL, updated_at TEXT,
        PRIMARY KEY (category, unit))
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path("data/rates.db")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rates (
            category TEXT NOT NULL,
            unit TEXT NOT NULL,
            rate_sar REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (category, unit)
        )
    """)
    return conn


def get_rate(category: str, unit: str) -> float:
    with _conn() as c:
        row = c.execute(
            "SELECT rate_sar FROM rates WHERE category = ? AND unit = ?",
            (category, unit),
        ).fetchone()
        return float(row[0]) if row else 0.0


def set_rate(category: str, unit: str, rate_sar: float) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO rates (category, unit, rate_sar, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(category, unit) DO UPDATE SET
                rate_sar = excluded.rate_sar,
                updated_at = excluded.updated_at
        """, (category, unit, rate_sar, datetime.now().isoformat(timespec="seconds")))


def get_all_rates() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT category, unit, rate_sar, updated_at FROM rates ORDER BY category, unit"
        ).fetchall()
    return [{"category": r[0], "unit": r[1], "rate_sar": r[2], "updated_at": r[3]} for r in rows]


def bulk_set(rates: list[dict]) -> int:
    """rates = [{'category':..., 'unit':..., 'rate_sar':...}, ...]"""
    n = 0
    for r in rates:
        set_rate(r["category"], r["unit"], float(r["rate_sar"]))
        n += 1
    return n