import json
from datetime import timedelta

import pytest

from aplos.database import DecisionRepository, EventRepository, RunInProgressError, RunRepository
from aplos.engine.pipeline import Pipeline
from aplos.llm.advisor import LLMAdvisor, build_advisor
from aplos.llm.backends import ScriptedBackend
from aplos.models import EventStatus, FinalAction, Freshness, Outcome
from aplos.scheduler.schedule import ApprovalError, ScheduleManager
from aplos.timeutil import fmt_local
from aplos.weather.base import WeatherProviderError
from aplos.weather.mock import MockWeatherProvider, StaticWeatherProvider
from aplos.weather.service import WeatherService
from tests.conftest import T0, make_bundle
from tests.helpers import EVENT_H, heavy_bundle, rain, rain_bundle, sand_bundle
from tests.test_weather import FailingProvider


def make_pipeline(conn, settings, clock, bundle=None, provider=None, backend=None):
    provider = provider or StaticWeatherProvider(bundle or make_bundle(fetched_at=clock()))
    advisor = LLMAdvisor(backend, settings.llm) if backend else build_advisor(settings.llm)
    return Pipeline(conn, settings, WeatherService(conn, settings, provider, clock), advisor, clock)


@pytest.fixture
def ev(conn):
    return EventRepository(conn).add(T0 + timedelta(hours=EVENT_H))


def decisions(conn):
    return DecisionRepository(conn).list()


def test_favorable_weather_no_change(conn, settings, clock, ev):
    s = make_pipeline(conn, settings, clock).run()
    assert s.status == "SUCCESS" and s.no_change == 1
    d = decisions(conn)[0]
    assert d.final_action == FinalAction.CLEAN and d.outcome == Outcome.NO_CHANGE
    assert EventRepository(conn).get(ev.id).scheduled_time == ev.scheduled_time


def test_rain_postpones_and_records_audit(conn, settings, clock, ev):
    s = make_pipeline(conn, settings, clock, rain_bundle()).run()
    assert s.applied == 1
    d = decisions(conn)[0]
    assert d.final_action == FinalAction.POSTPONE and d.outcome == Outcome.APPLIED and d.applied_at
    assert d.model_source == "none" and any("MODEL_UNAVAILABLE" in f for f in d.flags)
    assert d.inputs["forecast_window"] and d.rules["triggered_rules"]
    moved = EventRepository(conn).get(ev.id)
    assert moved.original_time == ev.original_time  # original preserved
    assert fmt_local(moved.scheduled_time, settings.site.tz) == "2026-03-12 06:00"
    assert moved.postpone_count == 1
    change = EventRepository(conn).changes(ev.id)[0]
    assert change["change_type"] == "POSTPONED" and change["decision_id"] == d.id


def test_heavy_rain_skips_event(conn, settings, clock, ev):
    make_pipeline(conn, settings, clock, heavy_bundle()).run()
    assert EventRepository(conn).get(ev.id).status == EventStatus.SKIPPED
    assert decisions(conn)[0].final_action == FinalAction.SKIP


def test_sand_postpones(conn, settings, clock, ev):
    make_pipeline(conn, settings, clock, sand_bundle()).run()
    d = decisions(conn)[0]
    assert d.final_action == FinalAction.POSTPONE and "Sand/dust" in " ".join(d.conditions)


def test_rerun_same_day_is_idempotent(conn, settings, clock, ev):
    p = make_pipeline(conn, settings, clock, rain_bundle())
    p.run()
    first = EventRepository(conn).get(ev.id)
    clock.advance(hours=1)
    s2 = make_pipeline(conn, settings, clock, rain_bundle()).run()
    assert s2.evaluated == 0 and s2.applied == 0
    assert len(decisions(conn)) == 1
    assert EventRepository(conn).get(ev.id) == first


def test_next_day_reevaluates_moved_event_without_looping(conn, settings, clock, ev):
    make_pipeline(conn, settings, clock, rain_bundle()).run()
    clock.advance(days=1)  # next local day, forecast now clear for the new time
    s = make_pipeline(conn, settings, clock, make_bundle(fetched_at=clock())).run()
    assert s.no_change == 1
    assert EventRepository(conn).get(ev.id).postpone_count == 1


def test_repeated_postponement_is_bounded(conn, settings, clock, ev):
    settings.rules.max_postponements = 2
    always_rain = lambda h, t: {"precip_mm": 1.0, "precip_probability_pct": 90}  # noqa: E731
    for _ in range(5):
        make_pipeline(conn, settings, clock, make_bundle(fetched_at=clock(), override=always_rain, hours=240)).run()
        clock.advance(days=1)
    e = EventRepository(conn).get(ev.id)
    assert e.postpone_count <= 2
    assert e.scheduled_time - e.original_time <= timedelta(hours=settings.rules.max_postpone_hours)


def test_stale_forecast_requires_approval(conn, settings, clock, ev):
    make_pipeline(conn, settings, clock, rain_bundle()).run()  # caches a forecast
    DecisionRepository(conn).conn.execute("DELETE FROM decisions")
    clock.advance(hours=14)  # same local day? 04:00 → 18:00 local, still 2026-03-10
    offline = make_pipeline(conn, settings, clock, provider=FailingProvider(WeatherProviderError("down")))
    # reset event so it is evaluated again
    conn.execute("UPDATE cleaning_events SET scheduled_time = original_time, postpone_count = 0")
    s = offline.run()
    d = decisions(conn)[0]
    assert d.forecast_freshness == Freshness.STALE
    assert d.outcome == Outcome.PENDING_APPROVAL
    assert any(f.startswith("STALE_FORECAST") for f in d.flags)
    assert s.pending_approval == 1


def test_offline_with_recent_cache_still_decides(conn, settings, clock, ev):
    WeatherService(conn, settings, StaticWeatherProvider(rain_bundle()), clock).refresh()
    clock.advance(hours=2)
    s = make_pipeline(conn, settings, clock, provider=FailingProvider(WeatherProviderError("down", "connection"))).run()
    d = decisions(conn)[0]
    assert s.forecast_freshness == "CACHED" and d.outcome == Outcome.APPLIED
    assert any(f.startswith("CACHED_FORECAST") for f in d.flags)


def test_no_data_makes_no_change(conn, settings, clock, ev):
    s = make_pipeline(conn, settings, clock, provider=FailingProvider(WeatherProviderError("down", "timeout"))).run()
    d = decisions(conn)[0]
    assert d.final_action == FinalAction.UNDETERMINED and d.outcome == Outcome.INSUFFICIENT_DATA
    assert s.insufficient_data == 1 and s.status == "SUCCESS"
    assert EventRepository(conn).get(ev.id).scheduled_time == ev.scheduled_time


def test_mock_weather_never_auto_applied(conn, settings, clock, ev):
    settings.weather.provider = "mock"
    p = make_pipeline(conn, settings, clock, provider=MockWeatherProvider("heavy_rain", clock=clock))
    p.run()
    d = decisions(conn)[0]
    assert d.outcome == Outcome.PENDING_APPROVAL and any("MOCK_WEATHER_DATA" in f for f in d.flags)


def test_model_conflict_with_rules_is_overridden(conn, settings, clock, ev):
    out = json.dumps({"action": "CLEAN", "reason": "Looks fine.", "conditions": [], "confidence": 0.95,
                      "next_cleaning_time": None})
    make_pipeline(conn, settings, clock, rain_bundle(), backend=ScriptedBackend([out])).run()
    d = decisions(conn)[0]
    assert d.final_action == FinalAction.POSTPONE
    assert any(f.startswith("MODEL_OVERRIDDEN") for f in d.flags)
    assert d.model["recommendation"]["action"] == "CLEAN"  # model output kept for audit


def test_model_choice_within_allowed_actions_is_used(conn, settings, clock, ev):
    marginal = make_bundle(override=rain(30, 32, 0.5, prob=45))
    out = json.dumps({"action": "POSTPONE", "reason": "Showers likely during cleaning.", "conditions": ["showers"],
                      "confidence": 0.75, "next_cleaning_time": "2026-03-12T07:00"})
    make_pipeline(conn, settings, clock, marginal, backend=ScriptedBackend([out])).run()
    d = decisions(conn)[0]
    assert d.final_action == FinalAction.POSTPONE and d.model_source == "scripted-test"
    assert fmt_local(d.proposed_time, settings.site.tz) == "2026-03-12 07:00"


def test_model_time_outside_constraints_replaced(conn, settings, clock, ev):
    out = json.dumps({"action": "POSTPONE", "reason": "Rain.", "conditions": [], "confidence": 0.9,
                      "next_cleaning_time": "2026-03-11T11:00"})  # still within the drying buffer
    make_pipeline(conn, settings, clock, rain_bundle(), backend=ScriptedBackend([out])).run()
    d = decisions(conn)[0]
    assert any(f.startswith("MODEL_TIME_REJECTED") for f in d.flags)
    assert fmt_local(d.proposed_time, settings.site.tz) == "2026-03-12 06:00"


def test_invalid_model_json_falls_back(conn, settings, clock, ev):
    make_pipeline(conn, settings, clock, rain_bundle(), backend=ScriptedBackend(["not json"])).run()
    d = decisions(conn)[0]
    assert d.final_action == FinalAction.POSTPONE and d.model_error
    assert any(f.startswith("MODEL_UNAVAILABLE") for f in d.flags)


def test_low_confidence_model_ignored(conn, settings, clock, ev):
    out = json.dumps({"action": "SKIP", "reason": "Unsure.", "conditions": [], "confidence": 0.3,
                      "next_cleaning_time": None})
    make_pipeline(conn, settings, clock, rain_bundle(), backend=ScriptedBackend([out])).run()
    d = decisions(conn)[0]
    assert d.final_action == FinalAction.POSTPONE and any("LOW_CONFIDENCE" in f for f in d.flags)


def test_manual_approval_flow(conn, settings, clock, ev):
    settings.automation.auto_apply = False
    make_pipeline(conn, settings, clock, rain_bundle()).run()
    d = decisions(conn)[0]
    assert d.outcome == Outcome.PENDING_APPROVAL
    assert EventRepository(conn).get(ev.id).postpone_count == 0
    mgr = ScheduleManager(conn, settings, clock)
    approved = mgr.approve(d.id, "ops-lead")
    assert approved.outcome == Outcome.APPROVED and approved.reviewed_by == "ops-lead"
    assert EventRepository(conn).get(ev.id).scheduled_time == d.proposed_time
    with pytest.raises(ApprovalError):
        mgr.approve(d.id, "ops-lead")  # cannot apply twice


def test_reject_and_conflict(conn, settings, clock, ev):
    settings.automation.auto_apply = False
    make_pipeline(conn, settings, clock, rain_bundle()).run()
    d = decisions(conn)[0]
    mgr = ScheduleManager(conn, settings, clock)
    mgr.reject(d.id, "ops", "crew already booked")
    assert DecisionRepository(conn).get(d.id).outcome == Outcome.REJECTED
    # A recommendation whose event was edited afterwards cannot be approved.
    clock.advance(days=1)
    make_pipeline(conn, settings, clock, rain_bundle(fetched_at=clock())).run()
    pending = DecisionRepository(conn).pending()
    assert len(pending) == 1
    mgr.edit_event(ev.id, ev.scheduled_time + timedelta(days=2), "moved by ops")
    assert DecisionRepository(conn).get(pending[0].id).outcome == Outcome.SUPERSEDED


def test_operator_edit_after_decision_causes_conflict(conn, settings, clock, ev):
    settings.automation.auto_apply = False
    make_pipeline(conn, settings, clock, rain_bundle()).run()
    d = decisions(conn)[0]
    e = EventRepository(conn).get(ev.id)
    EventRepository(conn).reschedule(e, e.scheduled_time + timedelta(hours=1), "manual nudge", "operator",
                                     count_as_postponement=False)
    with pytest.raises(ApprovalError):
        ScheduleManager(conn, settings, clock).approve(d.id, "ops")
    assert DecisionRepository(conn).get(d.id).outcome == Outcome.CONFLICT


def test_new_decision_supersedes_old_pending(conn, settings, clock, ev):
    settings.automation.auto_apply = False
    make_pipeline(conn, settings, clock, rain_bundle()).run()
    clock.advance(days=1)
    make_pipeline(conn, settings, clock, make_bundle(fetched_at=clock())).run()
    outcomes = sorted(d.outcome.value for d in decisions(conn))
    assert outcomes == ["NO_CHANGE", "SUPERSEDED"]


def test_concurrent_run_blocked(conn, settings, clock, ev):
    RunRepository(conn).start("2026-03-10", "manual", clock())
    with pytest.raises(RunInProgressError):
        make_pipeline(conn, settings, clock).run()


def test_events_outside_horizon_not_evaluated(conn, settings, clock):
    EventRepository(conn).add(T0 + timedelta(days=10))
    s = make_pipeline(conn, settings, clock).run()
    assert s.evaluated == 0


def test_run_is_logged(conn, settings, clock, ev):
    make_pipeline(conn, settings, clock).run()
    run = RunRepository(conn).latest()
    assert run["status"] == "SUCCESS" and run["duration_ms"] is not None
    assert json.loads(run["summary_json"])["evaluated"] == 1
