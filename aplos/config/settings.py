"""Validated application settings.

Settings are stored as JSON in SQLite (see `aplos.database.repositories`)
and edited from the dashboard. Every threshold is configurable because no
single set of numbers fits every installation; the defaults suit a dusty,
arid utility-scale site and are a starting point only.
"""

from __future__ import annotations

import re
from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> time:
    match = _HHMM.match(value.strip())
    if not match:
        raise ValueError(f"Expected HH:MM (24h), got {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class SiteSettings(_Base):
    name: str = Field("Primary Solar Site", min_length=1, max_length=100)
    latitude: float = Field(24.4539, ge=-90, le=90)
    longitude: float = Field(54.3773, ge=-180, le=180)
    timezone: str = "Asia/Dubai"

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown time zone: {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class ScheduleSettings(_Base):
    daily_run_time: str = Field("05:00", description="Local time the daily job runs (HH:MM)")
    cleaning_window_start_hour: int = Field(6, ge=0, le=23, description="Earliest local start hour")
    cleaning_window_end_hour: int = Field(16, ge=0, le=23, description="Latest local start hour")
    cleaning_duration_hours: float = Field(3.0, gt=0, le=24)
    evaluation_horizon_hours: int = Field(72, ge=6, le=240, description="Only events this close are evaluated")
    default_interval_days: int = Field(14, ge=1, le=120)
    default_cleaning_time: str = "07:00"
    auto_extend_plan: bool = Field(False, description="Keep the recurring plan filled ahead automatically")
    plan_horizon_days: int = Field(183, ge=14, le=730, description="How far ahead the plan is kept filled")
    lock_timeout_minutes: int = Field(30, ge=1, le=1440)

    @field_validator("daily_run_time", "default_cleaning_time")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        parse_hhmm(value)
        return value.strip()

    @model_validator(mode="after")
    def _window(self) -> "ScheduleSettings":
        if self.cleaning_window_start_hour > self.cleaning_window_end_hour:
            raise ValueError("Cleaning window start hour must not be after the end hour")
        return self

    @property
    def run_time(self) -> time:
        return parse_hhmm(self.daily_run_time)


class RuleSettings(_Base):
    pre_window_hours: float = Field(6, ge=0, le=72, description="Hours before cleaning that are checked")
    post_window_hours: float = Field(36, ge=0, le=168, description="Hours after cleaning that are checked")
    rain_amount_threshold_mm: float = Field(2.0, gt=0, le=200, description="Window rain total treated as significant")
    rain_probability_threshold_pct: float = Field(60, ge=0, le=100, description="Forecast confidence needed to act on rain")
    marginal_probability_pct: float = Field(40, ge=0, le=100, description="Below the threshold but worth a model opinion")
    skip_rain_amount_mm: float = Field(10.0, gt=0, le=500, description="Rain heavy enough to rinse panels (SKIP)")
    wet_hour_mm: float = Field(0.2, gt=0, le=10, description="Hourly rain that counts as a wet hour")
    dry_buffer_hours: float = Field(12, ge=0, le=96, description="Wait after rain before cleaning")
    dust_threshold_ugm3: float = Field(200, gt=0, le=20000, description="Dust concentration treated as a sand/dust event")
    sand_wind_gust_threshold_kmh: float = Field(45, gt=0, le=250, description="Gusts that lift sand at this site")
    use_wind_as_sand_proxy: bool = Field(True, description="Treat strong gusts as sand risk when no dust data exists")
    dust_settle_hours: float = Field(6, ge=0, le=96, description="Wait after a sand/dust event before cleaning")
    max_postpone_hours: float = Field(120, ge=1, le=720, description="Maximum delay from the originally planned time")
    max_postponements: int = Field(3, ge=0, le=20, description="Maximum automatic postponements per event")
    min_model_confidence: float = Field(0.6, ge=0, le=1, description="Model output below this is ignored")
    min_window_coverage_pct: float = Field(80, ge=0, le=100, description="Forecast coverage of the window required")
    stale_after_hours: float = Field(12, gt=0, le=168, description="Forecast age that is flagged stale")
    max_forecast_age_hours: float = Field(48, gt=0, le=336, description="Older forecasts are not used at all")

    @model_validator(mode="after")
    def _consistency(self) -> "RuleSettings":
        if self.skip_rain_amount_mm < self.rain_amount_threshold_mm:
            raise ValueError("Skip rain amount must be at least the rain amount threshold")
        if self.marginal_probability_pct > self.rain_probability_threshold_pct:
            raise ValueError("Marginal probability must not exceed the rain probability threshold")
        if self.stale_after_hours > self.max_forecast_age_hours:
            raise ValueError("Stale-after hours must not exceed the maximum forecast age")
        return self


class WeatherSettings(_Base):
    provider: Literal["open_meteo", "openweathermap", "mock"] = "open_meteo"
    request_timeout_s: float = Field(10, ge=1, le=120)
    retries: int = Field(2, ge=0, le=5)
    include_dust: bool = Field(True, description="Fetch dust concentration (Open-Meteo air quality)")
    forecast_days: int = Field(5, ge=1, le=16)
    mock_scenario: Literal["clear", "rain", "heavy_rain", "sand", "mixed"] = "clear"


class LLMSettings(_Base):
    mode: Literal["local", "mock", "disabled"] = "local"
    model_path: str = "models/gemma-2-2b-it-Q4_K_M.gguf"
    n_ctx: int = Field(2048, ge=512, le=8192)
    n_threads: int = Field(0, ge=0, le=128, description="0 = auto")
    n_gpu_layers: int = Field(0, ge=-1, le=200, description="-1 = offload all layers")
    temperature: float = Field(0.1, ge=0, le=2)
    max_tokens: int = Field(384, ge=64, le=2048)
    timeout_s: float = Field(90, ge=5, le=900)


class AutomationSettings(_Base):
    auto_apply: bool = Field(True, description="Apply recommendations without operator approval")
    auto_apply_on_stale: bool = Field(False, description="Allow automatic changes based on stale forecasts")


class AppSettings(_Base):
    site: SiteSettings = Field(default_factory=SiteSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    rules: RuleSettings = Field(default_factory=RuleSettings)
    weather: WeatherSettings = Field(default_factory=WeatherSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    automation: AutomationSettings = Field(default_factory=AutomationSettings)
