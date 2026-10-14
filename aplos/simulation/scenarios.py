"""SIMULATION ONLY: labelled weather scenarios for offline evaluation.

Each synthetic scenario has two weather series:

- *truth*: the weather that actually happened (generated first);
- *forecast*: what the pipeline sees at decision time — the truth with
  realistic forecast errors (timing shifts, amount errors, missed events
  and false alarms).

The label is the hindsight-correct action computed from the *truth* with
the labelling policy below, written independently of the rules engine.
Accuracy therefore measures how well forecast-based decisions match what
should have been done given the weather that occurred.

A real historical dataset can be used instead: provide archived forecasts
as issued and a label derived from observed weather (see README).
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aplos.models import Action

COLUMNS = ["h", "precip_mm", "precip_probability_pct", "wind_gust_kmh", "dust_ugm3", "temperature_c", "humidity_pct"]

# Hindsight labelling policy (operator's definition of the correct action).
LABEL_POLICY = {
    "pre_window_hours": 6,
    "post_window_hours": 36,
    "cleaning_duration_hours": 3,
    "significant_rain_mm": 2.0,
    "heavy_rain_mm": 10.0,
    "dry_buffer_hours": 12,
    "sand_dust_ugm3": 200,
    "settle_hours": 6,
}

CATEGORY_WEIGHTS = {"clear": 0.40, "light_rain": 0.10, "rain": 0.18, "heavy_rain": 0.10, "sand": 0.17, "mixed": 0.05}


@dataclass
class Scenario:
    id: str
    category: str  # generator category (what was simulated)
    issued_at: datetime
    cleaning_time: datetime
    label: Action
    label_reason: str
    truth_event: str  # rain | sand | rain+sand | none (within the label window)
    forecast: list[dict[str, Any]]
    truth_summary: dict[str, Any]


def _hourly_truth(
    rng: random.Random, hours: int, category: str, clean_h: int
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (truth points, event specs, background points without events)."""
    pts = []
    for h in range(hours):
        diurnal = math.sin(((h + 4) % 24 - 9) / 24 * 2 * math.pi)
        pts.append({
            "h": h, "precip_mm": 0.0, "wind_gust_kmh": round(rng.uniform(14, 30), 1),
            "dust_ugm3": round(rng.uniform(20, 120), 0), "temperature_c": round(29 + 7 * diurnal, 1),
            "humidity_pct": round(45 - 15 * diurnal, 0),
        })
    background = [dict(p) for p in pts]
    events = []

    def add_rain(intensity: tuple[float, float], dur: tuple[int, int]) -> None:
        start = clean_h + rng.randint(-10, 30)
        length = rng.randint(*dur)
        rate = rng.uniform(*intensity)
        events.append({"type": "rain", "start": start, "end": start + length, "rate": rate})
        for h in range(max(0, start), min(hours, start + length)):
            pts[h]["precip_mm"] = round(rate * rng.uniform(0.6, 1.4), 2)
            pts[h]["humidity_pct"] = min(100, pts[h]["humidity_pct"] + 35)

    def add_sand() -> None:
        start = clean_h + rng.randint(-14, 30)
        length = rng.randint(3, 12)
        peak = rng.uniform(300, 1500)
        events.append({"type": "sand", "start": start, "end": start + length, "peak": peak})
        for h in range(max(0, start), min(hours, start + length)):
            pts[h]["dust_ugm3"] = round(peak * rng.uniform(0.6, 1.0), 0)
            pts[h]["wind_gust_kmh"] = round(rng.uniform(50, 78), 1)

    if category == "light_rain":
        add_rain((0.1, 0.35), (2, 6))
    elif category == "rain":
        add_rain((0.4, 1.3), (3, 9))
    elif category == "heavy_rain":
        add_rain((1.6, 4.0), (4, 10))
    elif category == "sand":
        add_sand()
    elif category == "mixed":
        add_sand()
        add_rain((0.5, 3.0), (3, 8))
    return pts, events, background


def _forecast_from_truth(rng: random.Random, background: list[dict], events: list[dict], category: str) -> list[dict]:
    """Forecast = background weather + events re-placed with forecast errors."""
    hours = len(background)
    fc = [dict(p, precip_mm=0.0, precip_probability_pct=round(rng.uniform(0, 12), 0)) for p in background]
    for p in fc:
        p["dust_ugm3"] = round(p["dust_ugm3"] * rng.uniform(0.7, 1.3), 0)

    def place_rain(start: int, end: int, rate: float, prob: tuple[float, float]) -> None:
        pr = rng.uniform(*prob)
        for h in range(max(0, start), min(hours, end)):
            fc[h]["precip_mm"] = round(max(0.0, rate * rng.uniform(0.6, 1.4)), 2)
            fc[h]["precip_probability_pct"] = round(min(100, pr + rng.uniform(-8, 8)), 0)
            fc[h]["humidity_pct"] = min(100, fc[h]["humidity_pct"] + 30)

    def place_sand(start: int, end: int, peak: float) -> None:
        for h in range(max(0, start), min(hours, end)):
            fc[h]["dust_ugm3"] = round(peak * rng.uniform(0.6, 1.0), 0)
            fc[h]["wind_gust_kmh"] = round(rng.uniform(45, 75), 1)

    for e in events:
        shift = int(round(rng.gauss(0, 2.5)))
        if e["type"] == "rain":
            if rng.random() < 0.10:  # forecast largely misses the rain
                place_rain(e["start"] + shift, e["end"] + shift, e["rate"] * 0.25, (15, 35))
            else:
                factor = math.exp(rng.gauss(0, 0.45))
                place_rain(e["start"] + shift, e["end"] + shift, e["rate"] * factor, (55, 95))
        else:
            if rng.random() < 0.10:  # missed dust event
                continue
            place_sand(e["start"] + shift, e["end"] + shift, e["peak"] * math.exp(rng.gauss(0, 0.35)))

    if category == "clear":
        roll = rng.random()
        if roll < 0.09:  # false-alarm rain
            s = rng.randint(14, 50)
            place_rain(s, s + rng.randint(2, 6), rng.uniform(0.4, 1.2), (50, 80))
        elif roll < 0.14:  # false-alarm dust
            s = rng.randint(14, 50)
            place_sand(s, s + rng.randint(3, 8), rng.uniform(250, 600))
    return fc


def label_from_truth(truth: list[dict], clean_h: int) -> tuple[Action, str, str, dict[str, Any]]:
    """Hindsight-correct action from realized weather (independent of the rules engine)."""
    pol = LABEL_POLICY
    w0 = clean_h - pol["pre_window_hours"]
    w1 = clean_h + pol["cleaning_duration_hours"] + pol["post_window_hours"]
    window = [p for p in truth if w0 <= p["h"] < w1]
    rain_hours = [p["h"] for p in window if p["precip_mm"] >= 0.2]
    rain_total = round(sum(p["precip_mm"] for p in window), 2)
    sand_hours = [p["h"] for p in window if p["dust_ugm3"] >= pol["sand_dust_ugm3"]]
    rain_end = (max(rain_hours) + 1) if rain_hours else None
    sand_end = (max(sand_hours) + 1) if sand_hours else None
    rain_affects = rain_end is not None and rain_end + pol["dry_buffer_hours"] > clean_h
    sand_affects = sand_end is not None and sand_end + pol["settle_hours"] > clean_h
    summary = {"rain_total_mm": rain_total, "rain_end_h": rain_end, "sand_end_h": sand_end,
               "peak_dust": max((p["dust_ugm3"] for p in window), default=None)}

    significant = rain_total >= pol["significant_rain_mm"]
    event = "+".join(x for x, on in (("rain", significant and rain_affects), ("sand", sand_affects)) if on) or "none"
    if rain_total >= pol["heavy_rain_mm"] and not (sand_affects and sand_end and rain_end and sand_end > rain_end):
        return Action.SKIP, f"{rain_total} mm of rain fell: panels rinsed naturally", "rain" if not sand_affects else "rain+sand", summary
    if significant and rain_affects or sand_affects:
        why = []
        if significant and rain_affects:
            why.append(f"{rain_total} mm rain near the cleaning")
        if sand_affects:
            why.append("sand/dust during or after the cleaning")
        return Action.POSTPONE, " and ".join(why), event, summary
    return Action.CLEAN, "No significant rain or sand affected the cleaning", event, summary


def generate_dataset(weeks: int = 5, seed: int = 42, per_day: int = 3, tz_name: str = "Asia/Dubai") -> dict[str, Any]:
    rng = random.Random(seed)
    tz = ZoneInfo(tz_name)
    start_day = datetime(2026, 3, 2, 5, 0, tzinfo=tz)  # a Monday, 05:00 local
    hours = 84
    cats, weights = zip(*CATEGORY_WEIGHTS.items())
    scenarios = []
    for d in range(weeks * 7):
        issued_local = start_day + timedelta(days=d)
        for k in range(per_day):
            category = rng.choices(cats, weights)[0]
            clean_local_hour = rng.choice([6, 7, 8, 9, 10, 12, 14])
            cleaning_local = (issued_local + timedelta(days=1)).replace(hour=clean_local_hour)
            issued = issued_local.astimezone(timezone.utc)
            cleaning = cleaning_local.astimezone(timezone.utc)
            clean_h = int((cleaning - issued).total_seconds() // 3600)
            truth, events, background = _hourly_truth(rng, hours, category, clean_h)
            forecast = _forecast_from_truth(rng, background, events, category)
            label, reason, truth_event, summary = label_from_truth(truth, clean_h)
            scenarios.append({
                "id": f"D{d + 1:02d}-{k + 1}",
                "category": category,
                "issued_at": issued.isoformat(),
                "cleaning_time": cleaning.isoformat(),
                "label": label.value,
                "label_reason": reason,
                "truth_event": truth_event,
                "truth_summary": summary,
                "forecast": [[p[c] if c in p else None for c in COLUMNS] for p in forecast],
            })
    return {
        "meta": {
            "kind": "synthetic",
            "generator": "aplos.simulation.scenarios",
            "seed": seed,
            "weeks": weeks,
            "per_day": per_day,
            "timezone": tz_name,
            "columns": COLUMNS,
            "labeling_policy": LABEL_POLICY,
            "note": "Labels derived from simulated realized weather; forecasts include simulated errors.",
        },
        "scenarios": scenarios,
    }


def save_dataset(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def load_dataset(path: Path) -> list[Scenario]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cols = data["meta"]["columns"]
    out = []
    for s in data["scenarios"]:
        out.append(Scenario(
            id=s["id"], category=s["category"], issued_at=datetime.fromisoformat(s["issued_at"]),
            cleaning_time=datetime.fromisoformat(s["cleaning_time"]), label=Action(s["label"]),
            label_reason=s.get("label_reason", ""), truth_event=s.get("truth_event", "unknown"),
            forecast=[dict(zip(cols, row)) for row in s["forecast"]], truth_summary=s.get("truth_summary", {}),
        ))
    return out
