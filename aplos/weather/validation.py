"""Normalization and validation of incoming forecast data."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from aplos.models import DataSource, ForecastBundle, ForecastPoint

MIN_POINTS = 6
MAX_CLOCK_SKEW = timedelta(minutes=10)


class ForecastValidationError(ValueError):
    pass


def build_point(raw: dict[str, Any], warnings: list[str]) -> ForecastPoint | None:
    """Build a point, dropping it (with a warning) when values are out of range."""
    try:
        return ForecastPoint.model_validate(raw)
    except ValidationError as exc:
        fields = ", ".join(str(e["loc"][0]) for e in exc.errors())
        warnings.append(f"Dropped point {raw.get('time')}: invalid {fields}")
        return None


def validate_bundle(bundle: ForecastBundle, now: datetime) -> ForecastBundle:
    """Sort, de-duplicate and sanity-check a bundle. Raises ForecastValidationError."""
    warnings = list(bundle.warnings)
    if bundle.fetched_at > now + MAX_CLOCK_SKEW:
        raise ForecastValidationError("Forecast timestamp is in the future; check the system clock")

    seen: dict[datetime, ForecastPoint] = {}
    for point in bundle.points:
        if point.time in seen:
            warnings.append(f"Duplicate point {point.time.isoformat()} ignored")
            continue
        seen[point.time] = point
    points = sorted(seen.values(), key=lambda p: p.time)

    if len(points) < MIN_POINTS:
        raise ForecastValidationError(f"Forecast has only {len(points)} valid points (need {MIN_POINTS})")
    # Operator-entered observations may legitimately all be in the past.
    if points[-1].time <= now and bundle.source != DataSource.MANUAL:
        raise ForecastValidationError("Forecast contains no future periods")

    informative = [p for p in points if p.precip_probability_pct is not None or p.precip_mm is not None]
    if not informative:
        raise ForecastValidationError("Forecast has no precipitation data")

    step = timedelta(hours=bundle.interval_hours)
    gaps = sum(1 for a, b in zip(points, points[1:]) if b.time - a.time > step * 1.5)
    if gaps:
        warnings.append(f"Forecast has {gaps} gap(s) in its time series")

    return bundle.model_copy(update={"points": points, "warnings": warnings})
