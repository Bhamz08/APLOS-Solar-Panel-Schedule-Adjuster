import json

from aplos.cli import main
from aplos.models import Action
from aplos.simulation.evaluate import compute_metrics, run_evaluation
from aplos.simulation.scenarios import generate_dataset, label_from_truth, load_dataset, save_dataset


def test_labeler_uses_realized_weather():
    dry = [{"h": h, "precip_mm": 0.0, "dust_ugm3": 50} for h in range(80)]
    assert label_from_truth(dry, 30)[0] == Action.CLEAN
    wet = [dict(p, precip_mm=2.0 if 26 <= p["h"] < 34 else 0.0) for p in dry]
    assert label_from_truth(wet, 30)[0] == Action.SKIP  # 16 mm
    dusty = [dict(p, dust_ugm3=900 if 40 <= p["h"] < 45 else 50) for p in dry]
    assert label_from_truth(dusty, 30)[0] == Action.POSTPONE


def test_dataset_is_reproducible(tmp_path):
    a, b = generate_dataset(weeks=1, seed=3), generate_dataset(weeks=1, seed=3)
    assert a == b and len(a["scenarios"]) == 21
    save_dataset(a, tmp_path / "d.json")
    assert len(load_dataset(tmp_path / "d.json")) == 21


def test_evaluation_report(tmp_path):
    save_dataset(generate_dataset(weeks=1, seed=5), tmp_path / "d.json")
    report = run_evaluation(load_dataset(tmp_path / "d.json"), out_dir=tmp_path / "out")
    m = report["metrics"]
    assert m["total"] == 21 and 0 <= m["accuracy_pct"] <= 100
    assert m["correct"] + m["false_postponements"] + m["missed_postponements"] + m["wrong_adjustment_type"] \
        + m["undetermined"] == m["total"]
    assert (tmp_path / "out" / "evaluation_report.md").read_text(encoding="utf-8").count("86%") == 1


def test_metrics_counts():
    rows = [
        {"label": "CLEAN", "predicted": "POSTPONE", "correct": False, "truth_event": "none"},
        {"label": "POSTPONE", "predicted": "CLEAN", "correct": False, "truth_event": "sand"},
        {"label": "SKIP", "predicted": "SKIP", "correct": True, "truth_event": "rain"},
        {"label": "SKIP", "predicted": "POSTPONE", "correct": False, "truth_event": "rain"},
    ]
    m = compute_metrics(rows)
    assert (m["false_postponements"], m["missed_postponements"], m["wrong_adjustment_type"]) == (1, 1, 1)
    assert m["rain_events"]["accuracy_pct"] == 50.0
    assert m["adjust_vs_proceed_accuracy_pct"] == 50.0


def test_cli_run_now_with_mock_weather(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APLOS_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("APLOS_LOG_DIR", str(tmp_path / "logs"))
    assert main(["init-db"]) == 0
    assert main(["seed-schedule", "--count", "2", "--interval", "7"]) == 0
    capsys.readouterr()
    assert main(["run-now", "--mock-weather", "clear"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "SUCCESS" and summary["forecast_source"] == "mock:clear"
    assert main(["status"]) == 0
