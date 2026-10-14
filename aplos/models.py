"""Domain models shared across modules."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aplos.timeutil import to_utc


class Action(str, Enum):
    CLEAN = "CLEAN"
    POSTPONE = "POSTPONE"
    SKIP = "SKIP"


class FinalAction(str, Enum):
    CLEAN = "CLEAN"
    POSTPONE = "POSTPONE"
    SKIP = "SKIP"
    # No trustworthy data: the schedule is left untouched and flagged.
    UNDETERMINED = "UNDETERMINED"


class EventStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class PointKind(str, Enum):
    FORECAST = "forecast"
    # Past hours returned by a forecast API: model best estimates, not station readings.
    PAST_ESTIMATE = "past_estimate"
    # Operator-entered measurements (e.g. on-site rain gauge).
    OBSERVATION = "observation"


class DataSource(str, Enum):
    API = "api"
    MANUAL = "manual"
    MOCK = "mock"
    REPLAY = "replay"  # simulation datasets only


class Freshness(str, Enum):
    FRESH = "FRESH"  # retrieved during this run
    CACHED = "CACHED"  # from the local cache, still within the freshness limit
    STALE = "STALE"  # usable but older than the freshness limit
    UNAVAILABLE = "UNAVAILABLE"  # nothing trustworthy


class Outcome(str, Enum):
    APPLIED = "APPLIED"
    NO_CHANGE = "NO_CHANGE"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    CONFLICT = "CONFLICT"  # event changed before the decision could be applied


class _Model(BaseModel):
    model_config = ConfigDict(use_enum_values=False)


class ForecastPoint(_Model):
    time: datetime
    kind: PointKind = PointKind.FORECAST
    temperature_c: float | None = Field(None, ge=-90, le=65)
    humidity_pct: float | None = Field(None, ge=0, le=100)
    precip_probability_pct: float | None = Field(None, ge=0, le=100)
    precip_mm: float | None = Field(None, ge=0, le=500)
    wind_speed_kmh: float | None = Field(None, ge=0, le=400)
    wind_gust_kmh: float | None = Field(None, ge=0, le=500)
    dust_ugm3: float | None = Field(None, ge=0, le=100000)
    weather_code: int | None = None
    condition: str | None = Field(None, max_length=120)
    dust_event_flag: bool = False  # provider explicitly reported dust/sand

    @field_validator("time")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return to_utc(value)


class ForecastBundle(_Model):
    id: int | None = None
    provider: str
    source: DataSource
    fetched_at: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    interval_hours: float = Field(1.0, gt=0, le=24)
    points: list[ForecastPoint]
    warnings: list[str] = Field(default_factory=list)

    @field_validator("fetched_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return to_utc(value)

    @property
    def coverage_start(self) -> datetime | None:
        return self.points[0].time if self.points else None

    @property
    def coverage_end(self) -> datetime | None:
        return self.points[-1].time if self.points else None

    def between(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        """Points whose interval overlaps [start, end)."""
        from datetime import timedelta

        step = timedelta(hours=self.interval_hours)
        return [p for p in self.points if p.time < end and p.time + step > start]


class CleaningEvent(_Model):
    id: int
    original_time: datetime
    scheduled_time: datetime
    status: EventStatus
    postpone_count: int = 0
    last_reason: str | None = None
    notes: str | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime

    @property
    def is_adjusted(self) -> bool:
        return self.scheduled_time != self.original_time


class DecisionRecord(_Model):
    id: int | None = None
    run_id: int | None
    event_id: int
    run_date: str
    created_at: datetime
    event_version: int
    event_time_before: datetime
    original_time: datetime
    forecast_id: int | None
    forecast_source: str | None
    forecast_freshness: Freshness
    forecast_age_hours: float | None
    inputs: dict[str, Any]
    rules: dict[str, Any]
    model: dict[str, Any] | None
    model_source: str
    model_error: str | None
    final_action: FinalAction
    final_reason: str
    conditions: list[str]
    confidence: float | None
    proposed_time: datetime | None
    outcome: Outcome
    flags: list[str]
    applied_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
