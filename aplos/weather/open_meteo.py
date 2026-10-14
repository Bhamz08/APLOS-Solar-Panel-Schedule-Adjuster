"""Open-Meteo provider (no API key required).

Forecast: https://open-meteo.com/en/docs
Dust:     https://open-meteo.com/en/docs/air-quality-api (CAMS `dust`, µg/m³)

Past hours returned by Open-Meteo are model best estimates, not station
observations, so they are labelled `past_estimate`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from aplos.models import DataSource, ForecastBundle, PointKind
from aplos.timeutil import floor_hour, utcnow
from aplos.weather.base import WeatherProviderError, get_json
from aplos.weather.validation import build_point

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
HOURLY = (
    "temperature_2m,relative_humidity_2m,precipitation_probability,precipitation,"
    "wind_speed_10m,wind_gusts_10m,weather_code"
)

# WMO weather interpretation codes used by Open-Meteo.
WMO_CONDITIONS = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Heavy showers", 82: "Violent showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
}


class OpenMeteoProvider:
    name = "open-meteo"
    source = DataSource.API

    def __init__(
        self,
        timeout_s: float = 10,
        retries: int = 2,
        forecast_days: int = 5,
        include_dust: bool = True,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.retries = retries
        self.forecast_days = forecast_days
        self.include_dust = include_dust
        self.session = session or requests.Session()

    def fetch(self, latitude: float, longitude: float) -> ForecastBundle:
        fetched_at = utcnow()
        data = get_json(
            self.session,
            FORECAST_URL,
            {
                "latitude": latitude, "longitude": longitude, "hourly": HOURLY, "past_hours": 12,
                # forecast_days is ignored by the API when past_hours is set; use forecast_hours.
                "forecast_hours": self.forecast_days * 24, "timezone": "UTC", "timeformat": "unixtime",
                "wind_speed_unit": "kmh", "precipitation_unit": "mm",
            },
            self.timeout_s,
            self.retries,
        )
        warnings: list[str] = []
        dust = self._fetch_dust(latitude, longitude, warnings) if self.include_dust else {}
        return parse_forecast(data, dust, fetched_at, latitude, longitude, warnings)

    def _fetch_dust(self, latitude: float, longitude: float, warnings: list[str]) -> dict[int, float]:
        try:
            data = get_json(
                self.session,
                AIR_QUALITY_URL,
                {
                    "latitude": latitude, "longitude": longitude, "hourly": "dust", "past_hours": 12,
                    "forecast_hours": min(self.forecast_days, 5) * 24, "timezone": "UTC", "timeformat": "unixtime",
                },
                self.timeout_s,
                0,
            )
            hourly = data.get("hourly") or {}
            return {
                int(t): float(v)
                for t, v in zip(hourly.get("time") or [], hourly.get("dust") or [])
                if v is not None
            }
        except (WeatherProviderError, TypeError, ValueError) as exc:
            # Dust data is supplementary; continue without it.
            warnings.append(f"Dust data unavailable: {exc}")
            log.warning("Dust data unavailable", extra={"ctx": {"error": str(exc)}})
            return {}


def parse_forecast(
    data: Any,
    dust: dict[int, float],
    fetched_at: datetime,
    latitude: float,
    longitude: float,
    warnings: list[str] | None = None,
) -> ForecastBundle:
    warnings = warnings if warnings is not None else []
    if not isinstance(data, dict) or not isinstance(data.get("hourly"), dict):
        raise WeatherProviderError("Open-Meteo response missing 'hourly' block", "invalid")
    hourly = data["hourly"]
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        raise WeatherProviderError("Open-Meteo response has no forecast periods", "invalid")

    def col(name: str) -> list[Any]:
        values = hourly.get(name)
        if not isinstance(values, list) or len(values) != len(times):
            warnings.append(f"Field '{name}' missing or misaligned")
            return [None] * len(times)
        return values

    temp, rh, pop, rain = col("temperature_2m"), col("relative_humidity_2m"), col("precipitation_probability"), col("precipitation")
    wind, gust, code = col("wind_speed_10m"), col("wind_gusts_10m"), col("weather_code")
    now_hour = floor_hour(fetched_at)
    points = []
    for i, ts in enumerate(times):
        try:
            t = datetime.fromtimestamp(int(ts), timezone.utc)
        except (TypeError, ValueError, OverflowError):
            warnings.append(f"Invalid timestamp {ts!r} ignored")
            continue
        wcode = int(code[i]) if code[i] is not None else None
        point = build_point(
            {
                "time": t,
                "kind": PointKind.PAST_ESTIMATE if t < now_hour else PointKind.FORECAST,
                "temperature_c": temp[i], "humidity_pct": rh[i], "precip_probability_pct": pop[i],
                "precip_mm": rain[i], "wind_speed_kmh": wind[i], "wind_gust_kmh": gust[i],
                "dust_ugm3": dust.get(int(ts)), "weather_code": wcode,
                "condition": WMO_CONDITIONS.get(wcode) if wcode is not None else None,
            },
            warnings,
        )
        if point:
            points.append(point)
    return ForecastBundle(
        provider="open-meteo", source=DataSource.API, fetched_at=fetched_at, latitude=latitude,
        longitude=longitude, interval_hours=1.0, points=points, warnings=warnings,
    )
