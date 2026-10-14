"""Pipeline orchestration: forecast → rules → model → reconcile → schedule update.

Each run is idempotent: at most one automatic decision per event per local
day (enforced by a UNIQUE constraint), and only one run can hold the run
lock at a time. Every decision stores its full inputs, the rules applied,
the model output and the final action so it can be audited and reproduced.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from aplos.config.settings import AppSettings
from aplos.database.db import transaction
from aplos.database.repositories import (
    ConcurrentModificationError,
    DecisionRepository,
    EventRepository,
    RunRepository,
)
from aplos.engine.reconcile import reconcile
from aplos.engine.rules import RuleEvaluation, evaluate_rules
from aplos.llm.advisor import AdvisorResult, LLMAdvisor
from aplos.llm.prompts import build_context
from aplos.models import (
    Action,
    CleaningEvent,
    DataSource,
    DecisionRecord,
    FinalAction,
    Freshness,
    Outcome,
)
from aplos.scheduler.schedule import ScheduleManager
from aplos.timeutil import Clock, fmt_local, local_date, utcnow
from aplos.weather.service import ForecastResult, WeatherService

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    run_id: int | None
    run_date: str
    trigger: str
    status: str = "RUNNING"
    duration_ms: int = 0
    forecast_freshness: str | None = None
    forecast_source: str | None = None
    model_status: str | None = None
    evaluated: int = 0
    already_decided: int = 0
    applied: int = 0
    pending_approval: int = 0
    no_change: int = 0
    insufficient_data: int = 0
    conflicts: int = 0
    events_added: int = 0
    decision_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Pipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: AppSettings,
        weather: WeatherService,
        advisor: LLMAdvisor,
        clock: Clock = utcnow,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.weather = weather
        self.advisor = advisor
        self.clock = clock
        self.events = EventRepository(conn)
        self.decisions = DecisionRepository(conn)
        self.runs = RunRepository(conn)
        self.schedule = ScheduleManager(conn, settings, clock)

    # ------------------------------------------------------------------ run

    def run(self, trigger: str = "manual") -> RunSummary:
        """Execute one pipeline run. Raises RunInProgressError if another run holds the lock."""
        now = self.clock()
        tz = self.settings.site.tz
        run_date = local_date(now, tz)
        recovered = self.runs.recover_stale(now, self.settings.schedule.lock_timeout_minutes)
        if recovered:
            log.warning("Recovered abandoned run lock", extra={"ctx": {"runs": recovered}})
        run_id = self.runs.start(run_date, trigger, now)
        summary = RunSummary(run_id, run_date, trigger)
        started = time.perf_counter()
        log.info("Pipeline run started", extra={"ctx": {"run_id": run_id, "trigger": trigger, "run_date": run_date}})
        try:
            # Runs under the run lock, so concurrent runs cannot extend the plan twice.
            summary.events_added = len(self.schedule.extend_plan(now))
            status = self.advisor.status()
            summary.model_status = f"{status.source}: {status.detail}" if status.available else f"unavailable: {status.detail}"
            forecast = self.weather.get_forecast()
            summary.forecast_freshness = forecast.freshness.value
            summary.forecast_source = forecast.bundle.provider if forecast.bundle else None
            summary.errors.extend(forecast.errors if not forecast.usable else [])

            for event in self.events.due_for_evaluation(now, self.settings.schedule.evaluation_horizon_hours):
                if self.decisions.exists(event.id, run_date):
                    summary.already_decided += 1
                    continue
                try:
                    record = self._decide(event, forecast, run_id, run_date, now)
                except Exception as exc:  # isolate failures per event
                    log.exception("Event evaluation failed", extra={"ctx": {"event_id": event.id}})
                    summary.errors.append(f"Event {event.id}: {exc!r}")
                    continue
                finally:
                    self.runs.heartbeat(run_id, self.clock())
                if record is None:
                    summary.already_decided += 1
                    continue
                summary.evaluated += 1
                summary.decision_ids.append(record.id or 0)
                counter = {
                    Outcome.APPLIED: "applied", Outcome.PENDING_APPROVAL: "pending_approval",
                    Outcome.NO_CHANGE: "no_change", Outcome.INSUFFICIENT_DATA: "insufficient_data",
                    Outcome.CONFLICT: "conflicts",
                }.get(record.outcome)
                if counter:
                    setattr(summary, counter, getattr(summary, counter) + 1)
            event_errors = [e for e in summary.errors if e.startswith("Event ")]
            summary.status = "FAILED" if event_errors else "SUCCESS"
        except Exception as exc:
            summary.status = "FAILED"
            summary.errors.append(f"Run failed: {exc!r}")
            log.exception("Pipeline run failed", extra={"ctx": {"run_id": run_id}})
        finally:
            summary.duration_ms = int((time.perf_counter() - started) * 1000)
            self.runs.finish(
                run_id, summary.status, self.clock(), summary.duration_ms, summary.to_dict(),
                "; ".join(summary.errors)[:2000] or None,
            )
            log.info(
                "Pipeline run finished",
                extra={"ctx": {k: v for k, v in summary.to_dict().items() if k not in ("decision_ids",)}},
            )
        return summary

    # ------------------------------------------------------------------ per event

    def _decide(
        self, event: CleaningEvent, forecast: ForecastResult, run_id: int, run_date: str, now: datetime
    ) -> DecisionRecord | None:
        s = self.settings
        tz = s.site.tz
        bundle = forecast.bundle
        base: dict[str, Any] = dict(
            run_id=run_id, event_id=event.id, run_date=run_date, created_at=now, event_version=event.version,
            event_time_before=event.scheduled_time, original_time=event.original_time,
            forecast_id=bundle.id if bundle else None,
            forecast_source=f"{bundle.source.value}:{bundle.provider}" if bundle else None,
            forecast_freshness=forecast.freshness,
            forecast_age_hours=round(forecast.age_hours, 2) if forecast.age_hours is not None else None,
        )
        inputs: dict[str, Any] = {
            "event": event.model_dump(mode="json"),
            "forecast_errors": forecast.errors,
            "rules": s.rules.model_dump(),
            "schedule": s.schedule.model_dump(),
            "automation": s.automation.model_dump(),
        }

        if not forecast.usable or bundle is None:
            return self._store(DecisionRecord(
                **base, inputs=inputs, rules={}, model=None, model_source="not_called", model_error=None,
                final_action=FinalAction.UNDETERMINED,
                final_reason="No trustworthy weather data is available; the schedule was left unchanged.",
                conditions=[], confidence=None, proposed_time=None, outcome=Outcome.INSUFFICIENT_DATA,
                flags=["NO_TRUSTWORTHY_DATA: no automatic change made", *forecast.errors],
            ))

        next_event = self.events.next_scheduled_after(event.scheduled_time, exclude_id=event.id)
        ev = evaluate_rules(event, bundle, s, next_event.scheduled_time if next_event else None)
        inputs["forecast_window"] = [
            p.model_dump(mode="json") for p in bundle.between(ev.window_start, ev.window_end)
        ]
        flags = self._data_flags(forecast)

        if not ev.sufficient_coverage:
            return self._store(DecisionRecord(
                **base, inputs=inputs, rules=ev.to_dict(), model=None, model_source="not_called", model_error=None,
                final_action=FinalAction.UNDETERMINED,
                final_reason=(
                    f"The forecast covers only {ev.coverage_pct:.0f}% of the evaluation window "
                    f"(minimum {s.rules.min_window_coverage_pct:.0f}%); the schedule was left unchanged."
                ),
                conditions=ev.conditions, confidence=None, proposed_time=None, outcome=Outcome.INSUFFICIENT_DATA,
                flags=flags + ["INSUFFICIENT_FORECAST_COVERAGE"],
            ))

        advice = self._advise(event, bundle, ev, forecast)
        rc = reconcile(ev, advice, event, bundle, s)
        flags += rc.flags
        if ev.postpone_limit_reached:
            flags.append("POSTPONEMENT_LIMIT_REACHED: operator review advised")
        if ev.candidate_beyond_coverage and rc.action == Action.POSTPONE:
            flags.append("RESCHEDULED_BEYOND_FORECAST_COVERAGE: will be re-evaluated")
        if advice.source == "mock":
            flags.append("MOCK_MODEL_OUTPUT: development backend, not a language model")

        if rc.action == Action.CLEAN:
            outcome = Outcome.NO_CHANGE
        else:
            outcome, why = self._automation_gate(forecast)
            if why:
                flags.append(why)

        reason = rc.reason
        if rc.action == Action.POSTPONE and rc.proposed_time:
            reason += f" New time: {fmt_local(rc.proposed_time, tz)}."
        draft = DecisionRecord(
            **base, inputs=inputs, rules=ev.to_dict(),
            model={**advice.to_dict(), "used_for_decision": rc.used_model}, model_source=advice.source,
            model_error=advice.error, final_action=FinalAction(rc.action.value), final_reason=reason,
            conditions=ev.conditions, confidence=rc.confidence, proposed_time=rc.proposed_time,
            outcome=outcome, flags=flags,
        )
        if outcome != Outcome.APPLIED:
            return self._store(draft)
        # Recording the decision and changing the schedule happen atomically.
        try:
            with transaction(self.conn):
                stored = self._store(draft)
                if stored is None:
                    return None
                self.schedule.apply_decision(stored, "pipeline")
                self.decisions.set_outcome(stored.id or 0, Outcome.APPLIED, applied=True)
        except ConcurrentModificationError as exc:
            return self._store(draft.model_copy(update={"outcome": Outcome.CONFLICT, "flags": flags + [str(exc)]}))
        return self.decisions.get(stored.id or 0)

    def _store(self, record: DecisionRecord) -> DecisionRecord | None:
        stored = self.decisions.insert(record)
        if stored is None:
            log.info("Decision already exists for today; skipped", extra={"ctx": {"event_id": record.event_id}})
            return None
        self.decisions.supersede_pending(record.event_id, keep_id=stored.id)
        log.info(
            "Decision recorded",
            extra={"ctx": {
                "decision_id": stored.id, "event_id": stored.event_id, "action": stored.final_action.value,
                "outcome": stored.outcome.value, "model": stored.model_source,
                "freshness": stored.forecast_freshness.value,
            }},
        )
        return stored

    def _advise(
        self, event: CleaningEvent, bundle: Any, ev: RuleEvaluation, forecast: ForecastResult
    ) -> AdvisorResult:
        if self.advisor.backend is None:
            return AdvisorResult(None, "none", self.advisor.unavailable_reason, None, 0, 0)
        context = build_context(event, bundle, ev, self.settings, forecast.freshness.value, forecast.age_hours)
        return self.advisor.recommend(context)

    def _data_flags(self, forecast: ForecastResult) -> list[str]:
        flags = []
        b = forecast.bundle
        if forecast.freshness == Freshness.STALE:
            flags.append(f"STALE_FORECAST: forecast is {forecast.age_hours:.1f} h old")
        elif forecast.freshness == Freshness.CACHED:
            flags.append(f"CACHED_FORECAST: offline, using forecast from {forecast.age_hours:.1f} h ago")
        if b is not None and b.source == DataSource.MOCK:
            flags.append("MOCK_WEATHER_DATA: synthetic development data")
        if b is not None and b.source == DataSource.MANUAL:
            flags.append("MANUAL_WEATHER_DATA: operator-supplied")
        return flags

    def _automation_gate(self, forecast: ForecastResult) -> tuple[Outcome, str | None]:
        a = self.settings.automation
        if not a.auto_apply:
            return Outcome.PENDING_APPROVAL, "AWAITING_APPROVAL: automatic updates are disabled"
        if forecast.bundle is not None and forecast.bundle.source == DataSource.MOCK:
            return Outcome.PENDING_APPROVAL, "AWAITING_APPROVAL: mock weather data is never applied automatically"
        if forecast.freshness == Freshness.STALE and not a.auto_apply_on_stale:
            return Outcome.PENDING_APPROVAL, "AWAITING_APPROVAL: forecast is stale"
        return Outcome.APPLIED, None
