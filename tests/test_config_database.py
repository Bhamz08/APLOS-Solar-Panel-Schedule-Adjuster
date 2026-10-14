from datetime import timedelta

import pytest
from pydantic import ValidationError

from aplos.config.settings import AppSettings, RuleSettings, ScheduleSettings, SiteSettings
from aplos.database import (
    ConcurrentModificationError,
    EventRepository,
    RunInProgressError,
    RunRepository,
    SettingsRepository,
    connect,
    schema_version,
)
from aplos.database.db import MIGRATIONS
from aplos.models import EventStatus
from tests.conftest import T0


def test_defaults_are_valid():
    s = AppSettings()
    assert s.schedule.run_time.hour == 5
    assert s.site.tz.key == "Asia/Dubai"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SiteSettings(timezone="Mars/Olympus"),
        lambda: ScheduleSettings(daily_run_time="25:00"),
        lambda: ScheduleSettings(cleaning_window_start_hour=17, cleaning_window_end_hour=8),
        lambda: RuleSettings(rain_amount_threshold_mm=5, skip_rain_amount_mm=2),
        lambda: RuleSettings(stale_after_hours=50, max_forecast_age_hours=24),
    ],
)
def test_invalid_settings_rejected(factory):
    with pytest.raises(ValidationError):
        factory()


def test_settings_roundtrip(conn):
    repo = SettingsRepository(conn)
    s = repo.load()
    s.schedule.daily_run_time = "04:30"
    s.site.timezone = "Europe/Madrid"
    repo.save(s)
    loaded = repo.load()
    assert loaded.schedule.daily_run_time == "04:30"
    assert loaded.site.timezone == "Europe/Madrid"


def test_corrupt_settings_fall_back_to_defaults(conn):
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('app', '{\"site\": {\"latitude\": 999}}', 'x')")
    assert SettingsRepository(conn).load() == AppSettings()


def test_migrations_idempotent(tmp_path):
    path = tmp_path / "a.db"
    c1 = connect(path)
    assert schema_version(c1) == len(MIGRATIONS)
    c1.close()
    c2 = connect(path)  # reopening must not re-apply migrations
    assert schema_version(c2) == len(MIGRATIONS)
    c2.close()


def test_event_optimistic_locking(conn):
    repo = EventRepository(conn)
    e = repo.add(T0 + timedelta(days=1))
    repo.reschedule(e, e.scheduled_time + timedelta(hours=5), "rain", "pipeline")
    with pytest.raises(ConcurrentModificationError):
        repo.reschedule(e, e.scheduled_time + timedelta(hours=9), "stale copy", "pipeline")
    fresh = repo.get(e.id)
    assert fresh.postpone_count == 1
    assert fresh.original_time == e.original_time
    assert [c["change_type"] for c in repo.changes(e.id)] == ["POSTPONED", "CREATED"]


def test_operator_edit_resets_plan(conn):
    repo = EventRepository(conn)
    e = repo.add(T0 + timedelta(days=1))
    e = repo.reschedule(e, e.scheduled_time + timedelta(hours=5), "rain", "pipeline")
    e = repo.operator_edit(e, T0 + timedelta(days=3), "moved")
    assert e.original_time == e.scheduled_time == T0 + timedelta(days=3)
    assert e.postpone_count == 0
    e = repo.set_status(e, EventStatus.CANCELLED, "not needed", "operator")
    assert e.status == EventStatus.CANCELLED


def test_run_lock_and_stale_recovery(conn):
    runs = RunRepository(conn)
    run_id = runs.start("2026-03-10", "manual", T0)
    with pytest.raises(RunInProgressError):
        runs.start("2026-03-10", "scheduled", T0)
    # Process "crashed": no heartbeat for longer than the timeout.
    assert runs.recover_stale(T0 + timedelta(minutes=45), timeout_minutes=30) == 1
    assert runs.latest()["status"] == "ABANDONED"
    new_id = runs.start("2026-03-10", "catchup", T0 + timedelta(minutes=45))
    assert new_id != run_id
