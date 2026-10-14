"""SIMULATION ONLY: replay labelled scenarios through the real pipeline and score them.

Each scenario runs in its own in-memory database with a fixed clock, so the
production database is never touched.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aplos import APP_NAME, __version__
from aplos.config.settings import AppSettings
from aplos.database import DecisionRepository, EventRepository, SettingsRepository, connect
from aplos.engine.pipeline import Pipeline
from aplos.llm.advisor import build_advisor
from aplos.models import Action, DataSource, FinalAction, ForecastBundle, ForecastPoint, PointKind
from aplos.simulation.scenarios import Scenario
from aplos.weather.mock import StaticWeatherProvider
from aplos.weather.service import WeatherService

log = logging.getLogger(__name__)

HISTORICAL_BENCHMARK_PCT = 86.0  # original workshop implementation, five-week field test


def _bundle(sc: Scenario) -> ForecastBundle:
    pts = []
    for row in sc.forecast:
        t = sc.issued_at + timedelta(hours=row["h"])
        pts.append(ForecastPoint(
            time=t, kind=PointKind.FORECAST,
            **{k: v for k, v in row.items() if k != "h" and v is not None},
        ))
    return ForecastBundle(
        provider="replay", source=DataSource.REPLAY, fetched_at=sc.issued_at, latitude=24.45, longitude=54.38,
        interval_hours=1.0, points=pts,
    )


def evaluate_scenario(sc: Scenario, settings: AppSettings, advisor: Any) -> dict[str, Any]:
    conn = connect(":memory:")
    try:
        SettingsRepository(conn).save(settings)
        EventRepository(conn).add(sc.cleaning_time.astimezone(timezone.utc), f"scenario {sc.id}")
        clock = lambda: sc.issued_at.astimezone(timezone.utc)  # noqa: E731
        weather = WeatherService(conn, settings, StaticWeatherProvider(_bundle(sc)), clock)
        Pipeline(conn, settings, weather, advisor, clock).run(trigger="simulation")
        decisions = DecisionRepository(conn).list()
        d = decisions[0] if decisions else None
        predicted = d.final_action.value if d else "NONE"
        return {
            "id": sc.id, "category": sc.category, "truth_event": sc.truth_event, "label": sc.label.value,
            "predicted": predicted, "correct": predicted == sc.label.value,
            "model_source": d.model_source if d else None, "flags": "; ".join(d.flags) if d else "",
            "reason": d.final_reason if d else "", "label_reason": sc.label_reason,
        }
    finally:
        conn.close()


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    adjust = {Action.POSTPONE.value, Action.SKIP.value}
    total = len(rows)
    correct = sum(r["correct"] for r in rows)
    by_event: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "correct": 0})
    for r in rows:
        key = r["truth_event"]
        by_event[key]["total"] += 1
        by_event[key]["correct"] += int(r["correct"])
    for v in by_event.values():
        v["accuracy_pct"] = round(100 * v["correct"] / v["total"], 1) if v["total"] else 0.0

    def subset(pred: Any) -> dict[str, Any]:
        rs = [r for r in rows if pred(r)]
        c = sum(r["correct"] for r in rs)
        return {"total": len(rs), "correct": c, "accuracy_pct": round(100 * c / len(rs), 1) if rs else 0.0}

    confusion = Counter((r["label"], r["predicted"]) for r in rows)
    binary_correct = sum((r["label"] in adjust) == (r["predicted"] in adjust) for r in rows)
    return {
        "total": total,
        "correct": correct,
        "accuracy_pct": round(100 * correct / total, 1) if total else 0.0,
        "rain_events": subset(lambda r: "rain" in r["truth_event"]),
        "sand_events": subset(lambda r: "sand" in r["truth_event"]),
        "no_event": subset(lambda r: r["truth_event"] == "none"),
        "by_truth_event": dict(by_event),
        "false_postponements": sum(r["label"] == "CLEAN" and r["predicted"] in adjust for r in rows),
        "missed_postponements": sum(r["label"] in adjust and r["predicted"] == "CLEAN" for r in rows),
        "wrong_adjustment_type": sum(
            r["label"] in adjust and r["predicted"] in adjust and r["label"] != r["predicted"] for r in rows
        ),
        "undetermined": sum(r["predicted"] == FinalAction.UNDETERMINED.value for r in rows),
        "adjust_vs_proceed_accuracy_pct": round(100 * binary_correct / total, 1) if total else 0.0,
        "confusion": {f"{a}->{b}": n for (a, b), n in sorted(confusion.items())},
    }


def _markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        f"# Evaluation Report — {APP_NAME}",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Application version: {__version__}",
        f"- Dataset: `{report['dataset']}` ({report['dataset_kind']})",
        f"- Decision mode: {report['decision_mode']}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total scenarios evaluated | {m['total']} |",
        f"| Correct schedule decisions | {m['correct']} |",
        f"| Accuracy | {m['accuracy_pct']:.1f}% |",
        f"| Adjust-vs-proceed accuracy | {m['adjust_vs_proceed_accuracy_pct']:.1f}% |",
        f"| False postponements (adjusted when cleaning was fine) | {m['false_postponements']} |",
        f"| Missed postponements (cleaned when it should have adjusted) | {m['missed_postponements']} |",
        f"| Wrong adjustment type (POSTPONE vs SKIP) | {m['wrong_adjustment_type']} |",
        f"| Undetermined (no trustworthy data) | {m['undetermined']} |",
        "",
        "## By weather event (realized weather)",
        "",
        "| Event | Scenarios | Correct | Accuracy |",
        "|---|---|---|---|",
        f"| Rain events | {m['rain_events']['total']} | {m['rain_events']['correct']} | {m['rain_events']['accuracy_pct']:.1f}% |",
        f"| Sand/dust events | {m['sand_events']['total']} | {m['sand_events']['correct']} | {m['sand_events']['accuracy_pct']:.1f}% |",
        f"| No event | {m['no_event']['total']} | {m['no_event']['correct']} | {m['no_event']['accuracy_pct']:.1f}% |",
        "",
        "Scenarios with both rain and sand are counted in both rows. \"No event\" means no significant rain or "
        "sand affected the cleaning in the realized weather (some had forecast false alarms).",
        "",
        "## Confusion (label → predicted)",
        "",
        "| Label → Predicted | Count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in m["confusion"].items()],
        "",
        "## Benchmark context",
        "",
        f"The original workshop implementation achieved {HISTORICAL_BENCHMARK_PCT:.0f}% correct schedule adjustments "
        "in a five-week field test. That figure is a historical benchmark. The result above comes from "
        f"{'a synthetic dataset' if report['dataset_kind'] == 'synthetic' else 'the dataset listed'} and is not "
        "directly comparable: synthetic forecast errors and labels only approximate real site conditions.",
        "",
        "## Method",
        "",
        "Each scenario is replayed through the production pipeline (rules engine, optional model, reconciliation) "
        "in an isolated in-memory database with the clock fixed at the forecast issue time. The label is the "
        "hindsight-correct action computed from realized weather using the labelling policy recorded in the "
        "dataset metadata.",
        "",
    ]
    if report.get("model_note"):
        lines += ["## Model", "", report["model_note"], ""]
    return "\n".join(lines)


def run_evaluation(
    scenarios: list[Scenario],
    llm_mode: str = "disabled",
    out_dir: Path = Path("reports"),
    settings: AppSettings | None = None,
    dataset_name: str = "dataset",
    dataset_kind: str = "synthetic",
) -> dict[str, Any]:
    settings = settings or AppSettings()
    settings.llm.mode = llm_mode  # type: ignore[assignment]
    settings.weather.provider = "open_meteo"
    advisor = build_advisor(settings.llm, preload=llm_mode == "local")
    status = advisor.status()
    model_note = None
    if llm_mode == "local" and not status.available:
        model_note = f"Local model requested but unavailable ({status.detail}); decisions used deterministic rules only."
    elif llm_mode == "mock":
        model_note = "Mock backend: echoes the rule engine; this does not evaluate a language model."
    elif llm_mode == "local":
        model_note = f"Local model: {status.detail}."

    rows = [evaluate_scenario(sc, settings, advisor) for sc in scenarios]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": dataset_name,
        "dataset_kind": dataset_kind,
        "decision_mode": {"disabled": "deterministic rules only", "mock": "rules + mock model",
                          "local": "rules + local Gemma 2B" if status.available else "rules only (model unavailable)"}[llm_mode],
        "model_note": model_note,
        "metrics": compute_metrics(rows),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evaluation_report.md").write_text(_markdown(report), encoding="utf-8")
    (out_dir / "evaluation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (out_dir / "evaluation_scenarios.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["id"])
        writer.writeheader()
        writer.writerows(rows)
    return report
