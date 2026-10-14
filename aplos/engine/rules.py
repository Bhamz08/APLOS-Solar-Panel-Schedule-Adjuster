"""Deterministic decision rules.

The rules engine is authoritative. It decides which actions are permitted
(`allowed_actions`), the earliest safe reschedule time and the postponement
limits. The language model may only choose among the allowed actions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from aplos.config.settings import AppSettings, RuleSettings
from aplos.models import Action, CleaningEvent, ForecastBundle, ForecastPoint
from aplos.timeutil import ceil_hour, fmt_local, to_utc

RainLevel = Literal["none", "marginal", "significant", "heavy"]
MAX_SLOT_SEARCH = 24


@dataclass
class RainAssessment:
    level: RainLevel = "none"
    total_mm: float = 0.0
    expected_mm: float = 0.0
    max_probability_pct: float | None = None
    amount_known: bool = True
    start: datetime | None = None
    end: datetime | None = None
    relevant: bool = False  # affects this cleaning (not dried out long before it)
    timing: str | None = None  # before | during | after | spanning


@dataclass
class SandAssessment:
    detected: bool = False
    relevant: bool = False
    indicator: str | None = None  # provider_code | dust_concentration | wind_proxy
    start: datetime | None = None
    end: datetime | None = None
    peak_dust_ugm3: float | None = None
    peak_gust_kmh: float | None = None
    timing: str | None = None


@dataclass
class WindowAssessment:
    rain: RainAssessment
    sand: SandAssessment
    coverage_pct: float
    clear_after: datetime | None  # earliest time conditions allow cleaning again


@dataclass
class RuleEvaluation:
    cleaning_start: datetime
    cleaning_end: datetime
    window_start: datetime
    window_end: datetime
    coverage_pct: float
    sufficient_coverage: bool
    rain: RainAssessment
    sand: SandAssessment
    triggered_rules: list[str]
    deterministic_action: Action
    deterministic_reason: str
    allowed_actions: list[Action]
    earliest_reschedule: datetime | None
    latest_allowed: datetime
    postpone_candidate: datetime | None
    candidate_beyond_coverage: bool
    postpone_limit_reached: bool
    skip_next_time: datetime | None
    confidence: float
    conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, datetime):
                return v.isoformat()
            if isinstance(v, Action):
                return v.value
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            if isinstance(v, list):
                return [conv(x) for x in v]
            return v

        return conv(asdict(self))


def _timing(start: datetime, end: datetime, c_start: datetime, c_end: datetime) -> str:
    if end <= c_start:
        return "before"
    if start >= c_end:
        return "after"
    if start <= c_start and end >= c_end:
        return "spanning"
    return "during"


def _coverage(points: list[ForecastPoint], interval_h: float, start: datetime, end: datetime) -> float:
    total = (end - start).total_seconds()
    if total <= 0:
        return 100.0
    step = timedelta(hours=interval_h)
    covered = sum(
        max(0.0, (min(p.time + step, end) - max(p.time, start)).total_seconds()) for p in points
    )
    return round(min(100.0, 100.0 * covered / total), 1)


def assess_window(
    bundle: ForecastBundle, rules: RuleSettings, c_start: datetime, c_end: datetime
) -> WindowAssessment:
    w_start = c_start - timedelta(hours=rules.pre_window_hours)
    w_end = c_end + timedelta(hours=rules.post_window_hours)
    pts = bundle.between(w_start, w_end)
    step = timedelta(hours=bundle.interval_hours)
    wet_mm = rules.wet_hour_mm * bundle.interval_hours

    # ---- rain
    rain = RainAssessment()
    amounts = [p.precip_mm for p in pts if p.precip_mm is not None]
    rain.amount_known = bool(amounts)
    rain.total_mm = round(sum(amounts), 2)
    rain.expected_mm = round(
        sum((p.precip_mm or 0) * (p.precip_probability_pct if p.precip_probability_pct is not None else 100) / 100
            for p in pts), 2
    )
    wet = [
        p for p in pts
        if (p.precip_mm is not None and p.precip_mm >= wet_mm)
        or (p.precip_mm is None and (p.precip_probability_pct or 0) >= rules.rain_probability_threshold_pct)
    ]
    probs = [p.precip_probability_pct for p in (wet or pts) if p.precip_probability_pct is not None]
    rain.max_probability_pct = max(probs) if probs else None
    # Without a probability (e.g. operator-entered gauge data) the amount is taken at face value.
    prob = rain.max_probability_pct if rain.max_probability_pct is not None else 100.0
    if wet:
        rain.start, rain.end = wet[0].time, wet[-1].time + step
        rain.timing = _timing(rain.start, rain.end, c_start, c_end)
        confident = prob >= rules.rain_probability_threshold_pct
        if rain.amount_known:
            if confident and rain.total_mm >= rules.rain_amount_threshold_mm:
                rain.level = "heavy" if rain.total_mm >= rules.skip_rain_amount_mm else "significant"
            elif prob >= rules.marginal_probability_pct or rain.total_mm >= rules.rain_amount_threshold_mm:
                rain.level = "marginal"
        elif confident:
            rain.level = "significant"
        # Rain that ended (plus drying time) before cleaning starts does not affect it.
        rain.relevant = rain.end + timedelta(hours=rules.dry_buffer_hours) > c_start

    # ---- sand / dust
    sand = SandAssessment()
    sandy: list[tuple[ForecastPoint, str]] = []
    for p in pts:
        if p.dust_event_flag:
            sandy.append((p, "provider_code"))
        elif p.dust_ugm3 is not None and p.dust_ugm3 >= rules.dust_threshold_ugm3:
            sandy.append((p, "dust_concentration"))
        elif (
            rules.use_wind_as_sand_proxy and p.dust_ugm3 is None and p.wind_gust_kmh is not None
            and p.wind_gust_kmh >= rules.sand_wind_gust_threshold_kmh
        ):
            sandy.append((p, "wind_proxy"))
    dusts = [p.dust_ugm3 for p in pts if p.dust_ugm3 is not None]
    gusts = [p.wind_gust_kmh for p in pts if p.wind_gust_kmh is not None]
    sand.peak_dust_ugm3 = max(dusts) if dusts else None
    sand.peak_gust_kmh = max(gusts) if gusts else None
    if sandy:
        sand.detected = True
        order = ["provider_code", "dust_concentration", "wind_proxy"]
        sand.indicator = min((k for _, k in sandy), key=order.index)
        sand.start, sand.end = sandy[0][0].time, sandy[-1][0].time + step
        sand.timing = _timing(sand.start, sand.end, c_start, c_end)
        sand.relevant = sand.end + timedelta(hours=rules.dust_settle_hours) > c_start

    clear_after = None
    blockers = []
    if rain.relevant and rain.level in ("significant", "heavy", "marginal") and rain.end:
        blockers.append(rain.end + timedelta(hours=rules.dry_buffer_hours))
    if sand.relevant and sand.end:
        blockers.append(sand.end + timedelta(hours=rules.dust_settle_hours))
    if blockers:
        clear_after = max(blockers)
    return WindowAssessment(rain, sand, _coverage(pts, bundle.interval_hours, w_start, w_end), clear_after)


def snap_to_cleaning_hours(dt: datetime, settings: AppSettings) -> datetime:
    """Move `dt` forward into the site's allowed cleaning start hours (local time)."""
    tz: ZoneInfo = settings.site.tz
    sched = settings.schedule
    local = ceil_hour(dt).astimezone(tz)
    start_t = time(sched.cleaning_window_start_hour)
    if local.hour < sched.cleaning_window_start_hour:
        local = datetime.combine(local.date(), start_t, tzinfo=tz)
    elif local.hour > sched.cleaning_window_end_hour:
        local = datetime.combine(local.date() + timedelta(days=1), start_t, tzinfo=tz)
    return to_utc(local)


def find_postpone_slot(
    bundle: ForecastBundle, settings: AppSettings, not_before: datetime, latest: datetime
) -> tuple[datetime | None, bool]:
    """Earliest slot >= not_before whose window is clear. Returns (slot, beyond_coverage)."""
    rules = settings.rules
    duration = timedelta(hours=settings.schedule.cleaning_duration_hours)
    candidate = snap_to_cleaning_hours(not_before, settings)
    for _ in range(MAX_SLOT_SEARCH):
        if candidate > latest:
            return None, False
        wa = assess_window(bundle, rules, candidate, candidate + duration)
        if wa.coverage_pct < rules.min_window_coverage_pct:
            # Forecast does not reach this far; accept, it will be re-evaluated on later runs.
            return candidate, True
        blocking = (wa.rain.relevant and wa.rain.level in ("significant", "heavy")) or wa.sand.relevant
        if not blocking or wa.clear_after is None:
            return candidate, False
        candidate = snap_to_cleaning_hours(max(wa.clear_after, candidate + timedelta(hours=1)), settings)
    return None, False


def _describe(wa: WindowAssessment, tz: ZoneInfo) -> list[str]:
    out = []
    r, s = wa.rain, wa.sand
    if r.start and r.level != "none":
        prob = f", max probability {r.max_probability_pct:.0f}%" if r.max_probability_pct is not None else ""
        out.append(
            f"Rain {r.total_mm:.1f} mm ({r.level}{prob}) {fmt_local(r.start, tz)}–{fmt_local(r.end, tz, '%H:%M')}, "
            f"{r.timing} cleaning"
        )
    elif r.start:
        out.append(f"Trace rain {r.total_mm:.1f} mm, not significant")
    else:
        out.append("No rain expected in the evaluation window")
    if s.detected:
        detail = {
            "dust_concentration": f"dust up to {s.peak_dust_ugm3:.0f} µg/m³",
            "provider_code": "provider reports sand/dust",
            "wind_proxy": f"gusts up to {s.peak_gust_kmh:.0f} km/h (no dust data)",
        }[s.indicator or "wind_proxy"]
        out.append(f"Sand/dust event {fmt_local(s.start, tz)}–{fmt_local(s.end, tz, '%H:%M')} ({detail}), {s.timing} cleaning")
    elif s.peak_gust_kmh is not None:
        out.append(f"No sand/dust event; peak gust {s.peak_gust_kmh:.0f} km/h")
    return out


def evaluate_rules(
    event: CleaningEvent,
    bundle: ForecastBundle,
    settings: AppSettings,
    next_event_time: datetime | None = None,
) -> RuleEvaluation:
    rules = settings.rules
    tz = settings.site.tz
    c_start = event.scheduled_time
    c_end = c_start + timedelta(hours=settings.schedule.cleaning_duration_hours)
    wa = assess_window(bundle, rules, c_start, c_end)
    rain, sand = wa.rain, wa.sand
    latest_allowed = event.original_time + timedelta(hours=rules.max_postpone_hours)
    triggered: list[str] = []

    rain_blocks = rain.relevant and rain.level in ("significant", "heavy")
    if rain.level == "heavy":
        triggered.append(f"R1 heavy rain ≥ {rules.skip_rain_amount_mm} mm in window → skip (natural rinse)")
    elif rain_blocks:
        triggered.append(
            f"R2 rain ≥ {rules.rain_amount_threshold_mm} mm at ≥ {rules.rain_probability_threshold_pct:.0f}% "
            f"within {rules.dry_buffer_hours:.0f} h drying buffer → postpone"
        )
    elif rain.level == "marginal" and rain.relevant:
        triggered.append("R3 marginal rain signal → clean unless model advises postponing")
    if sand.relevant:
        triggered.append(
            f"S1 sand/dust event during or after cleaning (+{rules.dust_settle_hours:.0f} h settle) → postpone"
        )

    # Base deterministic action and the actions the model may choose from.
    if rain.level == "heavy" and not (sand.relevant and sand.end and rain.end and sand.end > rain.end):
        action, allowed = Action.SKIP, [Action.SKIP, Action.POSTPONE]
        reason = f"Heavy rain ({rain.total_mm:.1f} mm) is forecast around the cleaning window and should rinse the panels."
    elif rain_blocks or sand.relevant:
        action = Action.POSTPONE
        allowed = [Action.POSTPONE, Action.SKIP] if rain_blocks else [Action.POSTPONE]
        causes = []
        if rain_blocks:
            causes.append(f"{rain.total_mm:.1f} mm of rain ({rain.timing} cleaning)")
        if sand.relevant:
            causes.append(f"a sand/dust event ({sand.timing} cleaning)")
        reason = "Postpone: " + " and ".join(causes) + " would undo or prevent the cleaning."
    elif rain.level == "marginal" and rain.relevant:
        action, allowed = Action.CLEAN, [Action.CLEAN, Action.POSTPONE]
        reason = "Only a marginal rain signal is forecast; cleaning can proceed."
    else:
        action, allowed = Action.CLEAN, [Action.CLEAN]
        reason = "Forecast conditions are favorable for cleaning."

    # Postponement candidate (also computed for marginal cases so the model can choose it).
    candidate, beyond = None, False
    earliest = None
    if wa.clear_after is not None:
        earliest = max(wa.clear_after, c_start + timedelta(hours=1))
        candidate, beyond = find_postpone_slot(bundle, settings, earliest, latest_allowed)

    limit_reached = False
    if Action.POSTPONE in allowed:
        if event.postpone_count >= rules.max_postponements:
            limit_reached = True
            triggered.append(f"L1 already postponed {event.postpone_count}× (max {rules.max_postponements})")
        elif candidate is None:
            limit_reached = True
            triggered.append(f"L2 no clear slot before the {rules.max_postpone_hours:.0f} h postponement limit")
    if limit_reached:
        allowed = [a for a in allowed if a != Action.POSTPONE]
        if action == Action.POSTPONE:
            if rain_blocks:
                action, reason = Action.SKIP, "Postponement limit reached; rain is forecast, so this cleaning is skipped."
            else:
                action = Action.CLEAN
                reason = "Postponement limit reached; cleaning proceeds despite the forecast (operator review advised)."
        if not allowed:
            allowed = [action]
        if action not in allowed:
            allowed.append(action)
        candidate = None

    if Action.SKIP in allowed:
        skip_next = next_event_time or (
            event.original_time + timedelta(days=settings.schedule.default_interval_days)
        )
    else:
        skip_next = None

    # Heuristic confidence of the deterministic result (documented in README).
    if action == Action.CLEAN:
        p = rain.max_probability_pct or 0.0
        confidence = 0.9 - 0.4 * (p / 100.0) if rain.level != "none" else 0.9
    elif rain_blocks or rain.level == "heavy":
        confidence = 0.5 + 0.4 * ((rain.max_probability_pct if rain.max_probability_pct is not None else 75) / 100)
    else:
        confidence = {"provider_code": 0.85, "dust_concentration": 0.8, "wind_proxy": 0.65}.get(sand.indicator or "", 0.6)
    if beyond:
        confidence -= 0.1

    return RuleEvaluation(
        cleaning_start=c_start,
        cleaning_end=c_end,
        window_start=c_start - timedelta(hours=rules.pre_window_hours),
        window_end=c_end + timedelta(hours=rules.post_window_hours),
        coverage_pct=wa.coverage_pct,
        sufficient_coverage=wa.coverage_pct >= rules.min_window_coverage_pct,
        rain=rain,
        sand=sand,
        triggered_rules=triggered,
        deterministic_action=action,
        deterministic_reason=reason,
        allowed_actions=allowed,
        earliest_reschedule=snap_to_cleaning_hours(earliest, settings) if earliest else None,
        latest_allowed=latest_allowed,
        postpone_candidate=candidate,
        candidate_beyond_coverage=beyond,
        postpone_limit_reached=limit_reached,
        skip_next_time=skip_next,
        confidence=round(max(0.05, min(0.99, confidence)), 2),
        conditions=_describe(wa, tz),
    )
