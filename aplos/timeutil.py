"""Time helpers. All persisted timestamps are timezone-aware UTC."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("Naive datetime is ambiguous; attach a time zone first")
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return to_utc(dt).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp. Naive values are interpreted as UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def localize(naive: datetime, tz: ZoneInfo) -> datetime:
    """Attach a site time zone to a naive local datetime and return UTC."""
    if naive.tzinfo is not None:
        return to_utc(naive)
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def local_date(dt: datetime, tz: ZoneInfo) -> str:
    return to_utc(dt).astimezone(tz).date().isoformat()


def fmt_local(dt: datetime | None, tz: ZoneInfo, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return "—"
    return to_utc(dt).astimezone(tz).strftime(fmt)


def hours_between(a: datetime, b: datetime) -> float:
    return (to_utc(b) - to_utc(a)).total_seconds() / 3600.0


def combine_local(day: datetime, at: time, tz: ZoneInfo) -> datetime:
    """Return the UTC instant for `at` (local wall time) on the local day of `day`."""
    local_day = to_utc(day).astimezone(tz).date()
    return datetime.combine(local_day, at, tzinfo=tz).astimezone(timezone.utc)


def floor_hour(dt: datetime) -> datetime:
    return to_utc(dt).replace(minute=0, second=0, microsecond=0)


def ceil_hour(dt: datetime) -> datetime:
    floored = floor_hour(dt)
    return floored if floored == to_utc(dt) else floored + timedelta(hours=1)
