"""SQLite connection management and schema migrations."""

from __future__ import annotations

import itertools
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_V1 = """
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE cleaning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_time TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    status TEXT NOT NULL,
    postpone_count INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT,
    notes TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_events_sched ON cleaning_events(status, scheduled_time);

CREATE TABLE event_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES cleaning_events(id),
    changed_at TEXT NOT NULL,
    change_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    old_time TEXT,
    new_time TEXT,
    old_status TEXT,
    new_status TEXT,
    decision_id INTEGER,
    reason TEXT
);
CREATE INDEX ix_changes_event ON event_changes(event_id);

CREATE TABLE forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    interval_hours REAL NOT NULL,
    coverage_start TEXT,
    coverage_end TEXT,
    points_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_forecasts_fetched ON forecasts(source, fetched_at);

CREATE TABLE job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    error TEXT,
    summary_json TEXT
);
-- At most one run in progress at a time (duplicate execution guard).
CREATE UNIQUE INDEX ux_job_runs_running ON job_runs(status) WHERE status = 'RUNNING';
CREATE INDEX ix_job_runs_date ON job_runs(run_date, status);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES job_runs(id),
    event_id INTEGER NOT NULL REFERENCES cleaning_events(id),
    run_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    event_time_before TEXT NOT NULL,
    original_time TEXT NOT NULL,
    forecast_id INTEGER REFERENCES forecasts(id),
    forecast_source TEXT,
    forecast_freshness TEXT NOT NULL,
    forecast_age_hours REAL,
    inputs_json TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    model_json TEXT,
    model_source TEXT NOT NULL,
    model_error TEXT,
    final_action TEXT NOT NULL,
    final_reason TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    confidence REAL,
    proposed_time TEXT,
    outcome TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    applied_at TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    -- Idempotency: one automatic decision per event per local day.
    UNIQUE (event_id, run_date)
);
CREATE INDEX ix_decisions_created ON decisions(created_at);
"""

MIGRATIONS: list[str] = [_V1]

_savepoints = itertools.count(1)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection, apply pending migrations and return it.

    Connections use autocommit mode; use `transaction()` for atomic units.
    """
    target = str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    current = schema_version(conn)
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    return schema_version(conn)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic block. Nested use is safe (implemented with savepoints)."""
    name = f"sp_{next(_savepoints)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")
