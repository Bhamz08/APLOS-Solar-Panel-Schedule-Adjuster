"""Weather provider interface and shared HTTP handling."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import requests

from aplos.models import DataSource, ForecastBundle

log = logging.getLogger(__name__)


class WeatherProviderError(RuntimeError):
    """Retrieving or parsing a forecast failed (network, timeout, HTTP, schema)."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind  # timeout | connection | http | invalid | config | error


class WeatherProvider(Protocol):
    name: str
    source: DataSource

    def fetch(self, latitude: float, longitude: float) -> ForecastBundle:
        """Return a normalized (not yet validated) forecast or raise WeatherProviderError."""
        ...


def get_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    timeout_s: float,
    retries: int,
    backoff_s: float = 1.5,
) -> Any:
    """GET a JSON document with timeouts and bounded retries on transient failures."""
    last: WeatherProviderError | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=(min(timeout_s, 5.0), timeout_s))
            if resp.status_code in (401, 403):
                raise WeatherProviderError(f"Weather API rejected credentials (HTTP {resp.status_code})", "config")
            if resp.status_code == 429 or resp.status_code >= 500:
                last = WeatherProviderError(f"Weather API unavailable (HTTP {resp.status_code})", "http")
            elif resp.status_code >= 400:
                raise WeatherProviderError(f"Weather API request failed (HTTP {resp.status_code})", "http")
            else:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise WeatherProviderError("Weather API returned invalid JSON", "invalid") from exc
        except requests.Timeout:
            last = WeatherProviderError(f"Weather API timed out after {timeout_s:.0f}s", "timeout")
        except requests.ConnectionError:
            last = WeatherProviderError("Weather API unreachable (no connectivity?)", "connection")
        if attempt < retries:
            delay = backoff_s * (2**attempt)
            log.warning("Weather request failed; retrying", extra={"ctx": {"error": str(last), "retry_in_s": delay}})
            time.sleep(delay)
    assert last is not None
    raise last
