"""Headless dashboard tests (Streamlit AppTest): every page renders and key flows work."""

from datetime import timedelta

import pytest
from streamlit.testing.v1 import AppTest

from aplos.database import DecisionRepository, EventRepository, SettingsRepository, connect
from aplos.engine.pipeline import Pipeline
from aplos.llm.advisor import build_advisor
from aplos.models import Outcome
from aplos.timeutil import floor_hour, utcnow
from aplos.weather.mock import StaticWeatherProvider
from aplos.weather.service import WeatherService
from tests.conftest import make_bundle

PAGE = """
from aplos.ui.common import setup_page
from aplos.ui import {module}
setup_page()
{module}.render()
"""


@pytest.fixture
def seeded_db(tmp_path, monkeypatch, settings):
    path = tmp_path / "ui.db"
    monkeypatch.setenv("APLOS_DB_PATH", str(path))
    monkeypatch.setenv("APLOS_LOG_DIR", str(tmp_path / "logs"))
    conn = connect(path)
    settings.automation.auto_apply = False
    SettingsRepository(conn).save(settings)
    now = floor_hour(utcnow())
    events = EventRepository(conn)
    events.add(now + timedelta(hours=30))
    events.add(now + timedelta(days=7))
    rainy = make_bundle(fetched_at=now, override=lambda h, t: {"precip_mm": 1.0, "precip_probability_pct": 85}
                        if 26 <= h < 34 else {})
    Pipeline(conn, settings, WeatherService(conn, settings, StaticWeatherProvider(rainy)),
             build_advisor(settings.llm)).run("manual")
    conn.close()
    return path


@pytest.mark.parametrize("module", ["dashboard", "schedule_page", "weather_page", "history_page", "settings_page"])
def test_page_renders(seeded_db, module):
    at = AppTest.from_string(PAGE.format(module=module), default_timeout=30).run()
    assert not at.exception, at.exception


def test_approve_from_schedule_page(seeded_db):
    at = AppTest.from_string(PAGE.format(module="schedule_page"), default_timeout=30).run()
    at.text_input(key="reviewer_input").input("ops-lead").run()
    approve = [b for b in at.button if b.label == "Approve"]
    assert approve, "pending recommendation should offer an Approve button"
    approve[0].click().run()
    assert not at.exception
    conn = connect(seeded_db)
    decisions = DecisionRepository(conn).list()
    assert decisions[0].outcome == Outcome.APPROVED and decisions[0].reviewed_by == "ops-lead"
    assert EventRepository(conn).get(decisions[0].event_id).postpone_count == 1


def test_dashboard_shows_rules_only_model_status(seeded_db):
    at = AppTest.from_string(PAGE.format(module="dashboard"), default_timeout=30).run()
    html = " ".join(m.value for m in at.markdown)
    assert "RULES ONLY" in html  # never claims a model that is not loaded
    assert "Awaiting" in " ".join(i.value for i in at.info) or "awaiting" in html
