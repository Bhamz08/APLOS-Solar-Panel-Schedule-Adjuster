"""OpenWeatherMap 5-day / 3-hour forecast provider (requires OPENWEATHER_API_KEY).

https://openweathermap.org/forecast5
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from aplos.models import DataSource, ForecastBundle, PointKind
from aplos.timeutil import utcnow
from aplos.weather.base import WeatherProviderError, get_json
from aplos.weather.validation import build_point

FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
# OWM condition codes that indicate airborne sand or dust.
DUST_CODES = {731, 751, 761}


class OpenWeatherMapProvider:
    name = "openweathermap"
    source = DataSource.API

    def __init__(
        self, api_key: str | None, timeout_s: float = 10, retries: int = 2, session: requests.Session | None = None
    ) -> None:
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.retries = retries
        self.session = session or requests.Session()

    def fetch(self, latitude: float, longitude: float) -> ForecastBundle:
        if not self.api_key:
            raise WeatherProviderError("OPENWEATHER_API_KEY is not configured", "config")
        fetched_at = utcnow()
        data = get_json(
            self.session,
            FORECAST_URL,
            {"lat": latitude, "lon": longitude, "appid": self.api_key, "units": "metric"},
            self.timeout_s,
            self.retries,
        )
        return parse_forecast(data, fetched_at, latitude, longitude)


def parse_forecast(data: Any, fetched_at: datetime, latitude: float, longitude: float) -> ForecastBundle:
    if not isinstance(data, dict) or not isinstance(data.get("list"), list) or not data["list"]:
        raise WeatherProviderError("OpenWeatherMap response has no forecast list", "invalid")
    warnings: list[str] = []
    points = []
    for item in data["list"]:
        if not isinstance(item, dict):
            continue
        main, wind = item.get("main") or {}, item.get("wind") or {}
        weather = (item.get("weather") or [{}])[0] or {}
        code = weather.get("id")
        pop = item.get("pop")
        rain = (item.get("rain") or {}).get("3h")
        snow = (item.get("snow") or {}).get("3h")
        precip = None if rain is None and snow is None else float(rain or 0) + float(snow or 0)
        if precip is None and pop is not None:
            precip = 0.0
        try:
            t = datetime.fromtimestamp(int(item["dt"]), timezone.utc)
        except (KeyError, TypeError, ValueError):
            warnings.append("Forecast item without a valid timestamp ignored")
            continue
        point = build_point(
            {
                "time": t,
                "kind": PointKind.FORECAST,
                "temperature_c": main.get("temp"),
                "humidity_pct": main.get("humidity"),
                "precip_probability_pct": float(pop) * 100 if pop is not None else None,
                "precip_mm": precip,
                "wind_speed_kmh": float(wind["speed"]) * 3.6 if wind.get("speed") is not None else None,
                "wind_gust_kmh": float(wind["gust"]) * 3.6 if wind.get("gust") is not None else None,
                "weather_code": code,
                "condition": weather.get("description"),
                "dust_event_flag": code in DUST_CODES,
            },
            warnings,
        )
        if point:
            points.append(point)
    return ForecastBundle(
        provider="openweathermap", source=DataSource.API, fetched_at=fetched_at, latitude=latitude,
        longitude=longitude, interval_hours=3.0, points=points, warnings=warnings,
    )
