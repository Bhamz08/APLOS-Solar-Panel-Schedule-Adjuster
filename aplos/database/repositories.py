"""Persistence for schedules, forecasts, decisions, job runs and settings."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable

from pydantic import ValidationError

from aplos.config.settings import AppSettings
from aplos.database.db import transaction
from aplos.models import (
    CleaningEvent,
    DataSource,
    DecisionRecord,
    EventStatus,
    FinalAction,
    ForecastBundle,
    ForecastPoint,
    Freshness,
    Outcome,
)
from aplos.timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)


class ConcurrentModificationError(RuntimeError):
    """The row changed since it was read (optimistic locking)."""


class RunInProgressError(RuntimeError):
    """Another pipeline run currently holds the run lock."""


def _opt_iso(dt: datetime | None) -> str | None:
    return iso(dt) if dt else None


def _opt_dt(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


# --------------------------------------------------------------------------- settings


class SettingsRepository:
    KEY = "app"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def load(self) -> AppSettings:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (self.KEY,)).fetchone()
        if row is None:
            return AppSettings()
        try:
            return AppSettings.model_validate_json(row["value"])
        except ValidationError as exc:
            log.error("Stored settings are invalid; using defaults", extra={"ctx": {"error": str(exc)}})
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        validated = AppSettings.model_validate(settings.model_dump())
        self.conn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (self.KEY, validated.model_dump_json(), iso(utcnow())),
        )

    def updated_at(self) -> datetime | None:
        row = self.conn.execute("SELECT updated_at FROM settings WHERE key = ?", (self.KEY,)).fetchone()
        return parse_iso(row["updated_at"]) if row else None


# --------------------------------------------------------------------------- events


class EventRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _row(row: sqlite3.Row) -> CleaningEvent:
        return CleaningEvent(
            id=row["id"],
            original_time=parse_iso(row["original_time"]),
            scheduled_time=parse_iso(row["scheduled_time"]),
            status=EventStatus(row["status"]),
            postpone_count=row["postpone_count"],
            last_reason=row["last_reason"],
            notes=row["notes"],
            version=row["version"],
            created_at=parse_iso(row["created_at"]),
            updated_at=parse_iso(row["updated_at"]),
        )

    def add(self, when: datetime, notes: str | None = None, actor: str = "operator") -> CleaningEvent:
        now = iso(utcnow())
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO cleaning_events(original_time, scheduled_time, status, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (iso(when), iso(when), EventStatus.SCHEDULED.value, notes, now, now),
            )
            event_id = int(cur.lastrowid)
            self._log_change(event_id, "CREATED", actor, None, when, None, EventStatus.SCHEDULED, None, notes)
        return self.get(event_id)

    def get(self, event_id: int) -> CleaningEvent:
        row = self.conn.execute("SELECT * FROM cleaning_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"Cleaning event {event_id} not found")
        return self._row(row)

    def list(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        statuses: Iterable[EventStatus] | None = None,
    ) -> list[CleaningEvent]:
        sql, params = "SELECT * FROM cleaning_events WHERE 1=1", []
        if start:
            sql += " AND scheduled_time >= ?"
            params.append(iso(start))
        if end:
            sql += " AND scheduled_time < ?"
            params.append(iso(end))
        if statuses:
            values = [s.value for s in statuses]
            sql += f" AND status IN ({','.join('?' * len(values))})"
            params.extend(values)
        sql += " ORDER BY scheduled_time, id"
        return [self._row(r) for r in self.conn.execute(sql, params)]

    def due_for_evaluation(self, now: datetime, horizon_hours: float) -> list[CleaningEvent]:
        return self.list(now, now + timedelta(hours=horizon_hours), [EventStatus.SCHEDULED])

    def next_scheduled_after(self, when: datetime, exclude_id: int | None = None) -> CleaningEvent | None:
        row = self.conn.execute(
            "SELECT * FROM cleaning_events WHERE status = ? AND scheduled_time > ? AND id != ? "
            "ORDER BY scheduled_time LIMIT 1",
            (EventStatus.SCHEDULED.value, iso(when), exclude_id or -1),
        ).fetchone()
        return self._row(row) if row else None

    def reschedule(
        self,
        event: CleaningEvent,
        new_time: datetime,
        reason: str,
        actor: str,
        decision_id: int | None = None,
        count_as_postponement: bool = True,
    ) -> CleaningEvent:
        with transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE cleaning_events SET scheduled_time = ?, postpone_count = postpone_count + ?, "
                "last_reason = ?, version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
                (iso(new_time), 1 if count_as_postponement else 0, reason, iso(utcnow()), event.id, event.version),
            )
            if cur.rowcount != 1:
                raise ConcurrentModificationError(f"Event {event.id} changed since it was read")
            self._log_change(
                event.id, "POSTPONED" if count_as_postponement else "RESCHEDULED", actor,
                event.scheduled_time, new_time, event.status, event.status, decision_id, reason,
            )
        return self.get(event.id)

    def set_status(
        self, event: CleaningEvent, status: EventStatus, reason: str, actor: str, decision_id: int | None = None
    ) -> CleaningEvent:
        with transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE cleaning_events SET status = ?, last_reason = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status.value, reason, iso(utcnow()), event.id, event.version),
            )
            if cur.rowcount != 1:
                raise ConcurrentModificationError(f"Event {event.id} changed since it was read")
            self._log_change(
                event.id, status.value, actor, event.scheduled_time, event.scheduled_time,
                event.status, status, decision_id, reason,
            )
        return self.get(event.id)

    def operator_edit(
        self, event: CleaningEvent, new_time: datetime, notes: str | None, reason: str = "Edited by operator"
    ) -> CleaningEvent:
        """An operator edit sets a new plan: original time and postponement count reset."""
        with transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE cleaning_events SET original_time = ?, scheduled_time = ?, postpone_count = 0, notes = ?, "
                "last_reason = ?, version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
                (iso(new_time), iso(new_time), notes, reason, iso(utcnow()), event.id, event.version),
            )
            if cur.rowcount != 1:
                raise ConcurrentModificationError(f"Event {event.id} changed since it was read")
            self._log_change(
                event.id, "EDITED", "operator", event.scheduled_time, new_time, event.status, event.status, None, reason
            )
        return self.get(event.id)

    def changes(self, event_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM event_changes"
        params: list[Any] = []
        if event_id is not None:
            sql += " WHERE event_id = ?"
            params.append(event_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def _log_change(
        self,
        event_id: int,
        change_type: str,
        actor: str,
        old_time: datetime | None,
        new_time: datetime | None,
        old_status: EventStatus | None,
        new_status: EventStatus | None,
        decision_id: int | None,
        reason: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO event_changes(event_id, changed_at, change_type, actor, old_time, new_time, old_status, "
            "new_status, decision_id, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, iso(utcnow()), change_type, actor, _opt_iso(old_time), _opt_iso(new_time),
                old_status.value if old_status else None, new_status.value if new_status else None,
                decision_id, reason,
            ),
        )


# --------------------------------------------------------------------------- forecasts


class ForecastRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, bundle: ForecastBundle) -> ForecastBundle:
        cur = self.conn.execute(
            "INSERT INTO forecasts(provider, source, fetched_at, latitude, longitude, interval_hours, coverage_start, "
            "coverage_end, points_json, warnings_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle.provider, bundle.source.value, iso(bundle.fetched_at), bundle.latitude, bundle.longitude,
                bundle.interval_hours, _opt_iso(bundle.coverage_start), _opt_iso(bundle.coverage_end),
                json.dumps([p.model_dump(mode="json") for p in bundle.points]),
                json.dumps(bundle.warnings), iso(utcnow()),
            ),
        )
        return bundle.model_copy(update={"id": int(cur.lastrowid)})

    @staticmethod
    def _row(row: sqlite3.Row) -> ForecastBundle:
        return ForecastBundle(
            id=row["id"],
            provider=row["provider"],
            source=DataSource(row["source"]),
            fetched_at=parse_iso(row["fetched_at"]),
            latitude=row["latitude"],
            longitude=row["longitude"],
            interval_hours=row["interval_hours"],
            points=[ForecastPoint.model_validate(p) for p in json.loads(row["points_json"])],
            warnings=json.loads(row["warnings_json"]),
        )

    def get(self, forecast_id: int) -> ForecastBundle:
        row = self.conn.execute("SELECT * FROM forecasts WHERE id = ?", (forecast_id,)).fetchone()
        if row is None:
            raise KeyError(f"Forecast {forecast_id} not found")
        return self._row(row)

    def latest(
        self,
        sources: Iterable[DataSource],
        latitude: float | None = None,
        longitude: float | None = None,
        tolerance_deg: float = 0.1,
    ) -> ForecastBundle | None:
        """Most recent forecast from `sources`; if a location is given, only one for that site."""
        values = [s.value for s in sources]
        if not values:
            return None
        sql = f"SELECT * FROM forecasts WHERE source IN ({','.join('?' * len(values))})"
        params: list[Any] = list(values)
        if latitude is not None and longitude is not None:
            sql += " AND ABS(latitude - ?) <= ? AND ABS(longitude - ?) <= ?"
            params += [latitude, tolerance_deg, longitude, tolerance_deg]
        row = self.conn.execute(sql + " ORDER BY fetched_at DESC, id DESC LIMIT 1", params).fetchone()
        return self._row(row) if row else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, provider, source, fetched_at, coverage_start, coverage_end, warnings_json "
            "FROM forecasts ORDER BY fetched_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def prune(self, keep: int = 200) -> int:
        cur = self.conn.execute(
            "DELETE FROM forecasts WHERE id NOT IN (SELECT id FROM forecasts ORDER BY fetched_at DESC LIMIT ?) "
            "AND id NOT IN (SELECT forecast_id FROM decisions WHERE forecast_id IS NOT NULL)",
            (keep,),
        )
        return cur.rowcount


# --------------------------------------------------------------------------- decisions


class DecisionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(self, d: DecisionRecord) -> DecisionRecord | None:
        """Insert a decision. Returns None if one already exists for (event, day)."""
        try:
            cur = self.conn.execute(
                "INSERT INTO decisions(run_id, event_id, run_date, created_at, event_version, event_time_before, "
                "original_time, forecast_id, forecast_source, forecast_freshness, forecast_age_hours, inputs_json, "
                "rules_json, model_json, model_source, model_error, final_action, final_reason, conditions_json, "
                "confidence, proposed_time, outcome, flags_json, applied_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    d.run_id, d.event_id, d.run_date, iso(d.created_at), d.event_version, iso(d.event_time_before),
                    iso(d.original_time), d.forecast_id, d.forecast_source, d.forecast_freshness.value,
                    d.forecast_age_hours, json.dumps(d.inputs, default=str), json.dumps(d.rules, default=str),
                    json.dumps(d.model, default=str) if d.model is not None else None, d.model_source,
                    d.model_error, d.final_action.value, d.final_reason, json.dumps(d.conditions), d.confidence,
                    _opt_iso(d.proposed_time), d.outcome.value, json.dumps(d.flags), _opt_iso(d.applied_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                return None
            raise
        return d.model_copy(update={"id": int(cur.lastrowid)})

    @staticmethod
    def _row(row: sqlite3.Row) -> DecisionRecord:
        return DecisionRecord(
            id=row["id"],
            run_id=row["run_id"],
            event_id=row["event_id"],
            run_date=row["run_date"],
            created_at=parse_iso(row["created_at"]),
            event_version=row["event_version"],
            event_time_before=parse_iso(row["event_time_before"]),
            original_time=parse_iso(row["original_time"]),
            forecast_id=row["forecast_id"],
            forecast_source=row["forecast_source"],
            forecast_freshness=Freshness(row["forecast_freshness"]),
            forecast_age_hours=row["forecast_age_hours"],
            inputs=json.loads(row["inputs_json"]),
            rules=json.loads(row["rules_json"]),
            model=json.loads(row["model_json"]) if row["model_json"] else None,
            model_source=row["model_source"],
            model_error=row["model_error"],
            final_action=FinalAction(row["final_action"]),
            final_reason=row["final_reason"],
            conditions=json.loads(row["conditions_json"]),
            confidence=row["confidence"],
            proposed_time=_opt_dt(row["proposed_time"]),
            outcome=Outcome(row["outcome"]),
            flags=json.loads(row["flags_json"]),
            applied_at=_opt_dt(row["applied_at"]),
            reviewed_by=row["reviewed_by"],
            reviewed_at=_opt_dt(row["reviewed_at"]),
        )

    def get(self, decision_id: int) -> DecisionRecord:
        row = self.conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(f"Decision {decision_id} not found")
        return self._row(row)

    def exists(self, event_id: int, run_date: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM decisions WHERE event_id = ? AND run_date = ?", (event_id, run_date)
        ).fetchone()
        return row is not None

    def list(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        actions: Iterable[FinalAction] | None = None,
        outcomes: Iterable[Outcome] | None = None,
        event_id: int | None = None,
        limit: int = 1000,
    ) -> list[DecisionRecord]:
        sql, params = "SELECT * FROM decisions WHERE 1=1", []
        if start:
            sql += " AND created_at >= ?"
            params.append(iso(start))
        if end:
            sql += " AND created_at < ?"
            params.append(iso(end))
        if actions:
            vals = [a.value for a in actions]
            sql += f" AND final_action IN ({','.join('?' * len(vals))})"
            params.extend(vals)
        if outcomes:
            vals = [o.value for o in outcomes]
            sql += f" AND outcome IN ({','.join('?' * len(vals))})"
            params.extend(vals)
        if event_id is not None:
            sql += " AND event_id = ?"
            params.append(event_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [self._row(r) for r in self.conn.execute(sql, params)]

    def latest(self) -> DecisionRecord | None:
        rows = self.list(limit=1)
        return rows[0] if rows else None

    def pending(self) -> list[DecisionRecord]:
        return self.list(outcomes=[Outcome.PENDING_APPROVAL])

    def set_outcome(
        self,
        decision_id: int,
        outcome: Outcome,
        reviewed_by: str | None = None,
        applied: bool = False,
        note: str | None = None,
    ) -> None:
        now = iso(utcnow())
        current = self.get(decision_id)
        flags = current.flags + ([note] if note else [])
        self.conn.execute(
            "UPDATE decisions SET outcome = ?, reviewed_by = COALESCE(?, reviewed_by), "
            "reviewed_at = CASE WHEN ? IS NOT NULL THEN ? ELSE reviewed_at END, "
            "applied_at = CASE WHEN ? THEN ? ELSE applied_at END, flags_json = ? WHERE id = ?",
            (outcome.value, reviewed_by, reviewed_by, now, 1 if applied else 0, now, json.dumps(flags), decision_id),
        )

    def supersede_pending(self, event_id: int, keep_id: int | None = None) -> int:
        cur = self.conn.execute(
            "UPDATE decisions SET outcome = ? WHERE event_id = ? AND outcome = ? AND id != ?",
            (Outcome.SUPERSEDED.value, event_id, Outcome.PENDING_APPROVAL.value, keep_id or -1),
        )
        return cur.rowcount


# --------------------------------------------------------------------------- job runs


class RunRepository:
    RUNNING, SUCCESS, FAILED, ABANDONED = "RUNNING", "SUCCESS", "FAILED", "ABANDONED"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def recover_stale(self, now: datetime, timeout_minutes: float) -> int:
        """Mark runs whose heartbeat stopped (crash, power loss) as abandoned."""
        cutoff = iso(now - timedelta(minutes=timeout_minutes))
        cur = self.conn.execute(
            "UPDATE job_runs SET status = ?, finished_at = ?, error = COALESCE(error, ?) "
            "WHERE status = ? AND heartbeat_at < ?",
            (self.ABANDONED, iso(now), "Run did not finish (process stopped); lock released", self.RUNNING, cutoff),
        )
        return cur.rowcount

    def start(self, run_date: str, trigger: str, now: datetime) -> int:
        try:
            cur = self.conn.execute(
                "INSERT INTO job_runs(run_date, trigger, status, started_at, heartbeat_at) VALUES (?, ?, ?, ?, ?)",
                (run_date, trigger, self.RUNNING, iso(now), iso(now)),
            )
        except sqlite3.IntegrityError as exc:
            raise RunInProgressError("Another pipeline run is in progress") from exc
        return int(cur.lastrowid)

    def heartbeat(self, run_id: int, now: datetime) -> None:
        self.conn.execute("UPDATE job_runs SET heartbeat_at = ? WHERE id = ?", (iso(now), run_id))

    def finish(
        self, run_id: int, status: str, now: datetime, duration_ms: int, summary: dict[str, Any] | None, error: str | None
    ) -> None:
        self.conn.execute(
            "UPDATE job_runs SET status = ?, finished_at = ?, duration_ms = ?, summary_json = ?, error = ? WHERE id = ?",
            (status, iso(now), duration_ms, json.dumps(summary or {}, default=str), error, run_id),
        )

    def has_success(self, run_date: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM job_runs WHERE run_date = ? AND status = ? LIMIT 1", (run_date, self.SUCCESS)
        ).fetchone()
        return row is not None

    def latest(self, status: str | None = None) -> dict[str, Any] | None:
        if status:
            row = self.conn.execute(
                "SELECT * FROM job_runs WHERE status = ? ORDER BY id DESC LIMIT 1", (status,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))]
