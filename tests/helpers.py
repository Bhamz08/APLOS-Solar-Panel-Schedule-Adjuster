"""Scenario builders shared by tests. Times are hour offsets from T0 (UTC)."""

from __future__ import annotations

from datetime import timedelta

from aplos.models import CleaningEvent, EventStatus
from tests.conftest import T0, make_bundle

# Cleaning at T0+30h = 2026-03-11 06:00 UTC = 10:00 Asia/Dubai.
EVENT_H = 30


def event(postpone_count: int = 0, h: int = EVENT_H) -> CleaningEvent:
    t = T0 + timedelta(hours=h)
    return CleaningEvent(
        id=1, original_time=t, scheduled_time=t, status=EventStatus.SCHEDULED, postpone_count=postpone_count,
        created_at=T0, updated_at=T0,
    )


def rain(start_h: int, end_h: int, mm: float, prob: float = 80):
    return lambda h, t: {"precip_mm": mm, "precip_probability_pct": prob, "condition": "Rain"} if start_h <= h < end_h else {}


def sand(start_h: int, end_h: int, dust: float | None = 800, gust: float = 60):
    return lambda h, t: {"dust_ugm3": dust, "wind_gust_kmh": gust, "condition": "Sand"} if start_h <= h < end_h else {}


def combine(*fns):
    def f(h, t):
        out = {}
        for fn in fns:
            out.update(fn(h, t))
        return out
    return f


def rain_bundle(**kw):
    return make_bundle(override=rain(28, 34, 1.0), **kw)  # 6 mm at 80 %, spanning the cleaning start


def heavy_bundle(**kw):
    return make_bundle(override=rain(28, 34, 3.0), **kw)  # 18 mm


def sand_bundle(**kw):
    return make_bundle(override=sand(36, 43), **kw)  # dust event after cleaning
