"""Structured prompts for the cleaning-schedule advisor.

Gemma 2 has no system role; instructions are placed in the first user turn
and the chat template is applied in `backends.format_gemma_prompt`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from aplos.config.settings import AppSettings
from aplos.engine.rules import RuleEvaluation
from aplos.models import CleaningEvent, ForecastBundle
from aplos.timeutil import fmt_local

PROMPT_VERSION = "2026-10-v1"

INSTRUCTIONS = """You are a solar farm operations assistant. Decide whether a scheduled panel cleaning should go ahead.
Actions:
- CLEAN: proceed at the scheduled time.
- POSTPONE: move the cleaning later because rain or sand/dust would prevent it or soon undo it.
- SKIP: cancel this cleaning because heavy rain will rinse the panels; the next regular cleaning stays.
Rules you must follow:
1. Choose ONLY an action listed in "allowed_actions". These come from hard site rules.
2. If you POSTPONE, next_cleaning_time must be no earlier than "earliest_reschedule_local" and no later than "latest_allowed_local". Use local time format YYYY-MM-DDTHH:MM.
3. If you choose CLEAN, set next_cleaning_time to null.
4. Base your answer only on the data given. Do not invent weather.
5. confidence is a number from 0 to 1.
Reply with a single JSON object and nothing else:
{"action": "...", "reason": "one or two sentences", "conditions": ["short facts"], "confidence": 0.0, "next_cleaning_time": null}"""


def _blocks(bundle: ForecastBundle, ev: RuleEvaluation, settings: AppSettings, block_hours: int = 3) -> list[dict[str, Any]]:
    """Summarize the evaluation window in fixed blocks to fit a 2B model's context."""
    tz = settings.site.tz
    out = []
    t = ev.window_start
    while t < ev.window_end:
        end = t + timedelta(hours=block_hours)
        pts = bundle.between(t, end)
        if pts:
            def mx(attr: str) -> float | None:
                vals = [getattr(p, attr) for p in pts if getattr(p, attr) is not None]
                return round(max(vals), 1) if vals else None

            rain_vals = [p.precip_mm for p in pts if p.precip_mm is not None]
            out.append({
                "from": fmt_local(t, tz, "%a %H:%M"),
                "rain_mm": round(sum(rain_vals), 1) if rain_vals else None,
                "rain_prob_pct": mx("precip_probability_pct"),
                "gust_kmh": mx("wind_gust_kmh"),
                "dust_ugm3": mx("dust_ugm3"),
                "humidity_pct": mx("humidity_pct"),
                "temp_c": mx("temperature_c"),
                "sand_reported": any(p.dust_event_flag for p in pts) or None,
                "during_cleaning": t < ev.cleaning_end and end > ev.cleaning_start,
            })
        t = end
    return [{k: v for k, v in b.items() if v is not None} for b in out]


def build_context(
    event: CleaningEvent,
    bundle: ForecastBundle,
    ev: RuleEvaluation,
    settings: AppSettings,
    freshness: str,
    age_hours: float | None,
) -> dict[str, Any]:
    tz = settings.site.tz
    r = settings.rules
    return {
        "site": {"name": settings.site.name, "timezone": settings.site.timezone},
        "planned_cleaning_local": fmt_local(ev.cleaning_start, tz, "%Y-%m-%dT%H:%M"),
        "cleaning_duration_hours": settings.schedule.cleaning_duration_hours,
        "original_plan_local": fmt_local(event.original_time, tz, "%Y-%m-%dT%H:%M"),
        "times_already_postponed": event.postpone_count,
        "forecast": {
            "provider": bundle.provider, "freshness": freshness,
            "age_hours": round(age_hours, 1) if age_hours is not None else None,
            "blocks_3h": _blocks(bundle, ev, settings),
        },
        "site_rules": {
            "significant_rain_mm": r.rain_amount_threshold_mm,
            "rain_probability_threshold_pct": r.rain_probability_threshold_pct,
            "heavy_rain_skip_mm": r.skip_rain_amount_mm,
            "dry_buffer_hours": r.dry_buffer_hours,
            "dust_threshold_ugm3": r.dust_threshold_ugm3,
            "sand_gust_threshold_kmh": r.sand_wind_gust_threshold_kmh,
            "dust_settle_hours": r.dust_settle_hours,
        },
        "rule_engine": {
            "findings": ev.conditions,
            "triggered_rules": ev.triggered_rules,
            "default_action": ev.deterministic_action.value,
            "allowed_actions": [a.value for a in ev.allowed_actions],
            "earliest_reschedule_local": fmt_local(ev.earliest_reschedule, tz, "%Y-%m-%dT%H:%M") if ev.earliest_reschedule else None,
            "suggested_reschedule_local": fmt_local(ev.postpone_candidate, tz, "%Y-%m-%dT%H:%M") if ev.postpone_candidate else None,
            "latest_allowed_local": fmt_local(ev.latest_allowed, tz, "%Y-%m-%dT%H:%M"),
        },
    }


def build_prompt(context: dict[str, Any]) -> str:
    return f"{INSTRUCTIONS}\n\nDATA:\n{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
