"""Forecast retrieval with an offline-first local cache.

Order of preference on each run:
1. Fetch from the configured provider (requires internet connectivity).
2. Otherwise use the most recent cached forecast (API or operator-entered).
3. Otherwise report UNAVAILABLE; the pipeline then makes no schedule changes.

Connectivity is retried automatically on every run, so the system
reconnects as soon as the network returns.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime

from aplos.config import env
from aplos.config.settings import AppSettings
from aplos.database.repositories import ForecastRepository
from aplos.models import DataSource, ForecastBundle, ForecastPoint, Freshness
from aplos.timeutil import Clock, hours_between, utcnow
from aplos.weather.base import WeatherProvider, WeatherProviderError
from aplos.weather.mock import MockWeatherProvider
from aplos.weather.open_meteo import OpenMeteoProvider
from aplos.weather.openweathermap import OpenWeatherMapProvider
from aplos.weather.validation import ForecastValidationError, validate_bundle

log = logging.getLogger(__name__)


@dataclass
class ForecastResult:
    bundle: ForecastBundle | None
    freshness: Freshness
    age_hours: float | None
    fetched_this_run: bool
    errors: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.bundle is not None and self.freshness != Freshness.UNAVAILABLE


def build_provider(settings: AppSettings, clock: Clock = utcnow) -> WeatherProvider:
    w = settings.weather
    if w.provider == "open_meteo":
        return OpenMeteoProvider(w.request_timeout_s, w.retries, w.forecast_days, w.include_dust)
    if w.provider == "openweathermap":
        return OpenWeatherMapProvider(env.get_secret(env.ENV_OPENWEATHER_KEY), w.request_timeout_s, w.retries)
    if w.provider == "mock":
        return MockWeatherProvider(w.mock_scenario, clock=clock)
    raise ValueError(f"Unknown weather provider {w.provider!r}")


class WeatherService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: AppSettings,
        provider: WeatherProvider | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self.repo = ForecastRepository(conn)
        self.settings = settings
        self.clock = clock
        self.provider = provider if provider is not None else build_provider(settings, clock)

    # Data from mock providers is only ever used when the mock provider is selected.
    def _trusted_sources(self) -> list[DataSource]:
        if self.provider.source == DataSource.MOCK:
            return [DataSource.MOCK, DataSource.MANUAL]
        if self.provider.source == DataSource.REPLAY:
            return [DataSource.REPLAY]
        return [DataSource.API, DataSource.MANUAL]

    def refresh(self) -> ForecastBundle:
        """Fetch, validate and cache a forecast. Raises WeatherProviderError/ForecastValidationError."""
        site = self.settings.site
        started = time.perf_counter()
        bundle = self.provider.fetch(site.latitude, site.longitude)
        bundle = validate_bundle(bundle, self.clock())
        saved = self.repo.save(bundle)
        log.info(
            "Forecast retrieved",
            extra={"ctx": {
                "provider": bundle.provider, "points": len(bundle.points), "forecast_id": saved.id,
                "ms": int((time.perf_counter() - started) * 1000), "warnings": len(bundle.warnings),
            }},
        )
        return saved

    def get_forecast(self, try_fetch: bool = True) -> ForecastResult:
        errors: list[str] = []
        if try_fetch:
            try:
                bundle = self.refresh()
                return self._result(bundle, fetched_this_run=True, errors=errors)
            except (WeatherProviderError, ForecastValidationError) as exc:
                errors.append(f"Forecast retrieval failed: {exc}")
                log.warning("Forecast retrieval failed; falling back to cache", extra={"ctx": {"error": str(exc)}})
            except Exception as exc:  # unexpected provider bug must not stop the pipeline
                errors.append(f"Unexpected forecast error: {exc!r}")
                log.exception("Unexpected forecast error")
        cached = self.latest_cached()
        if cached is None:
            errors.append("No cached forecast available")
            return ForecastResult(None, Freshness.UNAVAILABLE, None, False, errors)
        return self._result(cached, fetched_this_run=False, errors=errors)

    def latest_cached(self) -> ForecastBundle | None:
        # Never fall back to a forecast cached for a different site location.
        site = self.settings.site
        return self.repo.latest(self._trusted_sources(), site.latitude, site.longitude)

    def classify(self, bundle: ForecastBundle, fetched_this_run: bool) -> tuple[Freshness, float]:
        rules = self.settings.rules
        age = max(0.0, hours_between(bundle.fetched_at, self.clock()))
        if age > rules.max_forecast_age_hours:
            return Freshness.UNAVAILABLE, age
        if age > rules.stale_after_hours:
            return Freshness.STALE, age
        return (Freshness.FRESH if fetched_this_run else Freshness.CACHED), age

    def _result(self, bundle: ForecastBundle, fetched_this_run: bool, errors: list[str]) -> ForecastResult:
        freshness, age = self.classify(bundle, fetched_this_run)
        if freshness == Freshness.UNAVAILABLE:
            errors.append(f"Latest forecast is {age:.1f}h old (limit {self.settings.rules.max_forecast_age_hours}h)")
        return ForecastResult(bundle, freshness, age, fetched_this_run, errors)

    def save_manual(self, points: list[ForecastPoint], entered_at: datetime | None = None, note: str = "") -> ForecastBundle:
        """Store operator-supplied weather (offline operation / on-site readings)."""
        site = self.settings.site
        interval = 1.0
        if len(points) >= 2:
            interval = max(0.25, min(24.0, hours_between(points[0].time, points[1].time)))
        bundle = ForecastBundle(
            provider="manual", source=DataSource.MANUAL, fetched_at=entered_at or self.clock(),
            latitude=site.latitude, longitude=site.longitude, interval_hours=interval, points=points,
            warnings=[f"Manually supplied weather data{': ' + note if note else ''}"],
        )
        bundle = validate_bundle(bundle, self.clock())
        return self.repo.save(bundle)
