from datetime import timedelta

import pytest
import requests

from aplos.models import DataSource, Freshness, PointKind
from aplos.weather import ForecastValidationError, WeatherProviderError, WeatherService, validate_bundle
from aplos.weather import open_meteo, openweathermap
from aplos.weather.base import get_json
from aplos.weather.mock import MockWeatherProvider, StaticWeatherProvider
from tests.conftest import T0, make_bundle, make_points


class FakeResponse:
    def __init__(self, status: int, payload=None, bad_json: bool = False):
        self.status_code = status
        self._payload = payload
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FailingProvider:
    name = "failing"
    source = DataSource.API

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def fetch(self, lat, lon):
        self.calls += 1
        raise self.exc


def test_timeout_is_retried_then_raised(monkeypatch):
    monkeypatch.setattr("aplos.weather.base.time.sleep", lambda s: None)
    session = FakeSession(requests.Timeout(), requests.Timeout(), requests.Timeout())
    with pytest.raises(WeatherProviderError) as err:
        get_json(session, "http://x", {}, timeout_s=2, retries=2)
    assert err.value.kind == "timeout"
    assert session.calls == 3


def test_server_error_then_success(monkeypatch):
    monkeypatch.setattr("aplos.weather.base.time.sleep", lambda s: None)
    session = FakeSession(FakeResponse(503), FakeResponse(200, {"ok": 1}))
    assert get_json(session, "http://x", {}, 2, 2) == {"ok": 1}


@pytest.mark.parametrize(
    "response,kind",
    [(FakeResponse(401), "config"), (FakeResponse(404), "http"), (FakeResponse(200, bad_json=True), "invalid")],
)
def test_non_retryable_errors(response, kind):
    with pytest.raises(WeatherProviderError) as err:
        get_json(FakeSession(response), "http://x", {}, 2, 2)
    assert err.value.kind == kind


def _open_meteo_payload(start, hours=24):
    ts = [int((start + timedelta(hours=h)).timestamp()) for h in range(hours)]
    return {
        "hourly": {
            "time": ts,
            "temperature_2m": [25.0] * hours,
            "relative_humidity_2m": [50] * hours,
            "precipitation_probability": [10] * (hours - 1) + [None],
            "precipitation": [0.0] * hours,
            "wind_speed_10m": [10.0] * hours,
            "wind_gusts_10m": [20.0] * hours,
            "weather_code": [0] * hours,
        }
    }


def test_open_meteo_parse_marks_past_hours_and_merges_dust():
    start = T0 - timedelta(hours=3)
    payload = _open_meteo_payload(start)
    payload["hourly"]["temperature_2m"][5] = 999  # out of range: dropped with a warning
    dust = {payload["hourly"]["time"][4]: 650.0}
    bundle = open_meteo.parse_forecast(payload, dust, T0, 24.4, 54.3)
    assert bundle.points[0].kind == PointKind.PAST_ESTIMATE
    assert bundle.points[3].kind == PointKind.FORECAST
    assert bundle.points[4].dust_ugm3 == 650.0
    assert len(bundle.points) == 23
    assert any("Dropped point" in w for w in bundle.warnings)


def test_open_meteo_missing_hourly_block():
    with pytest.raises(WeatherProviderError):
        open_meteo.parse_forecast({"error": True}, {}, T0, 0, 0)


def test_openweathermap_parse_units_and_dust_codes():
    item = {
        "dt": int(T0.timestamp()), "main": {"temp": 30, "humidity": 20}, "wind": {"speed": 10, "gust": 15},
        "pop": 0.4, "weather": [{"id": 751, "description": "sand"}],
    }
    bundle = openweathermap.parse_forecast({"list": [item]}, T0, 1, 1)
    p = bundle.points[0]
    assert p.wind_speed_kmh == pytest.approx(36.0)
    assert p.precip_probability_pct == pytest.approx(40.0)
    assert p.dust_event_flag is True
    assert bundle.interval_hours == 3.0


def test_openweathermap_requires_key():
    with pytest.raises(WeatherProviderError) as err:
        openweathermap.OpenWeatherMapProvider(api_key=None).fetch(1, 1)
    assert err.value.kind == "config"


def test_validation_rejects_empty_and_past_only():
    with pytest.raises(ForecastValidationError):
        validate_bundle(make_bundle(hours=3), T0)
    old = make_bundle(fetched_at=T0 - timedelta(days=10), hours=24)
    with pytest.raises(ForecastValidationError):
        validate_bundle(old, T0)


def test_validation_rejects_future_fetch_time():
    with pytest.raises(ForecastValidationError):
        validate_bundle(make_bundle(fetched_at=T0 + timedelta(hours=2)), T0)


def test_fresh_fetch_is_cached(conn, settings, clock):
    svc = WeatherService(conn, settings, StaticWeatherProvider(make_bundle()), clock)
    result = svc.get_forecast()
    assert result.freshness == Freshness.FRESH and result.fetched_this_run
    assert svc.latest_cached().id == result.bundle.id


def test_offline_falls_back_to_cache_then_goes_stale(conn, settings, clock):
    WeatherService(conn, settings, StaticWeatherProvider(make_bundle()), clock).get_forecast()
    offline = WeatherService(conn, settings, FailingProvider(WeatherProviderError("down", "connection")), clock)

    clock.advance(hours=2)
    r = offline.get_forecast()
    assert r.freshness == Freshness.CACHED and not r.fetched_this_run
    assert "Forecast retrieval failed" in r.errors[0]

    clock.advance(hours=12)
    assert offline.get_forecast().freshness == Freshness.STALE

    clock.advance(hours=40)
    r = offline.get_forecast()
    assert r.freshness == Freshness.UNAVAILABLE and not r.usable


def test_no_cache_and_no_network_is_unavailable(conn, settings, clock):
    svc = WeatherService(conn, settings, FailingProvider(WeatherProviderError("down", "timeout")), clock)
    r = svc.get_forecast()
    assert r.bundle is None and r.freshness == Freshness.UNAVAILABLE


def test_reconnects_when_network_returns(conn, settings, clock):
    failing = FailingProvider(WeatherProviderError("down", "connection"))
    WeatherService(conn, settings, failing, clock).get_forecast()
    clock.advance(hours=1)
    r = WeatherService(conn, settings, StaticWeatherProvider(make_bundle(fetched_at=clock())), clock).get_forecast()
    assert r.freshness == Freshness.FRESH


def test_mock_data_never_used_by_production_provider(conn, settings, clock):
    WeatherService(conn, settings, MockWeatherProvider("rain", clock=clock), clock).get_forecast()
    prod = WeatherService(conn, settings, FailingProvider(WeatherProviderError("down")), clock)
    assert prod.get_forecast().bundle is None


def test_cache_from_previous_site_location_not_used(conn, settings, clock):
    WeatherService(conn, settings, StaticWeatherProvider(make_bundle()), clock).get_forecast()
    settings.site.latitude, settings.site.longitude = 19.076, 72.8777  # site moved
    r = WeatherService(conn, settings, FailingProvider(WeatherProviderError("down")), clock).get_forecast()
    assert r.bundle is None and r.freshness == Freshness.UNAVAILABLE


def test_manual_data_used_offline(conn, settings, clock):
    svc = WeatherService(conn, settings, FailingProvider(WeatherProviderError("down")), clock)
    svc.save_manual(make_points(T0, 48), note="site gauge")
    r = svc.get_forecast()
    assert r.bundle.source == DataSource.MANUAL and r.freshness == Freshness.CACHED
