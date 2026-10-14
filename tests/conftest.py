from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Never let tests read a developer's real .env secrets or model path.
os.environ.pop("APLOS_MODEL_PATH", None)
os.environ.pop("OPENWEATHER_API_KEY", None)

from aplos.config.settings import AppSettings  # noqa: E402
from aplos.database import connect  # noqa: E402
from aplos.models import DataSource, ForecastBundle, ForecastPoint, PointKind  # noqa: E402

# 2026-03-10 00:00 UTC == 04:00 in Asia/Dubai (default site time zone).
T0 = datetime(2026, 3, 10, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def settings() -> AppSettings:
    s = AppSettings()
    s.llm.mode = "disabled"
    return s


def make_points(
    start: datetime,
    hours: int = 120,
    override: Callable[[int, datetime], dict] | None = None,
) -> list[ForecastPoint]:
    """Hourly clear-weather points; `override(hour_index, time)` returns field overrides."""
    pts = []
    for h in range(hours):
        t = start + timedelta(hours=h)
        p = {
            "time": t, "kind": PointKind.FORECAST, "temperature_c": 28.0, "humidity_pct": 40.0,
            "precip_probability_pct": 5.0, "precip_mm": 0.0, "wind_speed_kmh": 12.0, "wind_gust_kmh": 20.0,
            "dust_ugm3": 50.0, "condition": "Clear",
        }
        if override:
            p.update(override(h, t))
        pts.append(ForecastPoint.model_validate(p))
    return pts


def make_bundle(
    fetched_at: datetime = T0,
    override: Callable[[int, datetime], dict] | None = None,
    hours: int = 120,
    source: DataSource = DataSource.API,
) -> ForecastBundle:
    return ForecastBundle(
        provider="test", source=source, fetched_at=fetched_at, latitude=24.45, longitude=54.38,
        interval_hours=1.0, points=make_points(fetched_at.replace(minute=0, second=0), hours, override),
    )


def between(t: datetime, start: datetime, end: datetime) -> bool:
    return start <= t < end
