"""Local seen-store: stops the loop re-emitting the same company every run.

This is not the dedup authority — `crm_intake.py` is, and it checks the live
Notion board. This store exists so a 15-minute loop doesn't hand intake the
same 40 candidates 96 times a day.

Append-preserving, like sales-nav's store: rows are updated, never deleted, so
`first_seen` stays answerable.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from lib.config import research_config, state_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    identity_key   TEXT PRIMARY KEY,
    company        TEXT NOT NULL,
    website        TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    times_seen     INTEGER NOT NULL DEFAULT 1,
    emitted_at     TEXT,
    best_strength  INTEGER NOT NULL DEFAULT 0,
    payload        TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    lane       TEXT,
    found      INTEGER NOT NULL DEFAULT 0,
    emitted    INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS seen_emitted_idx ON seen(emitted_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_file() -> Path:
    return state_path(research_config()["dedup"]["localStore"])


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path or db_file())
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def is_emitted(conn: sqlite3.Connection, key: str) -> bool:
    """True when this company has already been handed to intake."""
    if not key:
        return False
    row = conn.execute(
        "SELECT emitted_at FROM seen WHERE identity_key = ?", (key,)
    ).fetchone()
    return bool(row and row["emitted_at"])


def record_seen(conn: sqlite3.Connection, cand) -> None:
    """Upsert a sighting. Keeps the strongest signal strength ever observed."""
    key = cand.key
    if not key:
        return
    payload = json.dumps(cand.as_dict(), default=str)
    conn.execute(
        """
        INSERT INTO seen (identity_key, company, website, first_seen, last_seen,
                          times_seen, best_strength, payload)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(identity_key) DO UPDATE SET
            last_seen     = excluded.last_seen,
            times_seen    = seen.times_seen + 1,
            best_strength = MAX(seen.best_strength, excluded.best_strength),
            company       = excluded.company,
            website       = COALESCE(NULLIF(excluded.website, ''), seen.website),
            payload       = excluded.payload
        """,
        (key, cand.company, cand.website, now_iso(), now_iso(),
         cand.signal_strength, payload),
    )


def mark_emitted(conn: sqlite3.Connection, keys: list[str]) -> None:
    stamp = now_iso()
    conn.executemany(
        "UPDATE seen SET emitted_at = ? WHERE identity_key = ? AND emitted_at IS NULL",
        [(stamp, k) for k in keys if k],
    )


def start_run(conn: sqlite3.Connection, lane: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, lane) VALUES (?, ?)", (now_iso(), lane)
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, found: int, emitted: int, note: str = "") -> None:
    conn.execute(
        "UPDATE runs SET found = ?, emitted = ?, note = ? WHERE id = ?",
        (found, emitted, note, run_id),
    )


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    total = conn.execute("SELECT COUNT(*) AS c FROM seen").fetchone()["c"]
    emitted = conn.execute(
        "SELECT COUNT(*) AS c FROM seen WHERE emitted_at IS NOT NULL"
    ).fetchone()["c"]
    return {"seen": total, "emitted": emitted, "held": total - emitted}
