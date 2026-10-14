"""Command-line interface: `python -m aplos <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from aplos import APP_NAME, __version__
from aplos.config import env


def _conn():
    from aplos.runtime import open_db

    return open_db()


def cmd_init_db(args: argparse.Namespace) -> int:
    from aplos.database import SettingsRepository, schema_version

    conn = _conn()
    repo = SettingsRepository(conn)
    if repo.updated_at() is None:
        repo.save(repo.load())  # persist defaults so they are visible/editable
    print(f"Database ready at {env.db_path()} (schema v{schema_version(conn)})")
    return 0


def cmd_run_now(args: argparse.Namespace) -> int:
    from aplos.database import RunInProgressError
    from aplos.runtime import build_pipeline, load_settings

    conn = _conn()
    settings = load_settings(conn)
    if args.mock_weather:
        settings.weather.provider = "mock"
        settings.weather.mock_scenario = args.mock_weather
    try:
        summary = build_pipeline(conn, settings).run(trigger="manual")
    except RunInProgressError as exc:
        print(f"Not started: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary.to_dict(), indent=2, default=str))
    return 0 if summary.status == "SUCCESS" else 1


def cmd_scheduler(args: argparse.Namespace) -> int:
    from aplos.runtime import build_pipeline
    from aplos.scheduler.daily import DailyScheduler

    conn = _conn()
    sched = DailyScheduler(conn, lambda c, s: build_pipeline(c, s))
    if args.once:
        summary = sched.tick()
        print(json.dumps(summary.to_dict(), indent=2, default=str) if summary else "Not due; nothing to do.")
        return 0
    try:
        sched.serve_forever(poll_seconds=args.poll)
    except KeyboardInterrupt:
        sched.stop()
    return 0


def cmd_fetch_weather(args: argparse.Namespace) -> int:
    from aplos.runtime import load_settings
    from aplos.weather.service import WeatherService

    conn = _conn()
    result = WeatherService(conn, load_settings(conn)).get_forecast()
    b = result.bundle
    print(f"Freshness: {result.freshness.value}")
    if b:
        print(f"Provider: {b.provider} ({b.source.value}), fetched {b.fetched_at.isoformat()}, {len(b.points)} points")
        for w in b.warnings:
            print(f"  warning: {w}")
    for e in result.errors:
        print(f"  error: {e}")
    return 0 if result.usable else 1


def cmd_check_model(args: argparse.Namespace) -> int:
    from aplos.llm.advisor import build_advisor, resolve_model_path
    from aplos.llm.backends import LlamaCppBackend
    from aplos.runtime import load_settings

    settings = load_settings(_conn()).llm
    if args.path:
        settings.model_path = args.path
        settings.mode = "local"
    path = Path(resolve_model_path(settings))
    print(f"Mode:              {settings.mode}")
    print(f"Model path:        {path}")
    print(f"Model file exists: {path.is_file()}" + (f" ({path.stat().st_size / 1e9:.2f} GB)" if path.is_file() else ""))
    print(f"llama-cpp-python:  {'installed' if LlamaCppBackend.library_available() else 'NOT installed'}")
    advisor = build_advisor(settings, preload=True)
    st = advisor.status()
    print(f"Status:            {'AVAILABLE' if st.available else 'UNAVAILABLE'} - {st.detail}")
    if st.available and args.generate:
        sample = {
            "rule_engine": {"default_action": "POSTPONE", "allowed_actions": ["POSTPONE", "SKIP"], "findings": [
                "Rain 6.0 mm (significant, max probability 80%) 08:00–14:00, spanning cleaning"],
                "earliest_reschedule_local": "2026-03-12T06:00", "suggested_reschedule_local": "2026-03-12T06:00",
                "latest_allowed_local": "2026-03-15T10:00"},
            "planned_cleaning_local": "2026-03-11T10:00",
        }
        result = advisor.recommend(sample)
        print(f"Test inference ({result.latency_ms} ms, source={result.source}):")
        print(result.recommendation.model_dump_json(indent=2) if result.recommendation else f"  failed: {result.error}")
        return 0 if result.recommendation else 1
    return 0 if st.available or settings.mode != "local" else 1


def cmd_seed_schedule(args: argparse.Namespace) -> int:
    from aplos.runtime import load_settings
    from aplos.scheduler.schedule import ScheduleManager

    conn = _conn()
    start = date.fromisoformat(args.start) if args.start else date.today()
    created = ScheduleManager(conn, load_settings(conn)).generate_recurring(start, args.count, args.interval, args.time)
    print(f"Created {len(created)} cleaning events")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from aplos.database import DecisionRepository, EventRepository, RunRepository
    from aplos.models import EventStatus
    from aplos.runtime import load_settings
    from aplos.scheduler.daily import next_run_at
    from aplos.timeutil import fmt_local, utcnow

    conn = _conn()
    s = load_settings(conn)
    tz = s.site.tz
    now = utcnow()
    upcoming = EventRepository(conn).list(now, statuses=[EventStatus.SCHEDULED])
    last = RunRepository(conn).latest()
    d = DecisionRepository(conn).latest()
    print(f"{APP_NAME} v{__version__} - site {s.site.name} ({s.site.timezone})")
    print(f"Next daily run:  {fmt_local(next_run_at(now, s), tz)}")
    print(f"Last run:        {last['status'] + ' at ' + last['started_at'] if last else 'never'}")
    print(f"Next cleaning:   {fmt_local(upcoming[0].scheduled_time, tz) if upcoming else 'none scheduled'}")
    if d:
        print(f"Last decision:   {d.final_action.value} / {d.outcome.value} - {d.final_reason}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from aplos.simulation.evaluate import run_evaluation
    from aplos.simulation.scenarios import generate_dataset, load_dataset, save_dataset

    import logging

    logging.getLogger("aplos").setLevel(logging.WARNING)  # one log line per scenario is noise here
    dataset = env.resolve_project_path(args.dataset)
    out = env.resolve_project_path(args.out)
    if args.generate or not dataset.exists():
        save_dataset(generate_dataset(weeks=args.weeks, seed=args.seed), dataset)
        print(f"Generated synthetic dataset: {dataset}")
    kind = "synthetic" if "synthetic" in dataset.name else "user-supplied"
    report = run_evaluation(load_dataset(dataset), llm_mode=args.llm, out_dir=out, dataset_name=dataset.name,
                            dataset_kind=kind)
    m = report["metrics"]
    print(f"Scenarios: {m['total']}  correct: {m['correct']}  accuracy: {m['accuracy_pct']:.1f}%")
    print(f"False postponements: {m['false_postponements']}  missed postponements: {m['missed_postponements']}")
    print(f"Report written to {out / 'evaluation_report.md'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aplos", description=APP_NAME)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create/migrate the local database").set_defaults(fn=cmd_init_db)

    r = sub.add_parser("run-now", help="Run the pipeline immediately")
    r.add_argument("--mock-weather", choices=["clear", "rain", "heavy_rain", "sand", "mixed"],
                   help="DEVELOPMENT: use synthetic weather for this run")
    r.set_defaults(fn=cmd_run_now)

    s = sub.add_parser("scheduler", help="Run the daily scheduler daemon")
    s.add_argument("--poll", type=float, default=60.0, help="Seconds between checks (default 60)")
    s.add_argument("--once", action="store_true", help="Check once (for OS schedulers) and exit")
    s.set_defaults(fn=cmd_scheduler)

    sub.add_parser("fetch-weather", help="Fetch and cache a forecast").set_defaults(fn=cmd_fetch_weather)

    m = sub.add_parser("check-model", help="Check the local Gemma model installation")
    m.add_argument("--path", help="Override model path for this check")
    m.add_argument("--generate", action="store_true", help="Run a test inference")
    m.set_defaults(fn=cmd_check_model)

    g = sub.add_parser("seed-schedule", help="Create a recurring cleaning plan")
    g.add_argument("--start", help="First local date YYYY-MM-DD (default today)")
    g.add_argument("--count", type=int, default=6)
    g.add_argument("--interval", type=int, default=None, help="Days between cleanings")
    g.add_argument("--time", default=None, help="Local start time HH:MM")
    g.set_defaults(fn=cmd_seed_schedule)

    sub.add_parser("status", help="Show current status").set_defaults(fn=cmd_status)

    sim = sub.add_parser("simulate", help="Evaluate decisions against a labelled scenario dataset")
    sim.add_argument("--dataset", default="simulation/datasets/synthetic_5_weeks.json")
    sim.add_argument("--generate", action="store_true", help="(Re)generate the synthetic dataset first")
    sim.add_argument("--weeks", type=int, default=5)
    sim.add_argument("--seed", type=int, default=42)
    sim.add_argument("--llm", choices=["disabled", "mock", "local"], default="disabled")
    sim.add_argument("--out", default="reports")
    sim.set_defaults(fn=cmd_simulate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from aplos.runtime import init_logging

    init_logging(console=True)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
