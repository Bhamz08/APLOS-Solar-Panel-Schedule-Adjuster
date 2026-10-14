"""Daily execution with restart and missed-run recovery.

All scheduling state lives in SQLite (`job_runs`), so the daemon can be
killed or the machine rebooted at any time:

- On every tick the daemon re-reads settings (run time, time zone).
- If today's run time has passed and no successful run exists for today,
  it runs immediately (catch-up after downtime or a missed execution).
- Failed runs are retried after `RETRY_DELAY` up to `MAX_ATTEMPTS` per day.
- A crashed run's lock is released after the configured lock timeout.

Earlier missed days are not replayed: decisions concern upcoming cleanings,
so replaying yesterday's run would use outdated context. Missed days are
logged.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from aplos.config.settings import AppSettings
from aplos.database.repositories import RunInProgressError, RunRepository, SettingsRepository
from aplos.engine.pipeline import Pipeline, RunSummary
from aplos.timeutil import Clock, combine_local, local_date, parse_iso, utcnow

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=30)
CATCHUP_GRACE = timedelta(minutes=5)

PipelineFactory = Callable[[sqlite3.Connection, AppSettings], Pipeline]


@dataclass
class DueCheck:
    due: bool
    trigger: str
    reason: str
    next_run_at: datetime


def scheduled_time_for(day: datetime, settings: AppSettings) -> datetime:
    return combine_local(day, settings.schedule.run_time, settings.site.tz)


def next_run_at(now: datetime, settings: AppSettings) -> datetime:
    today = scheduled_time_for(now, settings)
    return today if now < today else scheduled_time_for(now + timedelta(days=1), settings)


def check_due(conn: sqlite3.Connection, settings: AppSettings, now: datetime) -> DueCheck:
    runs = RunRepository(conn)
    tz = settings.site.tz
    run_date = local_date(now, tz)
    today_at = scheduled_time_for(now, settings)
    upcoming = next_run_at(now, settings)
    if now < today_at:
        return DueCheck(False, "scheduled", f"Next run today at {settings.schedule.daily_run_time}", today_at)
    if runs.has_success(run_date):
        return DueCheck(False, "scheduled", "Already completed today", upcoming)
    attempts = conn.execute(
        # An abandoned run's last heartbeat marks when the process actually stopped.
        "SELECT COUNT(*) AS n, MAX(CASE WHEN status = 'ABANDONED' THEN heartbeat_at ELSE finished_at END) AS last "
        "FROM job_runs "
        "WHERE run_date = ? AND trigger != 'manual' AND status IN ('FAILED', 'ABANDONED')",
        (run_date,),
    ).fetchone()
    if attempts["n"] >= MAX_ATTEMPTS:
        return DueCheck(False, "scheduled", f"Gave up after {MAX_ATTEMPTS} failed attempts today", upcoming)
    if attempts["n"] and attempts["last"]:
        retry_at = parse_iso(attempts["last"]) + RETRY_DELAY
        if now < retry_at:
            return DueCheck(False, "retry", "Waiting to retry after a failed run", retry_at)
    if attempts["n"]:
        return DueCheck(True, "retry", f"Retrying (attempt {attempts['n'] + 1})", now)
    trigger = "catchup" if now - today_at > CATCHUP_GRACE else "scheduled"
    reason = "Scheduled run time reached" if trigger == "scheduled" else "Catch-up: today's scheduled run was missed"
    return DueCheck(True, trigger, reason, now)


class DailyScheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        pipeline_factory: PipelineFactory,
        clock: Clock = utcnow,
    ) -> None:
        self.conn = conn
        self.pipeline_factory = pipeline_factory
        self.clock = clock
        self._stop = threading.Event()
        self._last_seen_date: str | None = None

    def tick(self) -> RunSummary | None:
        settings = SettingsRepository(self.conn).load()  # pick up changes made in the dashboard
        now = self.clock()
        RunRepository(self.conn).recover_stale(now, settings.schedule.lock_timeout_minutes)
        self._log_missed_days(now, settings)
        check = check_due(self.conn, settings, now)
        if not check.due:
            return None
        log.info("Daily job due", extra={"ctx": {"trigger": check.trigger, "reason": check.reason}})
        try:
            return self.pipeline_factory(self.conn, settings).run(trigger=check.trigger)
        except RunInProgressError:
            log.info("Another run is in progress; skipping this tick")
            return None

    def _log_missed_days(self, now: datetime, settings: AppSettings) -> None:
        today = local_date(now, settings.site.tz)
        if self._last_seen_date == today:
            return
        self._last_seen_date = today
        row = self.conn.execute(
            "SELECT MAX(run_date) AS d FROM job_runs WHERE status = 'SUCCESS' AND run_date < ?", (today,)
        ).fetchone()
        if row and row["d"]:
            gap = (datetime.fromisoformat(today) - datetime.fromisoformat(row["d"])).days - 1
            if gap > 0:
                log.warning("Missed daily runs detected", extra={"ctx": {"missed_days": gap, "last_success": row["d"]}})

    def serve_forever(self, poll_seconds: float = 60.0) -> None:
        log.info("Daily scheduler started", extra={"ctx": {"poll_s": poll_seconds}})
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # keep the daemon alive; failures are recorded per run
                log.exception("Scheduler tick failed")
            self._stop.wait(poll_seconds)
        log.info("Daily scheduler stopped")

    def stop(self) -> None:
        self._stop.set()
