"""DEVELOPMENT ONLY: synthetic weather for running the app without network access.

Data from this provider is labelled `source=mock` everywhere. The pipeline
never applies schedule changes automatically when the forecast is mock data.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from aplos.models import DataSource, ForecastBundle, ForecastPoint, PointKind
from aplos.timeutil import Clock, floor_hour, utcnow

SCENARIOS = ("clear", "rain", "heavy_rain", "sand", "mixed")


class MockWeatherProvider:
    source = DataSource.MOCK

    def __init__(self, scenario: str = "clear", seed: int = 7, clock: Clock = utcnow, hours: int = 120) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown mock scenario {scenario!r}; choose from {SCENARIOS}")
        self.scenario = scenario
        self.seed = seed
        self.clock = clock
        self.hours = hours
        self.name = f"mock:{scenario}"

    def fetch(self, latitude: float, longitude: float) -> ForecastBundle:
        now = self.clock()
        rng = random.Random(self.seed)
        start = floor_hour(now)
        points = [self._point(start + timedelta(hours=h), h, rng) for h in range(self.hours)]
        return ForecastBundle(
            provider=self.name, source=DataSource.MOCK, fetched_at=now, latitude=latitude, longitude=longitude,
            interval_hours=1.0, points=points, warnings=["MOCK DATA: synthetic weather for development"],
        )

    def _point(self, t: datetime, h: int, rng: random.Random) -> ForecastPoint:
        diurnal = math.sin((t.hour - 9) / 24 * 2 * math.pi)
        p = {
            "time": t, "kind": PointKind.FORECAST, "temperature_c": round(30 + 7 * diurnal, 1),
            "humidity_pct": round(45 - 15 * diurnal, 0), "precip_probability_pct": 5.0, "precip_mm": 0.0,
            "wind_speed_kmh": round(12 + 4 * rng.random(), 1), "wind_gust_kmh": round(20 + 6 * rng.random(), 1),
            "dust_ugm3": round(40 + 20 * rng.random(), 0), "condition": "Clear",
        }
        rain_band = 18 <= h <= 30
        sand_band = 20 <= h <= 32
        if self.scenario in ("rain", "mixed") and rain_band:
            p.update(precip_probability_pct=80.0, precip_mm=0.6, humidity_pct=85.0, condition="Rain")
        if self.scenario == "heavy_rain" and rain_band:
            p.update(precip_probability_pct=90.0, precip_mm=1.8, humidity_pct=92.0, condition="Heavy rain")
        if self.scenario == "sand" and sand_band or self.scenario == "mixed" and 40 <= h <= 48:
            p.update(wind_speed_kmh=42.0, wind_gust_kmh=65.0, dust_ugm3=900.0, condition="Blowing sand")
        return ForecastPoint.model_validate(p)


class StaticWeatherProvider:
    """Returns a fixed bundle (replayed datasets, tests)."""

    def __init__(self, bundle: ForecastBundle, name: str = "replay") -> None:
        self.bundle = bundle
        self.name = name
        self.source = bundle.source

    def fetch(self, latitude: float, longitude: float) -> ForecastBundle:
        return self.bundle
