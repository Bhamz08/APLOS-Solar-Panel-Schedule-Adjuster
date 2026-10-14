from datetime import date, timedelta

from aplos.database import EventRepository, RunRepository, SettingsRepository
from aplos.scheduler.daily import DailyScheduler, check_due, next_run_at
from aplos.scheduler.schedule import ScheduleManager
from aplos.timeutil import fmt_local
from tests.conftest import T0
from tests.test_pipeline import make_pipeline

# T0 = 04:00 Asia/Dubai; default daily run time is 05:00 local.


def test_next_run_respects_time_and_zone(settings):
    assert fmt_local(next_run_at(T0, settings), settings.site.tz) == "2026-03-10 05:00"
    settings.schedule.daily_run_time = "03:30"
    assert fmt_local(next_run_at(T0, settings), settings.site.tz) == "2026-03-11 03:30"
    settings.site.timezone = "UTC"
    settings.schedule.daily_run_time = "06:15"
    assert next_run_at(T0, settings) == T0 + timedelta(hours=6, minutes=15)


def test_due_logic(conn, settings, clock):
    assert not check_due(conn, settings, clock()).due
    clock.advance(hours=1, minutes=1)  # 05:01 local
    c = check_due(conn, settings, clock())
    assert c.due and c.trigger == "scheduled"
    clock.advance(hours=3)
    assert check_due(conn, settings, clock()).trigger == "catchup"


def _scheduler(conn, settings, clock, **kw):
    SettingsRepository(conn).save(settings)
    return DailyScheduler(conn, lambda c, s: make_pipeline(c, s, clock, **kw), clock)


def test_tick_runs_once_per_day(conn, settings, clock):
    sched = _scheduler(conn, settings, clock)
    assert sched.tick() is None  # before run time
    clock.advance(hours=1, minutes=1)
    assert sched.tick().status == "SUCCESS"
    clock.advance(minutes=30)
    assert sched.tick() is None  # duplicate execution prevented
    assert len(RunRepository(conn).list()) == 1
    clock.advance(days=1)
    assert sched.tick().run_date == "2026-03-11"


def test_restart_recovers_crashed_run_and_catches_up(conn, settings, clock):
    SettingsRepository(conn).save(settings)
    clock.advance(hours=1, minutes=1)
    RunRepository(conn).start("2026-03-10", "scheduled", clock())  # process died mid-run
    clock.advance(hours=2)  # machine rebooted later
    summary = DailyScheduler(conn, lambda c, s: make_pipeline(c, s, clock), clock).tick()
    assert summary is not None and summary.status == "SUCCESS"
    statuses = [r["status"] for r in RunRepository(conn).list()]
    assert statuses == ["SUCCESS", "ABANDONED"]


def test_failed_run_is_retried_with_backoff(conn, settings, clock):
    SettingsRepository(conn).save(settings)
    calls = {"n": 0}

    class Boom:
        def run(self, trigger):
            calls["n"] += 1
            runs = RunRepository(conn)
            rid = runs.start("2026-03-10", trigger, clock())
            runs.finish(rid, "FAILED", clock(), 1, {}, "boom")

    sched = DailyScheduler(conn, lambda c, s: Boom(), clock)
    clock.advance(hours=1, minutes=1)
    sched.tick()
    sched.tick()  # too soon to retry
    assert calls["n"] == 1
    clock.advance(minutes=31)
    sched.tick()
    assert calls["n"] == 2
    for _ in range(3):
        clock.advance(minutes=31)
        sched.tick()
    assert calls["n"] == 3  # gives up after MAX_ATTEMPTS


def test_settings_changes_picked_up_by_running_daemon(conn, settings, clock):
    sched = _scheduler(conn, settings, clock)
    settings.schedule.daily_run_time = "04:10"
    SettingsRepository(conn).save(settings)
    clock.advance(minutes=11)
    assert sched.tick() is not None


def test_extend_plan_keeps_horizon_filled(conn, settings, clock):
    settings.schedule.auto_extend_plan = True
    settings.schedule.plan_horizon_days = 60
    mgr = ScheduleManager(conn, settings, clock)
    mgr.generate_recurring(date(2026, 3, 12), count=2, interval_days=14, at="07:00")  # 12 and 26 Mar
    added = mgr.extend_plan(clock())
    assert [fmt_local(e.scheduled_time, settings.site.tz, "%d %b %H:%M") for e in added] == [
        "09 Apr 07:00", "23 Apr 07:00", "07 May 07:00",
    ]
    assert mgr.extend_plan(clock()) == []  # idempotent
    clock.advance(days=14)
    assert len(mgr.extend_plan(clock())) == 1  # one more as the horizon moves


def test_extend_plan_ignores_postponements_and_respects_cancellations(conn, settings, clock):
    settings.schedule.auto_extend_plan = True
    settings.schedule.plan_horizon_days = 31  # horizon reaches past 9 Apr 07:00
    mgr = ScheduleManager(conn, settings, clock)
    first, last = mgr.generate_recurring(date(2026, 3, 12), count=2, interval_days=14, at="07:00")
    # Postpone the last event by 2 days: the cycle must stay anchored to its original date.
    EventRepository(conn).reschedule(last, last.scheduled_time + timedelta(days=2), "rain", "pipeline")
    mgr.cancel_event(last.id, "not needed")  # a cancelled cleaning is not recreated
    added = mgr.extend_plan(clock())
    assert [fmt_local(e.scheduled_time, settings.site.tz, "%d %b") for e in added] == ["09 Apr"]


def test_extend_plan_disabled_by_default(conn, settings, clock):
    mgr = ScheduleManager(conn, settings, clock)
    mgr.generate_recurring(date(2026, 3, 12), count=1)
    assert mgr.extend_plan(clock()) == []


def test_pipeline_run_extends_plan(conn, settings, clock):
    settings.schedule.auto_extend_plan = True
    ScheduleManager(conn, settings, clock).generate_recurring(date(2026, 3, 12), count=1, interval_days=14)
    summary = make_pipeline(conn, settings, clock).run()
    assert summary.events_added == 12  # 26 Mar … 27 Aug fit in the 183-day horizon (to 9 Sep)


def test_generate_recurring_is_not_duplicated(conn, settings):
    mgr = ScheduleManager(conn, settings)
    created = mgr.generate_recurring(date(2026, 3, 12), count=4, interval_days=7, at="07:00")
    assert len(created) == 4
    assert fmt_local(created[1].scheduled_time, settings.site.tz) == "2026-03-19 07:00"
    assert mgr.generate_recurring(date(2026, 3, 12), count=4, interval_days=7, at="07:00") == []
    assert len(EventRepository(conn).list()) == 4
