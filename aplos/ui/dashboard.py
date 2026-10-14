from __future__ import annotations

import json

import streamlit as st

from aplos import APP_NAME
from aplos.database import DecisionRepository, EventRepository, ForecastRepository, RunInProgressError, RunRepository
from aplos.llm.advisor import build_advisor
from aplos.models import DataSource, EventStatus
from aplos.runtime import build_pipeline
from aplos.scheduler.daily import next_run_at
from aplos.timeutil import hours_between, parse_iso, utcnow
from aplos.ui.common import (
    ACTION_STYLE, ago, cached_forecast, card, conn, flags_html, freshness_pill, local, pill, settings,
)


def render() -> None:
    s = settings()
    c = conn()
    now = utcnow()
    st.title(APP_NAME)
    st.caption(f"{s.site.name} · {s.site.latitude:.4f}, {s.site.longitude:.4f} · {s.site.timezone}")

    if s.weather.provider == "mock":
        st.warning("Development mode: the weather provider is set to **mock** (synthetic data). "
                   "Recommendations based on it are never applied automatically.", icon="⚠️")

    runs = RunRepository(c)
    last_run = runs.latest()
    last_ok = runs.latest("SUCCESS")
    decisions = DecisionRepository(c)
    pending = decisions.pending()
    latest = decisions.latest()
    cached = cached_forecast(c, s)
    fresh_state = None
    if cached:
        age = hours_between(cached.fetched_at, now)
        fresh_state = "FRESH" if age <= s.rules.stale_after_hours else (
            "STALE" if age <= s.rules.max_forecast_age_hours else "UNAVAILABLE")
    last_summary = json.loads(last_run["summary_json"]) if last_run and last_run.get("summary_json") else {}

    # ---- operating status
    problems = []
    if last_run and last_run["status"] in ("FAILED", "ABANDONED"):
        problems.append("last run failed")
    if fresh_state in (None, "STALE", "UNAVAILABLE"):
        problems.append("forecast not fresh")
    if pending:
        problems.append(f"{len(pending)} recommendation(s) awaiting approval")
    if last_summary.get("forecast_freshness") == "CACHED":
        status, kind = "Offline — using cached forecast", "info"
    elif problems:
        status, kind = "Attention needed", "warn"
    elif last_run is None:
        status, kind = "Not run yet", "info"
    else:
        status, kind = "Operational", "good"

    col1, col2, col3 = st.columns(3)
    with col1:
        card("Operating status", status, "; ".join(problems) or "All checks passing", pill(kind.upper(), kind))
    with col2:
        upcoming = EventRepository(c).list(now, statuses=[EventStatus.SCHEDULED])
        if upcoming:
            e = upcoming[0]
            sub = f"Originally {local(e.original_time, s)}" if e.is_adjusted else "As originally planned"
            card("Next scheduled cleaning", local(e.scheduled_time, s), sub,
                 pill(f"postponed ×{e.postpone_count}", "warn") if e.postpone_count else "")
        else:
            card("Next scheduled cleaning", "None scheduled", "Add events on the Schedule page")
    with col3:
        card("Last successful run", local(parse_iso(last_ok["started_at"]), s) if last_ok else "Never",
             f"{ago(parse_iso(last_ok['started_at'])) if last_ok else ''} · next run {local(next_run_at(now, s), s, '%a %H:%M')}")

    col4, col5 = st.columns([1, 2])
    with col4:
        if cached:
            card("Latest forecast", local(cached.fetched_at, s), f"{cached.provider} · retrieved {ago(cached.fetched_at)}",
                 freshness_pill(fresh_state) + (pill("MANUAL", "info") if cached.source == DataSource.MANUAL else "")
                 + (pill("MOCK", "warn") if cached.source == DataSource.MOCK else ""))
        else:
            card("Latest forecast", "No forecast cached", "Fetch on the Weather page or enter data manually",
                 freshness_pill("UNAVAILABLE"))
        adv = build_advisor(s.llm).status()
        model_val = {"gemma-2b-local": "Gemma 2B (local)", "mock": "MOCK backend"}.get(adv.source, "Not in use")
        card("Language model", model_val, adv.detail,
             pill("AVAILABLE", "good") if adv.available and adv.source != "mock" else
             pill("MOCK", "warn") if adv.source == "mock" else pill("RULES ONLY", "info"))
    with col5:
        if latest:
            icon, label = ACTION_STYLE[latest.final_action.value]
            ev = EventRepository(c).get(latest.event_id)
            sub = (f"Event #{ev.id} at {local(latest.event_time_before, s)} · {latest.outcome.value.replace('_', ' ').title()}"
                   f" · decided {ago(latest.created_at)} · source: {latest.model_source}")
            if latest.proposed_time:
                sub += f" · proposed {local(latest.proposed_time, s)}"
            card("Latest recommendation", f"{icon} {label}",
                 sub, f'<div style="margin-top:.5rem">{latest.final_reason}</div>' + flags_html(latest.flags))
        else:
            card("Latest recommendation", "No decisions yet", "Run the pipeline to evaluate upcoming cleanings")

    if pending:
        st.info(f"{len(pending)} recommendation(s) are awaiting operator approval on the **Schedule** page.", icon="📝")

    st.subheader("Run pipeline")
    st.caption("Fetches the forecast (if online), evaluates cleanings within the next "
               f"{s.schedule.evaluation_horizon_hours} h and records decisions. Re-running on the same day is safe: "
               "events already decided today are not changed again.")
    if st.button("Run now", type="primary"):
        with st.spinner("Running pipeline…"):
            try:
                summary = build_pipeline(c, s).run(trigger="manual")
            except RunInProgressError:
                st.error("Another run is in progress.")
            else:
                (st.success if summary.status == "SUCCESS" else st.error)(
                    f"{summary.status}: evaluated {summary.evaluated}, applied {summary.applied}, "
                    f"awaiting approval {summary.pending_approval}, no change {summary.no_change}, "
                    f"insufficient data {summary.insufficient_data}, already decided today {summary.already_decided} "
                    f"· forecast {summary.forecast_freshness} · {summary.duration_ms} ms"
                )
                for err in summary.errors:
                    st.caption(f"⚠ {err}")

    with st.expander("Recent runs"):
        rows = [{
            "Run": r["id"], "Local date": r["run_date"], "Trigger": r["trigger"], "Status": r["status"],
            "Started": local(parse_iso(r["started_at"]), s, "%Y-%m-%d %H:%M:%S"),
            "Duration (ms)": r["duration_ms"], "Error": r["error"] or "",
        } for r in runs.list(25)]
        st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(f"Forecasts cached locally: {len(ForecastRepository(c).recent(1000))}")


