# Evaluation Report — APLOS Solar Panel Schedule Adjuster

- Generated: 2026-10-14T04:19:49+00:00
- Application version: 1.0.0
- Dataset: `synthetic_5_weeks.json` (synthetic)
- Decision mode: deterministic rules only

## Summary

| Metric | Value |
|---|---|
| Total scenarios evaluated | 105 |
| Correct schedule decisions | 84 |
| Accuracy | 80.0% |
| Adjust-vs-proceed accuracy | 81.9% |
| False postponements (adjusted when cleaning was fine) | 8 |
| Missed postponements (cleaned when it should have adjusted) | 11 |
| Wrong adjustment type (POSTPONE vs SKIP) | 2 |
| Undetermined (no trustworthy data) | 0 |

## By weather event (realized weather)

| Event | Scenarios | Correct | Accuracy |
|---|---|---|---|
| Rain events | 32 | 22 | 68.8% |
| Sand/dust events | 19 | 16 | 84.2% |
| No event | 59 | 51 | 86.4% |

Scenarios with both rain and sand are counted in both rows. "No event" means no significant rain or sand affected the cleaning in the realized weather (some had forecast false alarms).

## Confusion (label → predicted)

| Label → Predicted | Count |
|---|---|
| CLEAN->CLEAN | 51 |
| CLEAN->POSTPONE | 8 |
| POSTPONE->CLEAN | 9 |
| POSTPONE->POSTPONE | 28 |
| POSTPONE->SKIP | 1 |
| SKIP->CLEAN | 2 |
| SKIP->POSTPONE | 1 |
| SKIP->SKIP | 5 |

## Benchmark context

The original workshop implementation achieved 86% correct schedule adjustments in a five-week field test. That figure is a historical benchmark. The result above comes from a synthetic dataset and is not directly comparable: synthetic forecast errors and labels only approximate real site conditions.

## Method

Each scenario is replayed through the production pipeline (rules engine, optional model, reconciliation) in an isolated in-memory database with the clock fixed at the forecast issue time. The label is the hindsight-correct action computed from realized weather using the labelling policy recorded in the dataset metadata.
