from datetime import timedelta

from aplos.engine.rules import evaluate_rules, snap_to_cleaning_hours
from aplos.models import Action
from aplos.timeutil import fmt_local
from tests.conftest import T0, make_bundle
from tests.helpers import combine, event, heavy_bundle, rain, rain_bundle, sand, sand_bundle


def test_favorable_weather_cleans(settings):
    ev = evaluate_rules(event(), make_bundle(), settings)
    assert ev.deterministic_action == Action.CLEAN
    assert ev.allowed_actions == [Action.CLEAN]
    assert ev.sufficient_coverage


def test_rain_before_cleaning_postpones_after_dry_buffer(settings):
    ev = evaluate_rules(event(), rain_bundle(), settings)
    assert ev.rain.level == "significant"
    assert ev.deterministic_action == Action.POSTPONE
    assert set(ev.allowed_actions) == {Action.POSTPONE, Action.SKIP}
    # Rain ends T0+34h; +12h dry buffer = 02:00 local → snapped to 06:00 local next day.
    assert fmt_local(ev.postpone_candidate, settings.site.tz) == "2026-03-12 06:00"


def test_heavy_rain_skips(settings):
    ev = evaluate_rules(event(), heavy_bundle(), settings, next_event_time=T0 + timedelta(days=14))
    assert ev.rain.level == "heavy"
    assert ev.deterministic_action == Action.SKIP
    assert ev.skip_next_time == T0 + timedelta(days=14)


def test_rain_dried_out_before_cleaning_is_ignored(settings):
    settings.rules.pre_window_hours = 24
    settings.rules.dry_buffer_hours = 6
    ev = evaluate_rules(event(), make_bundle(override=rain(8, 12, 1.0)), settings)
    assert ev.rain.level == "significant" and not ev.rain.relevant
    assert ev.deterministic_action == Action.CLEAN


def test_sand_after_cleaning_postpones(settings):
    ev = evaluate_rules(event(), sand_bundle(), settings)
    assert ev.sand.relevant and ev.sand.indicator == "dust_concentration"
    assert ev.deterministic_action == Action.POSTPONE
    assert ev.allowed_actions == [Action.POSTPONE]
    assert ev.postpone_candidate >= ev.sand.end + timedelta(hours=settings.rules.dust_settle_hours)


def test_sand_well_before_cleaning_is_fine(settings):
    ev = evaluate_rules(event(), make_bundle(override=sand(5, 10)), settings)
    assert ev.deterministic_action == Action.CLEAN


def test_wind_proxy_only_without_dust_data(settings):
    gusty = make_bundle(override=sand(29, 32, dust=None, gust=60))
    ev = evaluate_rules(event(), gusty, settings)
    assert ev.sand.indicator == "wind_proxy" and ev.deterministic_action == Action.POSTPONE
    settings.rules.use_wind_as_sand_proxy = False
    assert evaluate_rules(event(), gusty, settings).deterministic_action == Action.CLEAN


def test_marginal_rain_lets_model_choose(settings):
    ev = evaluate_rules(event(), make_bundle(override=rain(30, 32, 0.5, prob=45)), settings)
    assert ev.rain.level == "marginal"
    assert ev.deterministic_action == Action.CLEAN
    assert set(ev.allowed_actions) == {Action.CLEAN, Action.POSTPONE}
    assert ev.postpone_candidate is not None


def test_thresholds_are_configurable(settings):
    settings.rules.rain_amount_threshold_mm = 8
    settings.rules.skip_rain_amount_mm = 20
    assert evaluate_rules(event(), rain_bundle(), settings).rain.level == "marginal"


def test_low_probability_rain_not_significant(settings):
    ev = evaluate_rules(event(), make_bundle(override=rain(28, 34, 1.0, prob=30)), settings)
    assert ev.rain.level == "marginal" and ev.deterministic_action == Action.CLEAN


def test_postponement_count_limit(settings):
    ev = evaluate_rules(event(postpone_count=3), rain_bundle(), settings)
    assert ev.postpone_limit_reached
    assert Action.POSTPONE not in ev.allowed_actions
    assert ev.deterministic_action == Action.SKIP  # rain: skip instead of endless postponing


def test_sand_with_limit_reached_proceeds(settings):
    ev = evaluate_rules(event(postpone_count=3), sand_bundle(), settings)
    assert ev.deterministic_action == Action.CLEAN and ev.postpone_limit_reached


def test_max_postpone_hours_limit(settings):
    settings.rules.max_postpone_hours = 6
    ev = evaluate_rules(event(), rain_bundle(), settings)
    assert ev.postpone_limit_reached and ev.postpone_candidate is None


def test_slot_search_skips_second_storm(settings):
    # Rain, then a sand event at the first candidate slot: candidate must move past both.
    bundle = make_bundle(override=combine(rain(28, 34, 1.0), sand(48, 54)))
    ev = evaluate_rules(event(), bundle, settings)
    assert ev.deterministic_action == Action.POSTPONE
    assert ev.postpone_candidate >= T0 + timedelta(hours=54 + settings.rules.dust_settle_hours)


def test_insufficient_coverage(settings):
    ev = evaluate_rules(event(), make_bundle(hours=40), settings)
    assert not ev.sufficient_coverage


def test_snap_to_cleaning_hours(settings):
    tz = settings.site.tz
    # 18:00 local is after the latest start (16) → next day 06:00 local.
    late = T0 + timedelta(hours=14)
    assert fmt_local(snap_to_cleaning_hours(late, settings), tz) == "2026-03-11 06:00"
    early = T0  # 04:00 local
    assert fmt_local(snap_to_cleaning_hours(early, settings), tz) == "2026-03-10 06:00"
