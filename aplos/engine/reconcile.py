"""Combine the deterministic evaluation with the model's recommendation.

Hard constraints always win:
- the model may only pick an action in `allowed_actions`;
- low-confidence model output is ignored;
- a model-proposed time is used only if it satisfies the postponement
  limits, the cleaning hours and the forecast check; otherwise the
  rule engine's slot is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aplos.config.settings import AppSettings
from aplos.engine.rules import RuleEvaluation, assess_window, snap_to_cleaning_hours
from aplos.llm.advisor import AdvisorResult
from aplos.llm.schema import ModelOutputError
from aplos.models import Action, CleaningEvent, ForecastBundle


@dataclass
class Reconciled:
    action: Action
    reason: str
    proposed_time: datetime | None
    confidence: float
    used_model: bool
    flags: list[str] = field(default_factory=list)


def _model_time_ok(
    t: datetime, ev: RuleEvaluation, event: CleaningEvent, bundle: ForecastBundle, settings: AppSettings
) -> str | None:
    """Return None if acceptable, else the reason it was rejected."""
    if t <= event.scheduled_time:
        return "not later than the current schedule"
    if ev.earliest_reschedule and t < ev.earliest_reschedule:
        return "earlier than the earliest safe time"
    if t > ev.latest_allowed:
        return "beyond the maximum postponement"
    if snap_to_cleaning_hours(t, settings) != t:
        return "outside the site's cleaning hours"
    wa = assess_window(bundle, settings.rules, t, t + timedelta(hours=settings.schedule.cleaning_duration_hours))
    if (wa.rain.relevant and wa.rain.level in ("significant", "heavy")) or wa.sand.relevant:
        return "the forecast is not clear at that time"
    return None


def reconcile(
    ev: RuleEvaluation,
    advice: AdvisorResult | None,
    event: CleaningEvent,
    bundle: ForecastBundle,
    settings: AppSettings,
) -> Reconciled:
    flags: list[str] = []
    rec = advice.recommendation if advice else None
    action, reason, confidence, used_model = ev.deterministic_action, ev.deterministic_reason, ev.confidence, False
    allowed = ", ".join(a.value for a in ev.allowed_actions)

    if rec is not None:
        if rec.action not in ev.allowed_actions:
            flags.append(
                f"MODEL_OVERRIDDEN: model recommended {rec.action.value}; site rules allow only {allowed}"
            )
        elif rec.confidence < settings.rules.min_model_confidence:
            flags.append(
                f"MODEL_LOW_CONFIDENCE: {rec.confidence:.2f} < {settings.rules.min_model_confidence:.2f}; rules used"
            )
        else:
            used_model = True
            action, reason, confidence = rec.action, rec.reason, rec.confidence
            if rec.action != ev.deterministic_action:
                flags.append(
                    f"MODEL_CHOSE_ALTERNATIVE: {rec.action.value} instead of rule default "
                    f"{ev.deterministic_action.value} (permitted)"
                )
    elif advice is not None and advice.error:
        flags.append(f"MODEL_UNAVAILABLE: {advice.error}; deterministic fallback used")

    proposed: datetime | None = None
    if action == Action.POSTPONE:
        proposed = ev.postpone_candidate
        if used_model and rec is not None and rec.next_cleaning_time:
            try:
                t = rec.next_time_utc(settings.site.tz)
            except ModelOutputError as exc:
                flags.append(f"MODEL_TIME_REJECTED: {exc}")
            else:
                problem = _model_time_ok(t, ev, event, bundle, settings) if t else "missing"
                if problem is None:
                    proposed = t
                else:
                    flags.append(f"MODEL_TIME_REJECTED: proposed time {problem}; rule-engine slot used")
        if proposed is None:
            # No valid slot: fall back to the deterministic action.
            flags.append("NO_VALID_POSTPONE_SLOT: deterministic action used")
            action, reason, confidence, used_model = (
                ev.deterministic_action, ev.deterministic_reason, ev.confidence, False
            )
            proposed = ev.postpone_candidate if action == Action.POSTPONE else None
    if action == Action.SKIP:
        proposed = ev.skip_next_time

    return Reconciled(action, reason, proposed, round(confidence, 2), used_model, flags)
