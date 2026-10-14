"""Cleaning schedule management: operator edits, approvals and applying decisions."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from aplos.config.settings import AppSettings, parse_hhmm
from aplos.database.db import transaction
from aplos.database.repositories import ConcurrentModificationError, DecisionRepository, EventRepository
from aplos.models import CleaningEvent, DecisionRecord, EventStatus, FinalAction, Outcome
from aplos.timeutil import Clock, utcnow

log = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    pass


class ScheduleManager:
    def __init__(self, conn: sqlite3.Connection, settings: AppSettings, clock: Clock = utcnow) -> None:
        self.conn = conn
        self.settings = settings
        self.clock = clock
        self.events = EventRepository(conn)
        self.decisions = DecisionRepository(conn)

    # ---------------------------------------------------------------- operator actions

    def add_event(self, when: datetime, notes: str | None = None) -> CleaningEvent:
        return self.events.add(when, notes)

    def edit_event(self, event_id: int, new_time: datetime, notes: str | None) -> CleaningEvent:
        event = self.events.get(event_id)
        if event.status != EventStatus.SCHEDULED:
            raise ValueError(f"Only scheduled events can be edited (event is {event.status.value})")
        updated = self.events.operator_edit(event, new_time, notes)
        self.decisions.supersede_pending(event_id)
        return updated

    def cancel_event(self, event_id: int, reason: str = "Cancelled by operator") -> CleaningEvent:
        event = self.events.get(event_id)
        updated = self.events.set_status(event, EventStatus.CANCELLED, reason, "operator")
        self.decisions.supersede_pending(event_id)
        return updated

    def complete_event(self, event_id: int, note: str = "Marked completed by operator") -> CleaningEvent:
        return self.events.set_status(self.events.get(event_id), EventStatus.COMPLETED, note, "operator")

    def generate_recurring(
        self, first_day: date, count: int, interval_days: int | None = None, at: str | None = None
    ) -> list[CleaningEvent]:
        """Create a recurring plan; existing events at the same time are not duplicated."""
        tz = self.settings.site.tz
        interval = interval_days or self.settings.schedule.default_interval_days
        at_t = parse_hhmm(at or self.settings.schedule.default_cleaning_time)
        existing = {e.original_time for e in self.events.list()}
        created = []
        for i in range(count):
            day = first_day + timedelta(days=i * interval)
            when = datetime.combine(day, at_t, tzinfo=tz).astimezone(timezone.utc)
            if when in existing:
                continue
            created.append(self.events.add(when, "Recurring plan"))
        return created

    def extend_plan(self, now: datetime) -> list[CleaningEvent]:
        """Keep the recurring plan filled `plan_horizon_days` ahead.

        New cleanings continue the cycle from the latest *originally planned*
        time of any event (cancelled ones included), so postponements do not
        shift the cycle and an operator-cancelled cleaning is not recreated.
        Wall-clock time is kept in the site time zone (DST-safe).
        """
        sched = self.settings.schedule
        if not sched.auto_extend_plan:
            return []
        events = self.events.list()
        if not events:
            return []  # no plan to continue
        tz = self.settings.site.tz
        anchor = max(e.original_time for e in events).astimezone(tz)
        horizon = now + timedelta(days=sched.plan_horizon_days)
        created = []
        n = 1
        while True:
            day = anchor.date() + timedelta(days=n * sched.default_interval_days)
            when = datetime.combine(day, anchor.time(), tzinfo=tz).astimezone(timezone.utc)
            if when > horizon:
                break
            created.append(self.events.add(when, "Recurring plan (auto-extended)", actor="scheduler"))
            n += 1
        if created:
            log.info("Recurring plan extended", extra={"ctx": {"added": len(created), "until": created[-1].scheduled_time}})
        return created

    # ---------------------------------------------------------------- decisions

    def apply_decision(self, decision: DecisionRecord, actor: str) -> CleaningEvent:
        """Apply a POSTPONE/SKIP decision. Raises ConcurrentModificationError if the event moved on."""
        event = self.events.get(decision.event_id)
        if event.version != decision.event_version or event.status != EventStatus.SCHEDULED:
            raise ConcurrentModificationError(
                f"Event {event.id} changed after the recommendation was made"
            )
        if decision.final_action == FinalAction.POSTPONE:
            if decision.proposed_time is None:
                raise ValueError("POSTPONE decision without a proposed time")
            return self.events.reschedule(event, decision.proposed_time, decision.final_reason, actor, decision.id)
        if decision.final_action == FinalAction.SKIP:
            return self.events.set_status(event, EventStatus.SKIPPED, decision.final_reason, actor, decision.id)
        return event

    def approve(self, decision_id: int, reviewer: str) -> DecisionRecord:
        decision = self.decisions.get(decision_id)
        if decision.outcome != Outcome.PENDING_APPROVAL:
            raise ApprovalError(f"Decision {decision_id} is {decision.outcome.value}, not pending approval")
        if decision.final_action == FinalAction.POSTPONE and decision.proposed_time and decision.proposed_time <= self.clock():
            self.decisions.set_outcome(decision_id, Outcome.CONFLICT, reviewer, note="Expired: proposed time has passed")
            raise ApprovalError("The proposed time has already passed; the recommendation expired")
        try:
            with transaction(self.conn):
                self.apply_decision(decision, f"operator:{reviewer}")
                self.decisions.set_outcome(decision_id, Outcome.APPROVED, reviewer, applied=True)
        except ConcurrentModificationError as exc:
            # Recorded outside the rolled-back transaction so the conflict is kept.
            self.decisions.set_outcome(decision_id, Outcome.CONFLICT, reviewer, note=str(exc))
            raise ApprovalError(str(exc)) from exc
        log.info("Recommendation approved", extra={"ctx": {"decision_id": decision_id, "reviewer": reviewer}})
        return self.decisions.get(decision_id)

    def reject(self, decision_id: int, reviewer: str, note: str | None = None) -> DecisionRecord:
        decision = self.decisions.get(decision_id)
        if decision.outcome != Outcome.PENDING_APPROVAL:
            raise ApprovalError(f"Decision {decision_id} is {decision.outcome.value}, not pending approval")
        self.decisions.set_outcome(decision_id, Outcome.REJECTED, reviewer, note=f"Rejected: {note}" if note else None)
        log.info("Recommendation rejected", extra={"ctx": {"decision_id": decision_id, "reviewer": reviewer}})
        return self.decisions.get(decision_id)
